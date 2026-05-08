import csv
import io
import json
import os
import zipfile
import re
import logging
import uuid
import mimetypes
from decimal import Decimal, InvalidOperation
from urllib.parse import quote_plus
from urllib import error as urlerror
from urllib import request as urlrequest
from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.staticfiles import finders
from django.core.files.storage import default_storage
from django.db import OperationalError, ProgrammingError
from django.db.models import Q
from django.http import FileResponse, HttpResponse, JsonResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.models import (
    Application,
    ApplicationHistory,
    ChatMessage,
    DocumentRule,
    MasterDataField,
    PaymentSetting,
    PortalNews,
    PushNotificationLog,
    UserDocument,
    UserProfile,
    UserToken,
    Vacancy,
)
from PIL import Image

logger = logging.getLogger(__name__)
DEFAULT_CATALOG = [
    {
        "category": Vacancy.CATEGORY_GOVERNMENT,
        "title": "CGPSC State Service Exam 2026",
        "organization": "Chhattisgarh Public Service Commission",
        "last_date": date(2026, 4, 18),
        "icon_name": "work",
        "display_order": 10,
    },
    {
        "category": Vacancy.CATEGORY_GOVERNMENT,
        "title": "SSC CGL 2026",
        "organization": "Staff Selection Commission",
        "last_date": date(2026, 5, 10),
        "icon_name": "description",
        "display_order": 20,
    },
    {
        "category": Vacancy.CATEGORY_GOVERNMENT,
        "title": "India Post GDS Recruitment 2026",
        "organization": "Department of Posts",
        "last_date": date(2026, 3, 30),
        "icon_name": "mail",
        "display_order": 30,
    },
    {
        "category": Vacancy.CATEGORY_STUDENT,
        "title": "University Exam Form",
        "organization": "Student Examination Portal",
        "last_date": date(2026, 3, 28),
        "icon_name": "assignment",
        "display_order": 10,
    },
    {
        "category": Vacancy.CATEGORY_STUDENT,
        "title": "College Admission Form",
        "organization": "Higher Education Admission",
        "last_date": date(2026, 4, 12),
        "icon_name": "school",
        "display_order": 20,
    },
    {
        "category": Vacancy.CATEGORY_STUDENT,
        "title": "Scholarship Application Form",
        "organization": "Student Scholarship Portal",
        "last_date": date(2026, 4, 30),
        "icon_name": "workspace_premium",
        "display_order": 30,
    },
]

PROFILE_DATA_STEPS = [
    ("personal", "Personal Details"),
    ("address", "Address Details"),
    ("academic", "Academic Details"),
    ("college", "College Details"),
    ("bank", "Card Details"),
    ("subject", "Subject Details"),
    ("documents", "Document Upload"),
]

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
DEFAULT_REQUIRED_DOCS = []
APPLY_PENDING_TIMEOUT_MINUTES = 30
AUTOFILL_LOCK_HOURS = 24
APPLY_PROFILE_DAILY_VIEW_LIMIT = 10
APPLY_PROFILE_UNMASK_WINDOW_MINUTES = 10
APPLY_PROFILE_UNMASK_DAILY_LIMIT = 2


def _parse_multi_values(raw_text):
    if not raw_text:
        return []
    parts = []
    seen = set()
    for chunk in str(raw_text).replace("\r", "\n").replace(",", "\n").split("\n"):
        value = chunk.strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            parts.append(value)
    return parts


def _collect_multi_values(request, text_name, list_name):
    values = _parse_multi_values(request.POST.get(text_name, ""))
    extra = _parse_multi_values("\n".join(request.POST.getlist(list_name)))
    merged = []
    seen = set()
    for item in values + extra:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _collect_doc_editor_inputs(request):
    names = request.POST.getlist("doc_name[]")
    types = request.POST.getlist("doc_type[]")
    output = []
    for idx, raw_name in enumerate(names):
        name = str(raw_name or "").strip()
        if not name:
            continue
        t = str(types[idx] if idx < len(types) else "DOC").strip().upper()
        if t not in {"DOC", "PHOTO", "DATA"}:
            t = "DOC"
        output.append(f"{t}|{name}")
    return output


def _collect_field_editor_inputs(request):
    values = []
    raw_names = request.POST.getlist("field_name[]")
    raw_types = request.POST.getlist("field_type[]")
    raw_comments = request.POST.getlist("field_comment[]")
    for idx, raw in enumerate(raw_names):
        v = str(raw or "").strip()
        comment = str(raw_comments[idx] if idx < len(raw_comments) else "").strip()
        if v:
            t = str(raw_types[idx] if idx < len(raw_types) else "FIELD").strip().upper()
            if t == "COMMENT":
                values.append(f"COMMENT|{v}")
            elif comment:
                values.append(f"{v}||{comment}")
            else:
                values.append(v)
    return values


def _payload_has_any_values(payload):
    if not isinstance(payload, dict):
        return False
    for key, val in payload.items():
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict) and str(item.get("value", "")).strip():
                    return True
    # also handle vacancy_required_documents drafts if present
    if isinstance(payload.get("vacancy_required_documents"), list):
        for item in payload.get("vacancy_required_documents", []):
            if isinstance(item, dict) and str(item.get("value", "")).strip():
                return True
    return False


def _parse_required_doc_name(raw_value):
    value = str(raw_value or "").strip()
    if value.startswith("DATA|"):
        return "Data", value[5:].strip()
    if value.startswith("PHOTO|"):
        return "Photo", value[6:].strip()
    if value.startswith("DOC|"):
        return "Document", value[4:].strip()
    return "Document", value


def _norm_doc_key(value):
    raw = str(value or "").strip().lower()
    raw = raw.replace("required", "").replace("document", "").replace("photo", "")
    return "".join(ch for ch in raw if ch.isalnum())


def _norm_field_key(value):
    raw = str(value or "").strip().lower()
    return "".join(ch for ch in raw if ch.isalnum())


def _build_requested_profile_rows(step_data, required_profile_fields):
    requested = [str(item or "").strip() for item in (required_profile_fields or []) if str(item or "").strip()]
    if not requested:
        return {}

    all_candidates = []
    for step_key, _ in PROFILE_DATA_STEPS:
        if step_key == "documents":
            continue
        for label, value in step_data.get(step_key, []):
            all_candidates.append(
                {
                    "step": step_key,
                    "label": label,
                    "value": value or "",
                    "norm": _norm_field_key(label),
                }
            )

    def _alias_norms(req_norm):
        aliases = [req_norm]
        alias_rules = [
            (["studentname", "candidatename", "applicantname", "name"], ["fullname"]),
            (["fathersname", "fathername", "guardianname"], ["fathername"]),
            (["mothersname", "mothername"], ["mothername"]),
            (["gender", "sex"], ["gender"]),
            (["category", "caste"], ["category"]),
            (["dateofbirth", "dob", "birth"], ["dob", "dateofbirth"]),
            (["contactinfo", "mobileno", "mobile"], ["mobile"]),
            (["email", "mailid"], ["email"]),
            (["nationality"], ["nationality"]),
            (["state", "district"], ["presentstate", "presentdistrict"]),
            (["religion"], ["religion"]),
            (["maritalstatus"], ["maritalstatus"]),
            (["rationcard"], ["rationcard", "rationcardnumber"]),
            (["bloodgroup"], ["bloodgroup"]),
            (["houseno", "wardno"], ["housewardno", "houseno", "wardno"]),
            (["village", "post"], ["presentcity", "villagepost"]),
            (["tehsil", "policest"], ["tehsilpolicest"]),
            (["pincode", "postalcode"], ["presentpincode", "pincode"]),
            (["aadharnumber", "aadhaarnumber", "aadhar"], ["aadhaar", "aadhar"]),
            (["schoolname"], ["schoolname", "twelfthboard"]),
            (["groupstream", "stream"], ["groupstream"]),
            (["subjects"], ["subjects"]),
            (["boardname"], ["twelfthboard", "tenthboard"]),
            (["passingyear"], ["passingyear"]),
            (["rollnumber"], ["twelfthrollnumber", "tenthrollnumber", "rollnumber"]),
            (["marks", "percentage"], ["twelfthpercentage", "tenthpercentage", "marks"]),
            (["10thsubjects", "tenthsubjects"], ["tenthsubjects"]),
            (["12thsubjects", "twelfthsubjects"], ["twelfthsubjects"]),
            (["currentcourse", "course"], ["currentcoursename", "course"]),
            (["currentyear"], ["currentyear"]),
            (["currentsemester", "semester"], ["currentsemester"]),
            (["currentsubject", "mysubject", "currentsubjects"], ["currentsubjects"]),
            (["collegename"], ["collegename"]),
            (["subjectgroup"], ["subjectgroup", "course"]),
        ]
        for tokens, targets in alias_rules:
            if any(tok in req_norm for tok in tokens):
                aliases.extend(targets)
        return _merge_unique_casefold(aliases)

    grouped = {key: [] for key, _ in PROFILE_DATA_STEPS if key != "documents"}
    used_idx = set()
    for req in requested:
        if req.startswith("COMMENT|"):
            grouped["personal"].append((req, "", ""))
            continue
        req_label, req_help = (req.split("||", 1) + [""])[:2] if "||" in req else (req, "")
        req_label = str(req_label).strip()
        req_help = str(req_help).strip()
        if not req_label:
            continue
        req_norm = _norm_field_key(req_label)
        best_idx = None
        for alias in _alias_norms(req_norm):
            for idx, item in enumerate(all_candidates):
                if idx in used_idx:
                    continue
                if alias and item["norm"] == alias:
                    best_idx = idx
                    break
            if best_idx is not None:
                break
            if alias:
                for idx, item in enumerate(all_candidates):
                    if idx in used_idx:
                        continue
                    if alias in item["norm"] or item["norm"] in alias:
                        best_idx = idx
                        break
            if best_idx is not None:
                break

        if best_idx is not None:
            used_idx.add(best_idx)
            match = all_candidates[best_idx]
            grouped[match["step"]].append((req_label, match["value"], req_help))
        else:
            grouped["personal"].append((req_label, "", req_help))

    return {k: v for k, v in grouped.items() if v}


def _merge_unique_casefold(values):
    merged = []
    seen = set()
    for item in values or []:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(text)
    return merged


def _parse_profile_field_entry(raw_item):
    text = str(raw_item or "").strip()
    if not text:
        return {"kind": "FIELD", "label": "", "comment": ""}
    if text.startswith("COMMENT|"):
        return {"kind": "COMMENT", "label": text[8:].strip(), "comment": ""}
    if "||" in text:
        label, comment = text.split("||", 1)
        return {"kind": "FIELD", "label": label.strip(), "comment": comment.strip()}
    return {"kind": "FIELD", "label": text, "comment": ""}


def _normalize_visibility_key(value):
    return "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum())


def _vacancy_visible_to_profile(vacancy, profile):
    if getattr(vacancy, "hidden_from_users", False):
        return False
    allowed = getattr(vacancy, "visible_to_users", []) or []
    if not allowed:
        return True
    profile_keys = {
        _normalize_visibility_key(getattr(profile.user, "username", "")),
        _normalize_visibility_key(getattr(profile, "full_name", "")),
        _normalize_visibility_key(getattr(profile, "mobile", "")),
        _normalize_visibility_key(getattr(profile, "email", "")),
        _normalize_visibility_key(getattr(profile, "id", "")),
        _normalize_visibility_key(getattr(profile.user, "id", "")),
    }
    profile_keys.discard("")
    allowed_keys = {_normalize_visibility_key(item) for item in allowed}
    allowed_keys.discard("")
    return bool(profile_keys & allowed_keys)


def _classify_bulk_requirement(section_name, field_name):
    section = str(section_name or "").strip().lower()
    field = str(field_name or "").strip()
    key = field.lower()
    doc_section_tokens = {"documents", "document", "upload", "दस्तावेज", "documents (upload)"}
    photo_tokens = {"photo", "passport photo", "signature", "sign", "thumb"}
    document_tokens = {
        "marksheet", "certificate", "domicile", "abc id", "id", "aadhar card", "aadhaar card",
        "ration card", "caste", "niwas", "pdf", "document", "upload", "pan card",
    }

    if any(token in key for token in photo_tokens):
        return "PHOTO", field
    if section in doc_section_tokens or any(token in key for token in document_tokens):
        return "DOC", field
    return "DATA", field


def _parse_bulk_requirements(raw_text):
    docs = []
    profile_fields = []
    if not raw_text:
        return docs, profile_fields

    current_section = ""
    known_sections = {
        "personal", "personal info", "personal details",
        "address", "address details",
        "academic", "academic details", "education",
        "college", "college details",
        "bank", "bank details", "account",
        "documents", "document", "documents (upload)",
    }
    lines = [ln.strip() for ln in str(raw_text).splitlines() if ln.strip()]
    for line in lines:
        lower_line = line.lower()
        if "field name" in lower_line and "category" in lower_line:
            continue
        if "zaroori details" in lower_line and "remark" in lower_line:
            continue

        cols = []
        if "\t" in line:
            cols = [c.strip() for c in line.split("\t")]
        else:
            try:
                cols = next(csv.reader([line], skipinitialspace=True))
                cols = [c.strip() for c in cols]
            except Exception:
                cols = [line.strip()]

        if not cols:
            continue
        remark_text = ""
        if len(cols) == 1:
            field_names = [cols[0]]
            section_name = current_section
        else:
            section_name = cols[0] or current_section
            normalized_section = section_name.strip().lower()
            if normalized_section in known_sections:
                field_names = [cols[1]] if len(cols) > 1 and cols[1] else []
                remark_text = cols[2].strip() if len(cols) > 2 else ""
            else:
                section_name = current_section
                field_names = []
                if cols:
                    joined = [c for c in cols if c]
                    if len(joined) > 1:
                        field_names.append(joined[0])
                        remark_text = joined[1].strip() if len(joined) > 1 else ""
                    elif joined:
                        field_names.append(joined[0])

        if section_name:
            current_section = section_name
        for field_name in field_names:
            field_name = re.sub(r"\s+", " ", str(field_name or "")).strip().strip('"')
            if not field_name:
                continue
            if field_name.startswith("(") and field_name.endswith(")"):
                continue

            kind, clean_field = _classify_bulk_requirement(current_section, field_name)
            if not clean_field:
                continue
            if kind == "DATA":
                clean_remark = re.sub(r"\s+", " ", str(remark_text or "")).strip().strip('"')
                if clean_remark:
                    profile_fields.append(f"{clean_field}||{clean_remark}")
                else:
                    profile_fields.append(clean_field)
            else:
                docs.append(f"{kind}|{clean_field}")

    return _merge_unique_casefold(docs), _merge_unique_casefold(profile_fields)


def _parse_bulk_documents(raw_text):
    text = str(raw_text or "").strip()
    if not text:
        return []
    # Accept simple lists like:
    # PAN Card
    # Passport Photo
    # Signature
    # or explicit kind rows like DOC|PAN Card / PHOTO|Passport Photo / DATA|Something
    docs, _ = _parse_bulk_requirements(text)
    if docs:
        return docs
    items = []
    for line in text.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        # allow comma-separated in one line
        for part in [p.strip() for p in line.split(",") if p.strip()]:
            if part.upper().startswith(("DOC|", "PHOTO|", "DATA|")):
                items.append(part)
            else:
                kind, clean = _classify_bulk_requirement("documents", part)
                items.append(f"{kind}|{clean}")
    return _merge_unique_casefold(items)


def home_router(request):
    if request.user.is_authenticated:
        return redirect("master_data_option")
    return redirect("login")


def terms_conditions(request):
    return render(request, "legal/terms_conditions.html")


def privacy_policy(request):
    return render(request, "legal/privacy_policy.html")


def refund_policy(request):
    return render(request, "legal/refund_policy.html")


def _is_admin_user(user):
    return bool(user and (user.is_superuser or user.is_staff))


def _can_access_admin(request):
    return _is_admin_user(request.user)


def _seed_default_vacancies():
    for item in DEFAULT_CATALOG:
        vacancy, created = Vacancy.objects.get_or_create(
            category=item["category"],
            title=item["title"],
            organization=item["organization"],
            defaults={
                "last_date": item["last_date"],
                "icon_name": item["icon_name"],
                "display_order": item["display_order"],
                "is_active": True,
            },
        )
        updates = []
        if not vacancy.icon_name:
            vacancy.icon_name = item["icon_name"]
            updates.append("icon_name")
        if vacancy.display_order == 0:
            vacancy.display_order = item["display_order"]
            updates.append("display_order")
        if updates:
            vacancy.save(update_fields=updates)


def _status_label(value):
    return dict(Application.STATUS_CHOICES).get(value, value)


def _safe_hex_color(value, fallback):
    raw = str(value or "").strip()
    if len(raw) == 7 and raw.startswith("#"):
        try:
            int(raw[1:], 16)
            return raw
        except ValueError:
            return fallback
    return fallback


def _active_payment_setting():
    try:
        return (
            PaymentSetting.objects.filter(is_active=True).order_by("-updated_at", "-id").first()
            or PaymentSetting.objects.order_by("-updated_at", "-id").first()
        )
    except (OperationalError, ProgrammingError):
        # Payment table migrate pending ho to apply page crash na ho.
        return None


def _upi_deep_link(setting):
    if not setting or not setting.upi_id:
        return ""
    params = [f"pa={quote_plus(setting.upi_id.strip())}"]
    if setting.payee_name:
        params.append(f"pn={quote_plus(setting.payee_name.strip())}")
    try:
        amount = Decimal(setting.amount or 0)
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("0")
    if amount > 0:
        params.append(f"am={quote_plus(str(amount))}")
    params.append("cu=INR")
    if setting.note:
        params.append(f"tn={quote_plus(setting.note.strip())}")
    return "upi://pay?" + "&".join(params)


def _cashfree_enabled():
    return bool(
        getattr(settings, "CASHFREE_ENABLED", False)
        and getattr(settings, "CASHFREE_CLIENT_ID", "")
        and getattr(settings, "CASHFREE_CLIENT_SECRET", "")
    )


def _cashfree_amount(setting):
    try:
        amount = Decimal(getattr(setting, "amount", 0) or 0)
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("0")
    if amount <= 0:
        amount = Decimal("100")
    return amount.quantize(Decimal("0.01"))


def _cashfree_phone(profile):
    digits = "".join(ch for ch in str(getattr(profile, "mobile", "") or "") if ch.isdigit())
    if len(digits) >= 10:
        return digits[-10:]
    return "9999999999"


def _cashfree_email(profile):
    value = str(getattr(profile, "email", "") or "").strip()
    if value and "@" in value:
        return value
    username = profile.user.username if getattr(profile, "user", None) else "user"
    safe = re.sub(r"[^a-zA-Z0-9._-]", "", username) or "user"
    return f"{safe}@digiform.local"


def _cashfree_headers():
    return {
        "Content-Type": "application/json",
        "accept": "application/json",
        "x-client-id": settings.CASHFREE_CLIENT_ID,
        "x-client-secret": settings.CASHFREE_CLIENT_SECRET,
        "x-api-version": settings.CASHFREE_API_VERSION,
    }


def _cashfree_api_request(method, path, payload=None):
    if not _cashfree_enabled():
        raise RuntimeError("Cashfree credentials configured nahi hain.")
    raw_body = None
    if payload is not None:
        raw_body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        f"{settings.CASHFREE_API_BASE}{path}",
        data=raw_body,
        headers=_cashfree_headers(),
        method=method.upper(),
    )
    try:
        with urlrequest.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urlerror.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        logger.exception("Cashfree HTTP error: %s", body)
        try:
            data = json.loads(body)
            message = data.get("message") or data.get("error_description") or body
        except Exception:
            message = body or str(exc)
        raise RuntimeError(message or "Cashfree request failed.")
    except urlerror.URLError as exc:
        logger.exception("Cashfree URL error")
        raise RuntimeError(f"Cashfree se connect nahi ho paya: {exc.reason}")


def _extract_apply_submission(request, profile, vacancy):
    step_data = _profile_step_data(profile)
    required_profile_fields = [
        item for item in (vacancy.required_profile_fields or [])
        if str(item or "").strip()
    ]
    required_docs = _inject_required_docs_rows(profile, vacancy, step_data)
    required_doc_rows = _build_required_doc_rows(
        profile,
        required_docs,
        step_data=step_data,
        required_profile_fields=required_profile_fields,
    )
    requested_profile_rows = _build_requested_profile_rows(step_data, required_profile_fields)
    has_vacancy_specific_setup = bool(required_profile_fields or required_docs)
    use_requested_only = has_vacancy_specific_setup
    payload = {}
    selected_steps = []

    for step_key, _ in PROFILE_DATA_STEPS:
        if step_key == "documents":
            continue
        rows = requested_profile_rows.get(step_key, []) if use_requested_only else step_data.get(step_key, [])
        out_rows = []
        for idx, row in enumerate(rows):
            label = row[0]
            current_value = row[1] or ""
            if request.POST.get(f"select__{step_key}__{idx}") != "1":
                continue
            posted_value = request.POST.get(f"field__{step_key}__{idx}", current_value)
            out_rows.append({"label": label, "value": posted_value})
        if out_rows:
            payload[step_key] = out_rows
            selected_steps.append(step_key)

    selected_vac_docs = []
    for row in required_doc_rows:
        if request.POST.get(row["checkbox_name"]) != "1":
            continue
        posted_value = request.POST.get(row["input_name"], row.get("value", "") or "").strip()
        uploaded = request.FILES.get(row["file_input_name"])
        if uploaded:
            file_url = _save_profile_document(profile, row["label"], uploaded)
            posted_value = file_url or posted_value or "Uploaded"
        if not posted_value:
            posted_value = row.get("value", "") or ""
        if posted_value:
            selected_vac_docs.append({"label": row["label"], "value": posted_value})
    if selected_vac_docs:
        selected_steps.append("documents")

    return {
        "payload": payload,
        "selected_steps": selected_steps,
        "selected_vac_docs": selected_vac_docs,
        "required_doc_rows": required_doc_rows,
        "use_requested_only": use_requested_only,
    }


def _persist_application_submission(profile, vacancy, payload, selected_steps, selected_vac_docs):
    payload_to_store = dict(payload or {})
    if selected_vac_docs:
        payload_to_store["vacancy_required_documents"] = list(selected_vac_docs)
    step_names = {key: label for key, label in PROFILE_DATA_STEPS}
    selected_labels = [step_names.get(key, key) for key in selected_steps if key in step_names]
    summary_line = "Selected Data: " + ", ".join(selected_labels)
    if selected_vac_docs:
        summary_line += " | Vacancy Docs: " + ", ".join([x["label"] for x in selected_vac_docs])
    payload_line = "Payload JSON: " + json.dumps(payload_to_store, ensure_ascii=True)

    app, created = Application.objects.get_or_create(profile=profile, vacancy=vacancy)
    if app.status == Application.STATUS_CANCELLED:
        app.status = Application.STATUS_PENDING
        app.cancelled_at = None
    app.remarks = summary_line + "\n" + payload_line
    app.save()
    return app, created


def _cashfree_build_order(request, profile, vacancy, setting):
    amount = _cashfree_amount(setting)
    order_id = f"df_{vacancy.id}_{profile.id}_{uuid.uuid4().hex[:12]}"
    payload = {
        "order_id": order_id,
        "order_amount": float(amount),
        "order_currency": "INR",
        "customer_details": {
            "customer_id": f"profile_{profile.id}",
            "customer_name": (profile.full_name or profile.user.username or "Digi Form User")[:60],
            "customer_email": _cashfree_email(profile),
            "customer_phone": _cashfree_phone(profile),
        },
        "order_meta": {
            "return_url": request.build_absolute_uri(reverse("cashfree_return")) + "?order_id={order_id}",
        },
        "order_note": (f"{vacancy.title} application fee")[:180],
    }
    return _cashfree_api_request("POST", "/orders", payload)


def _normalize_external_link(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        return raw
    return f"https://{raw}"


def _news_for_portal(portal_key):
    try:
        qs = PortalNews.objects.filter(
            is_active=True,
        ).filter(
            Q(target_portal=PortalNews.TARGET_ALL) | Q(target_portal=portal_key)
        )
        # DB schema mismatch ho to yahin catch ho jaye, template render me 500 na aaye.
        qs.exists()
        return qs
    except (OperationalError, ProgrammingError):
        # Migration pending ho to dashboard crash na ho.
        return PortalNews.objects.none()


def _portal_news_queryset(portal_key="all"):
    try:
        qs = PortalNews.objects.filter(is_active=True)
        if portal_key in {PortalNews.TARGET_GOVERNMENT, PortalNews.TARGET_STUDENT}:
            qs = qs.filter(
                Q(target_portal=PortalNews.TARGET_ALL) | Q(target_portal=portal_key)
            )
        # Lazy queryset ko force-check karo, taaki missing column par fallback mile.
        qs.exists()
        return qs
    except (OperationalError, ProgrammingError):
        return PortalNews.objects.none()


def _profile_step_data(profile):
    step_data = {
        "personal": [
            ("Full Name", profile.full_name),
            ("Father Name", profile.father_name),
            ("Mother Name", profile.mother_name),
            ("DOB", profile.dob.strftime("%Y-%m-%d") if profile.dob else ""),
            ("Gender", profile.get_gender_display() if profile.gender else ""),
            ("Category", profile.category),
            ("Mobile", profile.mobile),
            ("Email", profile.email),
            ("Aadhaar", profile.aadhar),
        ],
        "address": [
            ("Present State", profile.present_state),
            ("Present District", profile.present_district),
            ("Present City", profile.present_city),
            ("Present Pincode", profile.present_pincode),
            ("Present Address", profile.present_address),
            ("Permanent State", profile.permanent_state),
            ("Permanent District", profile.permanent_district),
            ("Permanent Pincode", profile.permanent_pincode),
            ("Permanent Address", profile.permanent_full_address or profile.permanent_address),
        ],
        "academic": [
            ("10th Board", profile.tenth_board),
            ("10th Roll Number", profile.tenth_roll_number),
            ("10th Percentage", profile.tenth_percentage),
            ("12th Board", profile.twelfth_board),
            ("12th Roll Number", profile.twelfth_roll_number),
            ("12th Percentage", profile.twelfth_percentage),
            ("Graduation", profile.graduation),
        ],
        "college": [
            ("College Name", profile.college_name),
            ("University", profile.university_name),
            ("Course", profile.course),
            ("Year/Semester", profile.year_semester),
            ("Enrollment Number", profile.enrollment_number),
        ],
        "bank": [
            ("Card Holder", profile.account_holder_name),
            ("Card Name", profile.bank_name),
            ("Card Number", profile.account_number),
            ("IFSC", profile.ifsc_code),
            ("Branch", profile.branch_name),
            ("Aadhaar Linked", profile.aadhaar_linked),
        ],
        "subject": [
            ("10th Subjects", profile.tenth_subjects),
            ("12th Subjects", profile.twelfth_subjects),
            ("Graduation Details", profile.graduation),
            ("Graduation Subjects", profile.graduation_subjects),
        ],
        "documents": (
            [("Passport Photo", profile.photo_url)] if getattr(profile, "photo_url", "") else []
        )
        + ([("Passport Photo", profile.photo.url)] if getattr(profile, "photo", None) else [])
        + ([("Signature", profile.signature_url)] if getattr(profile, "signature_url", "") else [])
        + ([("Signature", profile.signature.url)] if getattr(profile, "signature", None) else [])
        + [
            (doc.title or "Document", (getattr(doc, "file_url", "") or (doc.file.url if getattr(doc, "file", None) else "")))
            for doc in profile.documents.all()
        ],
    }
    _append_extra_rows(step_data["personal"], profile.personal_extra_rows)
    _append_extra_rows(step_data["address"], profile.address_extra_rows)
    _append_extra_rows(step_data["academic"], profile.academic_extra_rows)
    _append_extra_rows(step_data["college"], profile.college_extra_rows)
    _append_extra_rows(step_data["bank"], profile.bank_extra_rows)
    _append_extra_rows(step_data["subject"], profile.subject_extra_rows)
    return step_data


def _append_extra_rows(target_rows, extra_rows):
    for row in extra_rows or []:
        label = str((row or {}).get("label", "")).strip()
        value = str((row or {}).get("value", "")).strip()
        if not label and not value:
            continue
        target_rows.append((label or "Custom Field", value))


def _extra_rows_as_text(extra_rows):
    entries = []
    for row in extra_rows or []:
        label = str((row or {}).get("label", "")).strip()
        value = str((row or {}).get("value", "")).strip()
        if not label and not value:
            continue
        entries.append(f"{label or 'Custom Field'}: {value}")
    return " | ".join(entries)


def _attachment_kind(file_ref):
    if hasattr(file_ref, "url") or hasattr(file_ref, "name"):
        file_name = getattr(file_ref, "name", "") or ""
        try:
            file_url = getattr(file_ref, "url", "") or ""
        except Exception:
            file_url = ""
    else:
        file_name = str(file_ref or "")
        file_url = str(file_ref or "")

    lower_name = (file_name or "").lower()
    lower_url = (file_url or "").lower()
    if lower_name.endswith(".pdf"):
        return "pdf"
    if lower_name.endswith(IMAGE_EXTENSIONS):
        return "image"
    if "res.cloudinary.com" in lower_url and "/image/" in lower_url:
        return "image"
    if "res.cloudinary.com" in lower_url and "/raw/" in lower_url and lower_name.endswith(".pdf"):
        return "pdf"
    return "file"


def _file_download_name(file_field):
    raw_name = (getattr(file_field, "name", "") or "").strip()
    return raw_name.rsplit("/", 1)[-1] if raw_name else "attachment"


def _safe_file_url(file_field):
    if not file_field:
        return ""
    name = getattr(file_field, "name", "") or ""
    if not name:
        return ""
    try:
        if not default_storage.exists(name):
            return ""
        return file_field.url
    except Exception:
        return ""


def _pending_started_at(pending):
    if not isinstance(pending, dict):
        return None
    raw = pending.get("started_at")
    if not raw:
        return None
    try:
        parsed = timezone.datetime.fromisoformat(raw)
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed
    except (TypeError, ValueError):
        return None


def _blank_step_rows(step_data):
    blanked = {}
    for key, rows in (step_data or {}).items():
        if not isinstance(rows, list):
            blanked[key] = rows
            continue
        blanked[key] = [(label, "") for label, _ in rows]
    return blanked


def _mask_text_value(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 2:
        return "***"
    if len(text) <= 6:
        return text[:1] + "***"
    return text[:2] + "***" + text[-2:]


def _mask_step_rows(step_data):
    masked = {}
    for key, rows in (step_data or {}).items():
        if not isinstance(rows, list):
            masked[key] = rows
            continue
        masked[key] = [(label, _mask_text_value(value)) for label, value in rows]
    return masked


def _norm_label_key(value):
    return "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum())


MASTER_FIELD_MAP = {
    "personal": {
        "name": "full_name",
        "fullname": "full_name",
        "fathername": "father_name",
        "mothername": "mother_name",
        "dob": "dob",
        "dateofbirth": "dob",
        "gender": "gender",
        "category": "category",
        "mobile": "mobile",
        "mobilenumber": "mobile",
        "email": "email",
        "emailid": "email",
        "aadhaar": "aadhar",
        "aadhar": "aadhar",
    },
    "address": {
        "presentstate": "present_state",
        "presentdistrict": "present_district",
        "presentcity": "present_city",
        "presentcityvillage": "present_city",
        "presentpincode": "present_pincode",
        "presentfullnameaddress": "present_address",
        "presentaddress": "present_address",
        "permanentstate": "permanent_state",
        "permanentdistrict": "permanent_district",
        "permanentpincode": "permanent_pincode",
        "permanentaddress": "permanent_full_address",
        "permanentfulladdress": "permanent_full_address",
    },
    "academic": {
        "10thboard": "tenth_board",
        "10throllnumber": "tenth_roll_number",
        "10thpercentage": "tenth_percentage",
        "12thboard": "twelfth_board",
        "12throllnumber": "twelfth_roll_number",
        "12thpercentage": "twelfth_percentage",
        "graduation": "graduation",
    },
    "college": {
        "collegename": "college_name",
        "university": "university_name",
        "universityname": "university_name",
        "course": "course",
        "yearsemester": "year_semester",
        "enrollmentnumber": "enrollment_number",
    },
    "bank": {
        "accountholder": "account_holder_name",
        "accountholdername": "account_holder_name",
        "bankname": "bank_name",
        "accountnumber": "account_number",
        "ifsc": "ifsc_code",
        "ifsccode": "ifsc_code",
        "branch": "branch_name",
        "branchname": "branch_name",
        "aadhaarlinked": "aadhaar_linked",
    },
    "subject": {
        "10thsubjects": "tenth_subjects",
        "12thsubjects": "twelfth_subjects",
        "previouscourse": "previous_course_name",
        "previouscoursename": "previous_course_name",
        "previousyearsubjects": "previous_subjects",
        "currentsubjects": "current_subjects",
        "mycurrentsubjects": "current_subjects",
        "currentcourse": "current_course_name",
        "currentcoursename": "current_course_name",
        "currentyear": "current_year",
        "currentsemester": "current_semester",
    },
}


def _save_payload_to_master_data(profile, payload):
    changed_fields = set()
    section_extra_map = {
        "personal": "personal_extra_rows",
        "address": "address_extra_rows",
        "academic": "academic_extra_rows",
        "college": "college_extra_rows",
        "bank": "bank_extra_rows",
        "subject": "subject_extra_rows",
    }

    for section, field_map in MASTER_FIELD_MAP.items():
        rows = payload.get(section, [])
        if not isinstance(rows, list):
            continue
        extra_attr = section_extra_map.get(section)
        existing_extras = list(getattr(profile, extra_attr, []) or [])
        extra_keys = {_norm_label_key((r or {}).get("label", "")) for r in existing_extras}
        for item in rows:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "")).strip()
            value = str(item.get("value", "")).strip()
            if not label or not value:
                continue
            key = _norm_label_key(label)
            target_attr = field_map.get(key)
            if target_attr:
                current = getattr(profile, target_attr, "")
                if current in {None, ""}:
                    normalized_value = value
                    if target_attr == "gender":
                        low = value.lower()
                        if low.startswith("m"):
                            normalized_value = "M"
                        elif low.startswith("f"):
                            normalized_value = "F"
                        else:
                            normalized_value = "O"
                    elif target_attr == "dob":
                        try:
                            normalized_value = date.fromisoformat(value)
                        except ValueError:
                            normalized_value = None
                    if normalized_value not in {None, ""}:
                        setattr(profile, target_attr, normalized_value)
                        changed_fields.add(target_attr)
                    continue
                continue
            if extra_attr and key and key not in extra_keys:
                existing_extras.append({"label": label, "value": value, "is_permanent": False})
                extra_keys.add(key)
                setattr(profile, extra_attr, existing_extras)
                changed_fields.add(extra_attr)

    if changed_fields:
        profile.save(update_fields=sorted(changed_fields))
    return len(changed_fields)


def _get_profile_draft(profile, vacancy_id):
    store = getattr(profile, "apply_draft_data", {}) or {}
    return store.get(str(vacancy_id), {}) if isinstance(store, dict) else {}


def _save_profile_draft(profile, vacancy_id, payload, vac_docs, started_at):
    store = dict(getattr(profile, "apply_draft_data", {}) or {})
    store[str(vacancy_id)] = {
        "draft_payload": payload if isinstance(payload, dict) else {},
        "draft_vacancy_docs": vac_docs if isinstance(vac_docs, list) else [],
        "started_at": started_at or "",
        "updated_at": timezone.now().isoformat(),
    }
    profile.apply_draft_data = store
    profile.save(update_fields=["apply_draft_data"])


def _clear_profile_draft(profile, vacancy_id):
    store = dict(getattr(profile, "apply_draft_data", {}) or {})
    key = str(vacancy_id)
    if key in store:
        store.pop(key, None)
        profile.apply_draft_data = store
        profile.save(update_fields=["apply_draft_data"])


def _is_pending_apply_timed_out(pending):
    started_at = _pending_started_at(pending)
    if not started_at:
        return False
    return timezone.now() > started_at + timedelta(minutes=APPLY_PENDING_TIMEOUT_MINUTES)


def _register_apply_profile_view(profile):
    today = timezone.localdate()
    if profile.apply_profile_view_date != today:
        profile.apply_profile_view_date = today
        profile.apply_profile_view_count = 0
    if profile.apply_profile_view_count >= APPLY_PROFILE_DAILY_VIEW_LIMIT:
        return False, 0
    profile.apply_profile_view_count += 1
    profile.save(update_fields=["apply_profile_view_date", "apply_profile_view_count"])
    return True, max(APPLY_PROFILE_DAILY_VIEW_LIMIT - profile.apply_profile_view_count, 0)


def _is_apply_profile_unmask_active(profile):
    until = getattr(profile, "apply_profile_unmask_until", None)
    return bool(until and timezone.now() < until)


def _grant_apply_profile_unmask(profile):
    today = timezone.localdate()
    if profile.apply_profile_unmask_date != today:
        profile.apply_profile_unmask_date = today
        profile.apply_profile_unmask_count = 0
    if profile.apply_profile_unmask_count >= APPLY_PROFILE_UNMASK_DAILY_LIMIT:
        return False, 0
    profile.apply_profile_unmask_count += 1
    profile.apply_profile_unmask_until = timezone.now() + timedelta(minutes=APPLY_PROFILE_UNMASK_WINDOW_MINUTES)
    profile.save(
        update_fields=[
            "apply_profile_unmask_date",
            "apply_profile_unmask_count",
            "apply_profile_unmask_until",
        ]
    )
    return True, max(APPLY_PROFILE_UNMASK_DAILY_LIMIT - profile.apply_profile_unmask_count, 0)


def _decorate_chat_messages(messages_qs):
    decorated = list(messages_qs)
    for item in decorated:
        if item.attachment:
            item.attachment_kind = _attachment_kind(item.attachment)
            item.attachment_name = item.attachment.name.split("/")[-1]
            try:
                item.attachment_open_url = item.attachment.url
            except Exception:
                item.attachment_open_url = reverse("chat_attachment_download", args=[item.id])
            item.attachment_download_url = reverse("chat_attachment_download", args=[item.id]) + "?dl=1"
        else:
            item.attachment_kind = ""
            item.attachment_name = ""
            item.attachment_open_url = ""
            item.attachment_download_url = ""
    return decorated


def _validate_chat_attachment(uploaded):
    if not uploaded:
        return None
    name = (getattr(uploaded, "name", "") or "").lower()
    size = int(getattr(uploaded, "size", 0) or 0)
    content_type = (getattr(uploaded, "content_type", "") or "").lower()

    # Keep it simple: images + pdf. Prevents weird types causing storage errors.
    allowed_ext = (".jpg", ".jpeg", ".png", ".webp", ".pdf")
    if not any(name.endswith(ext) for ext in allowed_ext):
        return "Sirf JPG/PNG/WEBP ya PDF allowed hai."

    # 10 MB max (safe for PythonAnywhere + Cloud uploads)
    if size and size > 10 * 1024 * 1024:
        return "File size 10MB se kam hona chahiye."

    if name.endswith(".pdf") and content_type and "pdf" not in content_type:
        # Some browsers send application/octet-stream; don't hard-block those.
        pass
    return None


def _chat_message_payload(msg):
    attachment_url = ""
    if msg.attachment:
        try:
            attachment_url = msg.attachment.url
        except Exception:
            attachment_url = ""
    return {
        "id": msg.id,
        "from_admin": bool(msg.from_admin),
        "message": msg.message or "",
        "time": msg.created_at.strftime("%H:%M"),
        "attachment": {
            "url": attachment_url,
            "open_url": attachment_url or (reverse("chat_attachment_download", args=[msg.id]) if msg.attachment else ""),
            "name": _file_download_name(msg.attachment) if msg.attachment else "",
            "kind": _attachment_kind(msg.attachment) if msg.attachment else "",
            "download_url": (reverse("chat_attachment_download", args=[msg.id]) + "?dl=1") if msg.attachment else "",
        },
    }


def _is_ajax_request(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _extract_payload_from_remarks(remarks_text):
    if not remarks_text:
        return {}
    marker = "Payload JSON:"
    idx = remarks_text.find(marker)
    if idx == -1:
        return {}
    json_text = remarks_text[idx + len(marker):].strip()
    if not json_text:
        return {}
    try:
        parsed = json.loads(json_text)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _rows_from_payload(payload, key, fallback_rows):
    raw_rows = payload.get(key)
    if not isinstance(raw_rows, list):
        return fallback_rows
    output = []
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        value = str(item.get("value", "")).strip()
        if not label and not value:
            continue
        output.append((label or "Custom Field", value))
    return output or fallback_rows


def _payload_rows_flat(payload):
    if not isinstance(payload, dict):
        return []
    out = []
    for key, val in payload.items():
        if not isinstance(val, list):
            continue
        for item in val:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "")).strip()
            value = str(item.get("value", "")).strip()
            if label or value:
                out.append((label, value))
    return out


def _payload_first_value(payload, *needles):
    rows = _payload_rows_flat(payload)
    if not rows:
        return ""
    norm_needles = [_norm_field_key(x) for x in needles if str(x).strip()]
    for label, value in rows:
        norm_label = _norm_field_key(label)
        if any(n and (n in norm_label or norm_label in n) for n in norm_needles):
            return value
    return ""


def _is_probable_image_url(title, url):
    lower_title = str(title or "").strip().lower()
    lower_url = str(url or "").strip().lower().split("?", 1)[0]
    if any(lower_url.endswith(ext) for ext in IMAGE_EXTENSIONS):
        return True
    if "res.cloudinary.com" in lower_url and "/image/" in lower_url:
        return True
    return any(token in lower_title for token in ["photo", "signature", "image", "passport"])


def _selected_payload(profile, selected_steps):
    all_data = _profile_step_data(profile)
    payload = {}
    for key in selected_steps:
        rows = all_data.get(key, [])
        payload[key] = [{"label": label, "value": value or ""} for label, value in rows]
    return payload


def _inject_required_docs_rows(profile, vacancy, step_data):
    required_docs = vacancy.required_documents or DEFAULT_REQUIRED_DOCS
    existing_labels = set()

    available = {}
    if getattr(profile, "photo_url", ""):
        available["passport photo"] = profile.photo_url
    elif getattr(profile, "photo", None):
        available["passport photo"] = profile.photo.url
    if getattr(profile, "signature_url", ""):
        available["signature"] = profile.signature_url
    elif getattr(profile, "signature", None):
        available["signature"] = profile.signature.url
    for doc in profile.documents.all():
        title = (doc.title or "").strip().lower()
        if title:
            available[title] = getattr(doc, "file_url", "") or (doc.file.url if getattr(doc, "file", None) else "")

    existing_master = {}
    for rows in step_data.values():
        if not isinstance(rows, list):
            continue
        for label, val in rows:
            key = str(label or "").strip().lower()
            if key and str(val or "").strip():
                existing_master[key] = str(val).strip()

    def _find_doc_value(doc_name):
        _, clean_doc_name = _parse_required_doc_name(doc_name)
        key = clean_doc_name.lower()
        norm_key = _norm_doc_key(clean_doc_name)
        if not key:
            return ""
        if key in available:
            return available[key]
        for title, url in available.items():
            norm_title = _norm_doc_key(title)
            if key in title or title in key or (norm_key and (norm_key in norm_title or norm_title in norm_key)):
                return url
        return ""

    for doc_name in required_docs:
        doc_kind, clean_name = _parse_required_doc_name(doc_name)
        if not clean_name:
            continue
        clean_key = clean_name.lower()
        has_data_duplicate = clean_key in existing_master or any(
            clean_key in k or k in clean_key for k in existing_master.keys()
        )
        if doc_kind == "Data" and has_data_duplicate:
            continue
        if doc_kind in {"Document", "Photo"} and _find_doc_value(doc_name) not in {""}:
            continue
        row_label = f"Required ({doc_kind}): {clean_name}"
        if row_label.strip().lower() in existing_labels:
            continue
        existing_labels.add(row_label.strip().lower())
    return required_docs


def _build_required_doc_rows(profile, required_docs, step_data=None, required_profile_fields=None):
    available = {}
    if getattr(profile, "photo_url", ""):
        available["passport photo"] = profile.photo_url
    elif getattr(profile, "photo", None):
        available["passport photo"] = profile.photo.url
    if getattr(profile, "signature_url", ""):
        available["signature"] = profile.signature_url
    elif getattr(profile, "signature", None):
        available["signature"] = profile.signature.url
    for doc in profile.documents.all():
        title = (doc.title or "").strip().lower()
        if title:
            available[title] = getattr(doc, "file_url", "") or (doc.file.url if getattr(doc, "file", None) else "")

    existing_master = {}
    for rows_in_step in (step_data or {}).values():
        if not isinstance(rows_in_step, list):
            continue
        for label, val in rows_in_step:
            key = str(label or "").strip().lower()
            if key and str(val or "").strip():
                existing_master[key] = str(val).strip()

    rows = []
    merged_items = list(required_docs or [])

    for idx, doc_name in enumerate(merged_items):
        doc_kind, clean_name = _parse_required_doc_name(doc_name)
        if not clean_name:
            continue
        # Apply page ke document section me sirf document/photo fields dikhane hain.
        if doc_kind == "Data":
            continue
        key = clean_name.lower()
        norm_key = _norm_doc_key(clean_name)
        value = available.get(key, "")
        if not value:
            for title, url in available.items():
                norm_title = _norm_doc_key(title)
                if key in title or title in key or (norm_key and (norm_key in norm_title or norm_title in norm_key)):
                    value = url
                    break
        if not value:
            value = ""
        rows.append(
            {
                "idx": idx,
                "label": clean_name,
                "kind": doc_kind,
                "value": value,
                "file_name": (value.rsplit("/", 1)[-1] if value else ""),
                "input_name": f"vacdoc__{idx}",
                "checkbox_name": f"vacdoc_select__{idx}",
                "file_input_name": f"vacdoc_file__{idx}",
                "checked": True,
                "exists": value not in {""},
            }
        )
    return rows


def _build_mobile_apply_pages(step_cards, required_doc_rows, page_size):
    safe_size = max(int(page_size or 6), 1)
    pages = []
    for step in step_cards:
        rows = list(step.get("rows") or [])
        if not rows:
            continue
        for start in range(0, len(rows), safe_size):
            chunk = rows[start:start + safe_size]
            suffix = ""
            if len(rows) > safe_size:
                suffix = f" ({start // safe_size + 1})"
            pages.append(
                {
                    "title": f"{step.get('label', 'Step')}{suffix}",
                    "kind": "step",
                    "rows": chunk,
                    "step_key": step.get("key", ""),
                }
            )
    doc_rows = list(required_doc_rows or [])
    for start in range(0, len(doc_rows), safe_size):
        chunk = doc_rows[start:start + safe_size]
        suffix = ""
        if len(doc_rows) > safe_size:
            suffix = f" ({start // safe_size + 1})"
        pages.append(
            {
                "title": f"Required Documents{suffix}",
                "kind": "documents",
                "rows": chunk,
                "step_key": "documents",
            }
        )
    pages.append({"title": "Review & Submit", "kind": "review", "rows": [], "step_key": "review"})
    return pages


def _demo_document_links(application):
    return [
        {
            "title": "Aadhaar Card (Demo)",
            "url": f"/admin-panel/applicants/{application.id}/documents/demo/aadhaar/",
        },
        {
            "title": "Marksheet (Demo)",
            "url": f"/admin-panel/applicants/{application.id}/documents/demo/marksheet/",
        },
    ]


def _collect_document_links(application):
    profile = application.profile
    docs = []
    if getattr(profile, "photo_url", ""):
        docs.append({"title": "Passport Photo", "url": profile.photo_url})
    elif getattr(profile, "photo", None):
        docs.append({"title": "Passport Photo", "url": profile.photo.url})
    if getattr(profile, "signature_url", ""):
        docs.append({"title": "Signature", "url": profile.signature_url})
    elif getattr(profile, "signature", None):
        docs.append({"title": "Signature", "url": profile.signature.url})
    docs.extend(
        [
            {"title": d.title or "Document", "url": (getattr(d, "file_url", "") or (d.file.url if getattr(d, "file", None) else ""))}
            for d in profile.documents.all()
        ]
    )
    return docs or _demo_document_links(application)


def _slug_name(value):
    raw = (value or "").strip().lower()
    out = []
    for ch in raw:
        if ch.isalnum():
            out.append(ch)
        elif ch in {" ", "-", "_"}:
            out.append("_")
    slug = "".join(out).strip("_")
    return slug or "applicant"


def _save_profile_document(profile, title, file_obj):
    if not file_obj:
        return ""
    clean_title = str(title or "Additional Document").strip() or "Additional Document"
    existing = profile.documents.filter(title=clean_title).first()
    if existing:
        existing.file = file_obj
        existing.save()
        return existing.file.url if existing.file else ""
    doc = UserDocument.objects.create(profile=profile, title=clean_title, file=file_obj)
    return doc.file.url if doc.file else ""


def _file_meta(title, file_field):
    try:
        size_bytes = int(file_field.size)
    except Exception:
        size_bytes = 0
    size_kb = round(size_bytes / 1024, 2) if size_bytes else 0
    dims = "N/A"
    mime = "file"
    try:
        file_field.open("rb")
        with Image.open(file_field) as img:
            dims = f"{img.width} x {img.height}"
            mime = "image"
    except Exception:
        name = (getattr(file_field, "name", "") or "").lower()
        if name.endswith(".pdf"):
            mime = "pdf"
    finally:
        try:
            file_field.close()
        except Exception:
            pass
    return {
        "title": title,
        "size_kb": size_kb,
        "dimensions": dims,
        "kind": mime,
    }


def _profile_document_meta(profile):
    rows = []
    if profile.photo:
        rows.append(_file_meta("Passport Photo", profile.photo))
    if profile.signature:
        rows.append(_file_meta("Signature", profile.signature))
    for d in profile.documents.all():
        rows.append(_file_meta(d.title or "Document", d.file))
    return rows


def _flatten_application_row(application):
    profile = application.profile
    return {
        "Application ID": application.id,
        "Applicant ID": profile.id,
        "Username": profile.user.username,
        "Full Name": profile.full_name,
        "Vacancy": application.vacancy.title,
        "Organization": application.vacancy.organization,
        "Status": _status_label(application.status),
        "Applied At": application.applied_at.strftime("%Y-%m-%d %H:%M"),
        "DOB": profile.dob.strftime("%Y-%m-%d") if profile.dob else "",
        "Gender": profile.get_gender_display() if profile.gender else "",
        "Category": profile.category,
        "Mobile": profile.mobile,
        "Email": profile.email,
        "Aadhaar": profile.aadhar,
        "Father Name": profile.father_name,
        "Mother Name": profile.mother_name,
        "Present Address": profile.present_address,
        "Present City": profile.present_city,
        "Present District": profile.present_district,
        "Present State": profile.present_state,
        "Present Pincode": profile.present_pincode,
        "Permanent Address": profile.permanent_full_address or profile.permanent_address,
        "Permanent District": profile.permanent_district,
        "Permanent State": profile.permanent_state,
        "Permanent Pincode": profile.permanent_pincode,
        "10th Board": profile.tenth_board,
        "10th Roll Number": profile.tenth_roll_number,
        "10th Percentage": profile.tenth_percentage,
        "12th Board": profile.twelfth_board,
        "12th Roll Number": profile.twelfth_roll_number,
        "12th Percentage": profile.twelfth_percentage,
        "Graduation": profile.graduation,
        "College Name": profile.college_name,
        "University": profile.university_name,
        "Course": profile.course,
        "Year/Semester": profile.year_semester,
        "Enrollment Number": profile.enrollment_number,
        "Card Name": profile.bank_name,
        "Account Holder": profile.account_holder_name,
        "Account Number": profile.account_number,
        "IFSC": profile.ifsc_code,
        "Branch": profile.branch_name,
        "Personal Extra Rows": _extra_rows_as_text(profile.personal_extra_rows),
        "Address Extra Rows": _extra_rows_as_text(profile.address_extra_rows),
        "Academic Extra Rows": _extra_rows_as_text(profile.academic_extra_rows),
        "College Extra Rows": _extra_rows_as_text(profile.college_extra_rows),
        "Card Extra Rows": _extra_rows_as_text(profile.bank_extra_rows),
    }


def _application_base_queryset():
    return Application.objects.select_related("profile__user", "vacancy").prefetch_related("profile__documents")


def _filtered_applications(q, status):
    qs = _application_base_queryset()
    if status and status != "all":
        qs = qs.filter(status=status)
    if q:
        q = q.strip()
        filters = (
            Q(profile__full_name__icontains=q)
            | Q(profile__user__username__icontains=q)
            | Q(profile__mobile__icontains=q)
        )
        if q.isdigit():
            filters |= Q(id=int(q)) | Q(profile__id=int(q))
        qs = qs.filter(filters)
    return qs.order_by("applied_at", "id")


@login_required
def student_services_dashboard(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    _seed_default_vacancies()
    services = [
        item
        for item in Vacancy.objects.filter(category=Vacancy.CATEGORY_STUDENT).order_by(
            "display_order", "last_date", "id"
        )
        if _vacancy_visible_to_profile(item, profile)
    ]
    user_apps = Application.objects.filter(profile=profile).select_related("vacancy")
    application_map = {app.vacancy_id: app for app in user_apps}
    service_cards = []
    for service in services:
        service_cards.append(
            {
                "vacancy": service,
                "application": application_map.get(service.id),
            }
        )
    my_applications = (
        user_apps.filter(vacancy__category=Vacancy.CATEGORY_STUDENT)
        .order_by("-applied_at")
    )
    news_items = _news_for_portal(PortalNews.TARGET_STUDENT)
    return render(
        request,
        "portal_main/student_services.html",
        {
            "profile": profile,
            "service_cards": service_cards,
            "my_applications": my_applications,
            "news_items": news_items,
            "is_admin_user": _can_access_admin(request),
        },
    )


@login_required
def apply_student_service(request, vacancy_id):
    if request.method != "POST":
        return redirect("student_services_dashboard")

    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    vacancy = get_object_or_404(
        Vacancy,
        id=vacancy_id,
        category=Vacancy.CATEGORY_STUDENT,
    )
    if not _vacancy_visible_to_profile(vacancy, profile):
        messages.error(request, "Ye student option is user ke liye active nahi hai.")
        return redirect("student_services_dashboard")
    if not vacancy.is_active:
        messages.error(request, "Ye student form abhi deactivated hai.")
        return redirect("student_services_dashboard")
    profile_draft = _get_profile_draft(profile, vacancy.id)
    started_at = str(profile_draft.get("started_at", "")).strip() or timezone.now().isoformat()

    request.session["pending_form_apply"] = {
        "kind": "student",
        "vacancy_id": vacancy.id,
        "title": vacancy.title,
        "organization": vacancy.organization,
        "started_at": started_at,
        "draft_payload": profile_draft.get("draft_payload", {}) if isinstance(profile_draft, dict) else {},
        "draft_vacancy_docs": profile_draft.get("draft_vacancy_docs", []) if isinstance(profile_draft, dict) else [],
    }
    return redirect("confirm_send_to_admin")


@login_required
def dashboard(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)

    _seed_default_vacancies()
    vacancies = [
        item
        for item in Vacancy.objects.filter(category=Vacancy.CATEGORY_GOVERNMENT).order_by(
            "display_order", "last_date", "id"
        )
        if _vacancy_visible_to_profile(item, profile)
    ]
    user_apps = Application.objects.filter(profile=profile).select_related("vacancy")
    application_map = {app.vacancy_id: app for app in user_apps}
    vacancy_cards = []
    for vacancy in vacancies:
        vacancy_cards.append({"vacancy": vacancy, "application": application_map.get(vacancy.id)})
    my_applications = (
        user_apps.filter(vacancy__category=Vacancy.CATEGORY_GOVERNMENT)
        .order_by("-applied_at")
    )
    news_items = _news_for_portal(PortalNews.TARGET_GOVERNMENT)

    return render(
        request,
        "portal_main/dashboard.html",
        {
            "profile": profile,
            "vacancy_cards": vacancy_cards,
            "my_applications": my_applications,
            "news_items": news_items,
            "is_admin_user": _can_access_admin(request),
        },
    )


@login_required
def news_hub(request):
    portal = request.GET.get("portal", "all").strip().lower()
    if portal not in {"all", PortalNews.TARGET_GOVERNMENT, PortalNews.TARGET_STUDENT}:
        portal = "all"
    category = request.GET.get("category", "all").strip().lower()
    if category not in {"all", "recruitments", "exams"}:
        category = "all"

    headline_items = _portal_news_queryset(portal)
    if category == "recruitments":
        headline_items = headline_items.filter(news_type=PortalNews.TYPE_VACANCY)
    elif category == "exams":
        headline_items = headline_items.filter(news_type=PortalNews.TYPE_RESULT)

    featured_item = _portal_news_queryset(portal).first()
    return render(
        request,
        "portal_main/news_hub.html",
        {
            "portal": portal,
            "category": category,
            "headline_items": headline_items,
            "featured_item": featured_item,
        },
    )


@login_required
def news_detail(request, news_id):
    portal = request.GET.get("portal", "all").strip().lower()
    if portal not in {"all", PortalNews.TARGET_GOVERNMENT, PortalNews.TARGET_STUDENT}:
        portal = "all"
    news_item = get_object_or_404(_portal_news_queryset(portal), id=news_id)
    related = _portal_news_queryset(portal).exclude(id=news_item.id)[:10]
    return render(
        request,
        "portal_main/news_detail.html",
        {
            "portal": portal,
            "news_item": news_item,
            "related": related,
        },
    )


@login_required
def apply_vacancy(request, vacancy_id):
    if request.method != "POST":
        return redirect("dashboard")

    profile = get_object_or_404(UserProfile, user=request.user)
    vacancy = get_object_or_404(
        Vacancy,
        id=vacancy_id,
        category=Vacancy.CATEGORY_GOVERNMENT,
    )
    if not _vacancy_visible_to_profile(vacancy, profile):
        messages.error(request, "Ye vacancy is user ke liye active nahi hai.")
        return redirect("dashboard")
    if not vacancy.is_active:
        messages.error(request, "Ye vacancy abhi deactivated hai.")
        return redirect("dashboard")
    profile_draft = _get_profile_draft(profile, vacancy.id)
    started_at = str(profile_draft.get("started_at", "")).strip() or timezone.now().isoformat()
    request.session["pending_form_apply"] = {
        "kind": "government",
        "vacancy_id": vacancy.id,
        "title": vacancy.title,
        "organization": vacancy.organization,
        "started_at": started_at,
        "draft_payload": profile_draft.get("draft_payload", {}) if isinstance(profile_draft, dict) else {},
        "draft_vacancy_docs": profile_draft.get("draft_vacancy_docs", []) if isinstance(profile_draft, dict) else [],
    }
    return redirect("confirm_send_to_admin")


@login_required
def confirm_send_to_admin(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    pending = request.session.get("pending_form_apply")
    if not pending:
        messages.info(request, "Pehle koi form select karke Apply click karo.")
        return redirect("role_select")
    # Autofill lock/masking: user request -> disable for now.
    lock_until = None
    lock_active = False
    timed_out_active = False
    vacancy = get_object_or_404(Vacancy, id=pending.get("vacancy_id"))
    if not vacancy.is_active:
        request.session.pop("pending_form_apply", None)
        messages.error(request, "Ye vacancy abhi deactivated hai.")
        if vacancy.category == Vacancy.CATEGORY_STUDENT:
            return redirect("student_services_dashboard")
        return redirect("dashboard")
    if not _vacancy_visible_to_profile(vacancy, profile):
        request.session.pop("pending_form_apply", None)
        messages.error(request, "Ye vacancy ab is user ke liye available nahi hai.")
        return redirect("role_select")
    profile_draft = _get_profile_draft(profile, vacancy.id)
    payment_setting = _active_payment_setting()
    payment_upi_link = _upi_deep_link(payment_setting)
    draft_payload = pending.get("draft_payload") if isinstance(pending, dict) else {}
    if not isinstance(draft_payload, dict):
        draft_payload = {}
    draft_vac_docs = pending.get("draft_vacancy_docs") if isinstance(pending, dict) else []
    if not isinstance(draft_vac_docs, list):
        draft_vac_docs = []
    if not draft_payload and isinstance(profile_draft, dict):
        prof_payload = profile_draft.get("draft_payload", {})
        if isinstance(prof_payload, dict):
            draft_payload = prof_payload
    if not draft_vac_docs and isinstance(profile_draft, dict):
        prof_docs = profile_draft.get("draft_vacancy_docs", [])
        if isinstance(prof_docs, list):
            draft_vac_docs = prof_docs
    if isinstance(profile_draft, dict) and not pending.get("started_at") and profile_draft.get("started_at"):
        pending["started_at"] = profile_draft.get("started_at")
        request.session["pending_form_apply"] = pending

    if request.method == "POST":
        submit_mode = request.POST.get("submit_mode", "skip")
        selected_steps = []
        if submit_mode not in {"save_only", "save_master"}:
            consent_1 = request.POST.get("consent_data_usage") == "1"
            consent_2 = request.POST.get("consent_user_responsibility") == "1"
            if not (consent_1 and consent_2):
                messages.error(request, "Form send karne se pehle dono disclaimer tick karna zaroori hai.")
                return redirect("confirm_send_to_admin")

        submission = _extract_apply_submission(request, profile, vacancy)
        payload = submission["payload"]
        selected_steps = submission["selected_steps"]
        selected_vac_docs = submission["selected_vac_docs"]
        if submit_mode == "save_only":
            pending["draft_payload"] = payload
            pending["draft_vacancy_docs"] = selected_vac_docs
            pending["last_edit_at"] = timezone.now().isoformat()
            request.session["pending_form_apply"] = pending
            _save_profile_draft(profile, vacancy.id, payload, selected_vac_docs, pending.get("started_at", ""))
            messages.success(request, "Apply page data + uploaded docs save ho gaye. Ab View Profile me check kar sakte ho.")
            return redirect("confirm_send_to_admin")
        if submit_mode == "save_master":
            pending["draft_payload"] = payload
            pending["draft_vacancy_docs"] = selected_vac_docs
            pending["last_edit_at"] = timezone.now().isoformat()
            request.session["pending_form_apply"] = pending
            _save_profile_draft(profile, vacancy.id, payload, selected_vac_docs, pending.get("started_at", ""))
            saved_count = _save_payload_to_master_data(profile, payload)
            messages.success(
                request,
                f"Apply data master data me save ho gaya. Updated sections: {saved_count}. Purana existing data overwrite nahi hua.",
            )
            return redirect("confirm_send_to_admin")
        if selected_vac_docs:
            payload["vacancy_required_documents"] = selected_vac_docs
        if not payload and not selected_vac_docs:
            messages.error(request, "Koi data available nahi hai. Admin panel se vacancy ke liye fields/docs add karo.")
            return redirect("confirm_send_to_admin")
        app, created = _persist_application_submission(profile, vacancy, payload, selected_steps, selected_vac_docs)

        _clear_profile_draft(profile, vacancy.id)
        request.session.pop("pending_form_apply", None)
        if pending.get("kind") == "student":
            if submit_mode == "pay":
                messages.success(request, "Payment attempt ke saath student form data admin ko send kar diya gaya.")
            else:
                messages.success(request, "Student form data admin ko send kar diya gaya.")
            return redirect("student_services_dashboard")

        if created:
            if submit_mode == "pay":
                messages.success(request, "Payment attempt ke saath government form data admin ko send kar diya gaya.")
            else:
                messages.success(request, "Government form data admin ko send kar diya gaya.")
        else:
            if submit_mode == "pay":
                messages.success(request, "Payment attempt ke saath government form request update karke resend kar diya gaya.")
            else:
                messages.success(request, "Government form request update karke admin ko resend kar diya gaya.")
        return redirect("dashboard")

    step_data = _profile_step_data(profile)
    # masking disabled
    required_profile_fields = [
        item for item in (vacancy.required_profile_fields or [])
        if str(item or "").strip()
    ]
    required_docs = _inject_required_docs_rows(profile, vacancy, step_data)
    required_doc_rows = _build_required_doc_rows(
        profile,
        required_docs,
        step_data=step_data,
        required_profile_fields=required_profile_fields,
    )
    if draft_vac_docs:
        draft_doc_map = {}
        for item in draft_vac_docs:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "")).strip().lower()
            if not label:
                continue
            draft_doc_map[label] = str(item.get("value", "")).strip()
        for row in required_doc_rows:
            key = str(row.get("label", "")).strip().lower()
            if key in draft_doc_map and draft_doc_map[key]:
                row["value"] = draft_doc_map[key]
                row["exists"] = row["value"] not in {"", "Not uploaded yet"}

    requested_profile_rows = _build_requested_profile_rows(step_data, required_profile_fields)
    has_vacancy_specific_setup = bool(required_profile_fields or required_docs)
    use_requested_only = has_vacancy_specific_setup
    use_draft_payload = bool(draft_payload) and _payload_has_any_values(draft_payload)
    selected_default = []
    step_cards = []
    for key, label in PROFILE_DATA_STEPS:
        if use_requested_only and key != "documents":
            rows = requested_profile_rows.get(key, [])
        else:
            rows = step_data.get(key, [])
        if use_requested_only and key == "documents":
            rows = []
        if use_draft_payload:
            rows = _rows_from_payload(draft_payload, key, rows)
        row_items = []
        for idx, row in enumerate(rows):
            row_label = row[0]
            row_value = row[1] or ""
            row_help = row[2] if len(row) > 2 else ""
            should_check = True
            row_items.append(
                {
                    "label": row_label[8:] if str(row_label).startswith("COMMENT|") else row_label,
                    "value": row_value,
                    "help_text": row_help,
                    "input_name": f"field__{key}__{idx}",
                    "checkbox_name": f"select__{key}__{idx}",
                    "checked": should_check,
                    "is_comment": str(row_label).startswith("COMMENT|"),
                }
            )
        step_cards.append(
            {
                "key": key,
                "label": label,
                "rows": row_items,
            }
        )
        if row_items:
            selected_default.append(key)
    mobile_page_size = max(int(getattr(vacancy, "mobile_page_size", 6) or 6), 1)
    mobile_pages = _build_mobile_apply_pages(step_cards, required_doc_rows, mobile_page_size)
    return render(
        request,
        "portal_main/confirm_send_to_admin.html",
        {
            "profile": profile,
            "pending": pending,
            "vacancy": vacancy,
            "steps": PROFILE_DATA_STEPS,
            "step_cards": step_cards,
            "selected_default": selected_default,
            "required_doc_rows": required_doc_rows,
            "payment_setting": payment_setting,
            "payment_upi_link": payment_upi_link,
            "cashfree_enabled": _cashfree_enabled(),
            "cashfree_mode": getattr(settings, "CASHFREE_MODE", "sandbox"),
            "mobile_page_size": mobile_page_size,
            "mobile_pages": mobile_pages,
            "use_requested_only": use_requested_only,
            # kept for backward compat if templates reference these keys in future
            "autofill_lock_active": False,
            "autofill_lock_until": None,
            "apply_timed_out": False,
            "apply_timeout_minutes": APPLY_PENDING_TIMEOUT_MINUTES,
        },
    )


@login_required
def cashfree_create_order(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)
    pending = request.session.get("pending_form_apply")
    if not pending:
        return JsonResponse({"error": "Pehle koi vacancy select karo."}, status=400)
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    vacancy = get_object_or_404(Vacancy, id=pending.get("vacancy_id"))
    if not vacancy.is_active:
        request.session.pop("pending_form_apply", None)
        return JsonResponse({"error": "Ye vacancy abhi deactivated hai."}, status=400)
    if not _vacancy_visible_to_profile(vacancy, profile):
        return JsonResponse({"error": "Ye vacancy is user ke liye available nahi hai."}, status=403)
    if not _cashfree_enabled():
        return JsonResponse({"error": "Cashfree sandbox config set nahi hai."}, status=500)

    consent_1 = request.POST.get("consent_data_usage") == "1"
    consent_2 = request.POST.get("consent_user_responsibility") == "1"
    if not (consent_1 and consent_2):
        return JsonResponse({"error": "Payment se pehle dono disclaimer tick karna zaroori hai."}, status=400)

    submission = _extract_apply_submission(request, profile, vacancy)
    payload = submission["payload"]
    selected_steps = submission["selected_steps"]
    selected_vac_docs = submission["selected_vac_docs"]
    if not payload and not selected_vac_docs:
        return JsonResponse({"error": "Payment se pehle kam se kam required data select karo."}, status=400)

    mode = request.POST.get("payment_mode", "pay").strip().lower()
    if mode not in {"pay", "pay_and_apply"}:
        mode = "pay"

    setting = _active_payment_setting()
    try:
        order_data = _cashfree_build_order(request, profile, vacancy, setting)
    except RuntimeError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    payment_session_id = str(order_data.get("payment_session_id", "")).strip()
    order_id = str(order_data.get("order_id", "")).strip()
    if not payment_session_id or not order_id:
        return JsonResponse({"error": "Cashfree se valid payment session nahi mila."}, status=500)

    pending["draft_payload"] = payload
    pending["draft_vacancy_docs"] = selected_vac_docs
    pending["last_edit_at"] = timezone.now().isoformat()
    request.session["pending_form_apply"] = pending
    _save_profile_draft(profile, vacancy.id, payload, selected_vac_docs, pending.get("started_at", ""))
    request.session["cashfree_pending_payment"] = {
        "order_id": order_id,
        "payment_mode": mode,
        "vacancy_id": vacancy.id,
        "kind": pending.get("kind", ""),
        "payload": payload,
        "selected_steps": selected_steps,
        "selected_vac_docs": selected_vac_docs,
    }
    request.session.modified = True
    return JsonResponse(
        {
            "ok": True,
            "payment_session_id": payment_session_id,
            "order_id": order_id,
            "mode": getattr(settings, "CASHFREE_MODE", "sandbox"),
        }
    )


@login_required
def cashfree_return(request):
    order_id = str(request.GET.get("order_id", "")).strip()
    if not order_id:
        messages.error(request, "Cashfree order id missing hai.")
        return redirect("confirm_send_to_admin")
    session_data = request.session.get("cashfree_pending_payment") or {}
    if session_data and session_data.get("order_id") != order_id:
        messages.error(request, "Cashfree payment session mismatch hai.")
        return redirect("confirm_send_to_admin")
    try:
        order_data = _cashfree_api_request("GET", f"/orders/{order_id}")
    except RuntimeError as exc:
        messages.error(request, str(exc))
        return redirect("confirm_send_to_admin")

    payment_status = str(order_data.get("order_status", "")).upper()
    if payment_status != "PAID":
        messages.warning(request, f"Payment status abhi {payment_status or 'UNKNOWN'} hai. Try again ya thodi der baad check karo.")
        return redirect("confirm_send_to_admin")

    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    vacancy = get_object_or_404(Vacancy, id=session_data.get("vacancy_id"))
    payload = session_data.get("payload") if isinstance(session_data.get("payload"), dict) else {}
    selected_steps = session_data.get("selected_steps") if isinstance(session_data.get("selected_steps"), list) else []
    selected_vac_docs = session_data.get("selected_vac_docs") if isinstance(session_data.get("selected_vac_docs"), list) else []
    payment_mode = session_data.get("payment_mode", "pay")

    if payment_mode == "pay_and_apply":
        _persist_application_submission(profile, vacancy, payload, selected_steps, selected_vac_docs)
        _clear_profile_draft(profile, vacancy.id)
        request.session.pop("pending_form_apply", None)
        request.session.pop("cashfree_pending_payment", None)
        messages.success(request, "Payment successful raha aur form admin ko send ho gaya.")
        if session_data.get("kind") == "student":
            return redirect("student_services_dashboard")
        return redirect("dashboard")

    request.session.pop("cashfree_pending_payment", None)
    messages.success(request, "Payment successful raha. Ab chahe to Apply Changes Send To Admin bhi kar sakte ho.")
    return redirect("confirm_send_to_admin")


@login_required
def apply_profile_preview(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    pending = request.session.get("pending_form_apply") or {}
    vacancy = None
    if pending.get("vacancy_id"):
        vacancy = Vacancy.objects.filter(id=pending.get("vacancy_id")).first()
    if vacancy and not vacancy.is_active:
        request.session.pop("pending_form_apply", None)
        messages.error(request, "Ye vacancy abhi deactivated hai.")
        if vacancy.category == Vacancy.CATEGORY_STUDENT:
            return redirect("student_services_dashboard")
        return redirect("dashboard")
    # Autofill lock/masking disabled for now.
    timed_out_active = False
    lock_until = None

    if request.method == "POST" and request.POST.get("action") == "unlock_apply_profile":
        granted, remaining = _grant_apply_profile_unmask(profile)
        if granted:
            messages.success(
                request,
                f"Apply profile full view 10 minute ke liye unlock ho gaya. Aaj remaining unlock: {remaining}.",
            )
        else:
            messages.error(request, "Aaj ka 2-time full-view limit complete ho gaya.")
        return redirect("apply_profile_preview")

    allowed_view, remaining_views = _register_apply_profile_view(profile)
    if not allowed_view:
        messages.error(request, "Aaj ka apply profile view limit (10) complete ho gaya.")
        return redirect("confirm_send_to_admin")

    all_step_data = _profile_step_data(profile)

    payload = {}
    draft_payload = pending.get("draft_payload") if isinstance(pending, dict) else {}
    if not isinstance(draft_payload, dict):
        draft_payload = {}
    if vacancy:
        existing_app = (
            Application.objects.filter(profile=profile, vacancy=vacancy)
            .order_by("-updated_at", "-id")
            .first()
        )
        if existing_app:
            payload = _extract_payload_from_remarks(existing_app.remarks)

    step_data = {key: [] for key, _ in PROFILE_DATA_STEPS if key != "documents"}
    active_payload = draft_payload if draft_payload else payload
    if active_payload:
        for key in step_data.keys():
            step_data[key] = _rows_from_payload(active_payload, key, [])
    else:
        step_data = dict(all_step_data)

    required_doc_rows = []
    if vacancy:
        base_step_data = dict(step_data)
        required_docs = _inject_required_docs_rows(profile, vacancy, base_step_data)
        required_doc_rows = _build_required_doc_rows(
            profile,
            required_docs,
            step_data=base_step_data,
            required_profile_fields=vacancy.required_profile_fields,
        )
        draft_vac_docs = pending.get("draft_vacancy_docs") if isinstance(pending, dict) else []
        if isinstance(draft_vac_docs, list):
            draft_map = {}
            for item in draft_vac_docs:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label", "")).strip().lower()
                value = str(item.get("value", "")).strip()
                if label and value:
                    draft_map[label] = value
            for row in required_doc_rows:
                rkey = str(row.get("label", "")).strip().lower()
                if rkey in draft_map:
                    row["value"] = draft_map[rkey]
                    row["exists"] = row["value"] not in {"", "Not uploaded yet"}

    unmask_active = _is_apply_profile_unmask_active(profile)
    mask_for_preview = bool(timed_out_active and not unmask_active)
    if mask_for_preview:
        step_data = _mask_step_rows(step_data)

    document_links = []
    seen_urls = set()

    def _push(title, url):
        if not url:
            return
        if url in seen_urls:
            return
        seen_urls.add(url)
        kind = "image" if _is_probable_image_url(title, url) else "file"
        document_links.append(
            {
                "title": title,
                "url": url,
                "kind": kind,
            }
        )

    def _resolve_value_url(value):
        raw = str(value or "").strip()
        if not raw or raw.lower() == "not uploaded yet":
            return ""
        if raw.startswith("/media/") or raw.startswith("http://") or raw.startswith("https://"):
            return raw
        return ""

    if not mask_for_preview:
        active_doc_items = []
        if isinstance(draft_payload, dict) and isinstance(draft_payload.get("vacancy_required_documents"), list):
            active_doc_items = draft_payload.get("vacancy_required_documents", [])
        elif isinstance(pending, dict) and isinstance(pending.get("draft_vacancy_docs"), list) and pending.get("draft_vacancy_docs"):
            active_doc_items = pending.get("draft_vacancy_docs", [])
        elif payload and isinstance(payload.get("vacancy_required_documents"), list):
            active_doc_items = payload.get("vacancy_required_documents", [])

        for item in active_doc_items:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "Document").strip()
            url = _resolve_value_url(item.get("value"))
            if url:
                _push(label, url)

        if not active_doc_items and not active_payload:
            for row in required_doc_rows:
                url = _resolve_value_url(row.get("value"))
                if not url:
                    continue
                _push(row.get("label") or "Document", url)

        if not active_doc_items and payload and isinstance(payload.get("vacancy_required_documents"), list):
            for item in payload.get("vacancy_required_documents", []):
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or "Document").strip()
                url = _resolve_value_url(item.get("value"))
                if url:
                    _push(label, url)

        if not document_links and not active_payload:
            photo_url = _safe_file_url(profile.photo)
            sign_url = _safe_file_url(profile.signature)
            _push("Passport Photo", photo_url)
            _push("Signature", sign_url)
            for doc in profile.documents.all():
                url = _safe_file_url(doc.file)
                if not url:
                    continue
                _push(doc.title or "Document", url)

    step_data = {key: rows for key, rows in step_data.items() if rows}

    return render(
        request,
        "portal_main/apply_profile_preview.html",
        {
            "profile": profile,
            "step_data": step_data,
            "document_links": document_links,
            "vacancy": vacancy,
            "apply_timed_out": timed_out_active,
            "mask_for_preview": mask_for_preview,
            "remaining_profile_views": remaining_views,
            "unmask_active": unmask_active,
            "unmask_until": profile.apply_profile_unmask_until,
            "unmask_remaining_today": max(
                APPLY_PROFILE_UNMASK_DAILY_LIMIT
                - (
                    profile.apply_profile_unmask_count
                    if profile.apply_profile_unmask_date == timezone.localdate()
                    else 0
                ),
                0,
            ),
        },
    )


@login_required
def admin_payment(request):
    if not _can_access_admin(request):
        messages.error(request, "Admin panel access allowed nahi hai.")
        return redirect("dashboard")

    setting = _active_payment_setting()
    if request.method == "POST":
        upi_id = request.POST.get("upi_id", "").strip()
        payee_name = request.POST.get("payee_name", "").strip()
        note = request.POST.get("note", "").strip()
        amount_raw = request.POST.get("amount", "0").strip()
        is_active = request.POST.get("is_active") == "on"
        try:
            amount = Decimal(amount_raw or "0")
            if amount < 0:
                amount = Decimal("0")
        except (InvalidOperation, ValueError):
            amount = Decimal("0")

        try:
            if not setting:
                setting = PaymentSetting()
            setting.upi_id = upi_id
            setting.payee_name = payee_name
            setting.note = note
            setting.amount = amount
            setting.is_active = is_active
            if request.FILES.get("qr_image"):
                setting.qr_image = request.FILES["qr_image"]
            if request.POST.get("clear_qr") == "on":
                setting.qr_image = None
            setting.save()
            messages.success(request, "Payment settings update ho gayi.")
        except (OperationalError, ProgrammingError):
            messages.error(request, "Payment table ready nahi hai. `manage.py migrate` run karo.")
        return redirect("admin_payment")

    return render(
        request,
        "portal_main/admin_payment.html",
        {
            "setting": setting,
            "upi_link": _upi_deep_link(setting),
            "cashfree_enabled": _cashfree_enabled(),
            "cashfree_mode": getattr(settings, "CASHFREE_MODE", "sandbox"),
            "cashfree_amount": _cashfree_amount(setting),
        },
    )


@login_required
def cancel_own_application(request, application_id):
    if request.method != "POST":
        return redirect("dashboard")
    profile = get_object_or_404(UserProfile, user=request.user)
    app = get_object_or_404(Application, id=application_id, profile=profile)
    app.status = Application.STATUS_CANCELLED
    app.cancelled_at = timezone.now()
    app.save(update_fields=["status", "cancelled_at", "updated_at"])
    messages.warning(request, "Application request cancel kar di gayi.")
    source = request.POST.get("source", "government")
    if source == "student":
        return redirect("student_services_dashboard")
    return redirect("dashboard")


def firebase_messaging_sw(request):
    sw_path = finders.find("firebase-messaging-sw.js")
    if not sw_path:
        raise Http404("firebase-messaging-sw.js not found")
    with open(sw_path, "rb") as f:
        content = f.read()
    resp = HttpResponse(content, content_type="application/javascript")
    resp["Cache-Control"] = "no-store"
    return resp


def save_fcm_token(request):
    if request.method != "POST":
        return JsonResponse({"status": "failed"}, status=400)
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "failed", "message": "Invalid JSON"}, status=400)

    token_val = (data.get("token") or "").strip()
    if not token_val:
        return JsonResponse({"status": "failed", "message": "Token missing"}, status=400)

    user = request.user if request.user.is_authenticated else None
    username = user.username if user else "Guest"
    if user and hasattr(user, "profile") and user.profile.full_name:
        username = user.profile.full_name

    UserToken.objects.update_or_create(
        token=token_val,
        defaults={
            "user": user,
            "username": username,
            "user_agent": request.META.get("HTTP_USER_AGENT", "")[:300],
            "is_active": True,
        },
    )
    return JsonResponse({"status": "success"})

def notify_chat(request):
    if request.method != "POST":
        return JsonResponse({"status": "failed"}, status=400)
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        data = request.POST

    receiver_id = data.get("receiver_id", "").strip()
    message = data.get("message", "Naya message aaya hai!").strip()
    sender_name = data.get("sender_name", "User").strip()

    if not receiver_id:
        return JsonResponse({"status": "failed", "error": "Receiver missing"}, status=400)

    if receiver_id.lower() == "admin":
        tokens = list(UserToken.objects.filter(user__is_staff=True, is_active=True).values_list("token", flat=True).distinct())
    else:
        tokens = list(UserToken.objects.filter(username=receiver_id, is_active=True).values_list("token", flat=True).distinct())

    if tokens:
        _send_push_to_tokens(f"New Message from {sender_name}", message, tokens)

    return JsonResponse({"status": "sent", "tokens_notified": len(tokens)})

def _firebase_admin_app():
    try:
        import firebase_admin
        from firebase_admin import credentials
    except Exception as exc:
        return None, f"firebase-admin install nahi hai: {exc}"

    try:
        return firebase_admin.get_app(), ""
    except ValueError:
        pass

    service_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    service_file = os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE", "").strip()

    if not service_file and not service_json:
        from django.conf import settings
        default_path = settings.BASE_DIR / "firebase" / "service-account.json"
        if default_path.exists():
            service_file = str(default_path)

    try:
        if service_json:
            return firebase_admin.initialize_app(credentials.Certificate(json.loads(service_json))), ""
        if service_file:
            return firebase_admin.initialize_app(credentials.Certificate(service_file)), ""
        return firebase_admin.initialize_app(), ""
    except Exception as exc:
        return None, f"Server par Firebase service account set hona zaroori hai. Error: {exc}"


def _send_push_to_tokens(title, body, tokens):
    app, init_error = _firebase_admin_app()
    if not app:
        return 0, len(tokens), f"Firebase Admin credential set nahi hai: {init_error}"

    try:
        from firebase_admin import messaging
    except Exception as exc:
        return 0, len(tokens), f"Firebase messaging load nahi hua: {exc}"

    success_count = 0
    failure_count = 0
    errors = []
    for start in range(0, len(tokens), 500):
        batch = tokens[start:start + 500]
        msg = messaging.MulticastMessage(
            notification=messaging.Notification(title=title, body=body),
            webpush=messaging.WebpushConfig(
                notification=messaging.WebpushNotification(
                    title=title,
                    body=body,
                    icon="/static/icons/icon-192.png",
                )
            ),
            tokens=batch,
        )
        try:
            if hasattr(messaging, "send_each_for_multicast"):
                resp = messaging.send_each_for_multicast(msg)
            else:
                resp = messaging.send_multicast(msg)
            success_count += getattr(resp, "success_count", 0)
            failure_count += getattr(resp, "failure_count", 0)
        except Exception as exc:
            failure_count += len(batch)
            errors.append(str(exc))
    return success_count, failure_count, " | ".join(errors[:3])


@login_required
def admin_notifications(request):
    if not _can_access_admin(request):
        messages.error(request, "Admin panel access allowed nahi hai.")
        return redirect("dashboard")

    tokens_qs = UserToken.objects.filter(is_active=True).exclude(token="")
    if request.method == "POST":
        title = (request.POST.get("title") or "").strip()
        body = (request.POST.get("body") or "").strip()
        if not title or not body:
            messages.error(request, "Title aur message dono bharna zaroori hai.")
            return redirect("admin_notifications")

        tokens = list(tokens_qs.values_list("token", flat=True).distinct())
        success_count, failure_count, error_text = _send_push_to_tokens(title, body, tokens)
        PushNotificationLog.objects.create(
            title=title,
            body=body,
            target_count=len(tokens),
            success_count=success_count,
            failure_count=failure_count,
            error_message=error_text,
            sent_by=request.user.username,
        )
        if success_count:
            messages.success(request, f"{success_count} notification sent ho gaya.")
        if failure_count or error_text:
            messages.error(request, error_text or f"{failure_count} notification fail hua.")
        return redirect("admin_notifications")

    return render(
        request,
        "portal_main/admin_notifications.html",
        {
            "token_count": tokens_qs.count(),
            "recent_logs": PushNotificationLog.objects.all()[:12],
            "is_admin_user": True,
        },
    )


@login_required
def admin_applicants(request):
    if not _can_access_admin(request):
        messages.error(request, "Admin panel access allowed nahi hai.")
        return redirect("dashboard")

    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "all").strip() or "all"
    applications = _filtered_applications(query, status)
    for app in applications:
        app.document_links = _collect_document_links(app)
        payload = _extract_payload_from_remarks(app.remarks)
        app.display_full_name = (
            _payload_first_value(payload, "Full Name", "Student Name", "Applicant Name", "Name")
            or app.profile.full_name
            or app.profile.user.username
        )
        app.display_dob = _payload_first_value(payload, "Date Of Birth", "DOB") or (
            app.profile.dob.strftime("%Y-%m-%d") if app.profile.dob else ""
        )
        app.display_gender = _payload_first_value(payload, "Gender") or app.profile.get_gender_display()
        app.display_category = _payload_first_value(payload, "Category", "Category/Caste", "Caste") or app.profile.category
        app.display_mobile = _payload_first_value(payload, "Mobile Number", "Mobile", "Contact Info", "Mobile No") or app.profile.mobile
    history_rows = list(ApplicationHistory.objects.all()[:120])

    context = {
        "applications": applications,
        "history_rows": history_rows,
        "query": query,
        "status": status,
        "status_choices": [("all", "All")] + list(Application.STATUS_CHOICES),
        "is_admin_user": True,
    }
    return render(request, "portal_main/admin_applicants.html", context)


@login_required
def admin_option_control(request, category):
    if not _can_access_admin(request):
        messages.error(request, "Admin panel access allowed nahi hai.")
        return redirect("dashboard")
    if category not in {Vacancy.CATEGORY_GOVERNMENT, Vacancy.CATEGORY_STUDENT}:
        return redirect("admin_option_control", category=Vacancy.CATEGORY_GOVERNMENT)

    options = Vacancy.objects.filter(category=category).order_by("display_order", "last_date", "id")
    for opt in options:
        opt.field_rows = [_parse_profile_field_entry(item) for item in (opt.required_profile_fields or [])]
    return render(
        request,
        "portal_main/admin_option_control.html",
        {
            "category": category,
            "options": options,
            "is_admin_user": True,
        },
    )


@login_required
def admin_master_data_control(request):
    if not _can_access_admin(request):
        messages.error(request, "Admin panel access allowed nahi hai.")
        return redirect("dashboard")

    try:
        if request.method == "POST":
            action = request.POST.get("action", "").strip()
            if action == "add":
                step = request.POST.get("step", "").strip()
                field_kind = request.POST.get("field_kind", "").strip()
                label = request.POST.get("label", "").strip()
                try:
                    display_order = max(int(request.POST.get("display_order", "0") or "0"), 0)
                except ValueError:
                    display_order = 0
                is_active = request.POST.get("is_active") == "on"

                valid_steps = {value for value, _ in MasterDataField.STEP_CHOICES}
                valid_kinds = {value for value, _ in MasterDataField.KIND_CHOICES}
                if step not in valid_steps or field_kind not in valid_kinds or not label:
                    messages.error(request, "Step, type aur label required hai.")
                    return redirect("admin_master_data_control")

                duplicate = MasterDataField.objects.filter(
                    step=step,
                    field_kind=field_kind,
                    label__iexact=label,
                ).exists()
                if duplicate:
                    messages.warning(request, "Same label already exists is step me.")
                    return redirect("admin_master_data_control")

                MasterDataField.objects.create(
                    step=step,
                    field_kind=field_kind,
                    label=label,
                    display_order=display_order,
                    is_active=is_active,
                )
                messages.success(request, "Master data row add ho gayi.")
                return redirect("admin_master_data_control")

            if action == "delete":
                field_id = request.POST.get("field_id", "").strip()
                field = get_object_or_404(MasterDataField, id=field_id)
                field.delete()
                if _is_ajax_request(request):
                    return JsonResponse({"ok": True, "action": "delete", "field_id": int(field_id)})
                messages.success(request, "Master data row remove ho gayi.")
                return redirect("admin_master_data_control")

            if action == "toggle":
                field_id = request.POST.get("field_id", "").strip()
                field = get_object_or_404(MasterDataField, id=field_id)
                field.is_active = not field.is_active
                field.save(update_fields=["is_active"])
                if _is_ajax_request(request):
                    return JsonResponse(
                        {
                            "ok": True,
                            "action": "toggle",
                            "field_id": int(field_id),
                            "is_active": field.is_active,
                        }
                    )
                messages.success(request, "Master data row status update ho gaya.")
                return redirect("admin_master_data_control")

        fields = MasterDataField.objects.all().order_by("step", "display_order", "label", "id")
        grouped = {}
        step_labels = dict(MasterDataField.STEP_CHOICES)
        for field in fields:
            grouped.setdefault(field.step, {"label": step_labels.get(field.step, field.step), "rows": []})
            grouped[field.step]["rows"].append(field)
    except (OperationalError, ProgrammingError):
        messages.error(request, "MasterDataField table ready nahi hai. `manage.py migrate` run karo.")
        grouped = {}

    return render(
        request,
        "portal_main/admin_masterdata_control.html",
        {
            "grouped_fields": grouped,
            "step_choices": MasterDataField.STEP_CHOICES,
            "kind_choices": MasterDataField.KIND_CHOICES,
            "is_admin_user": True,
        },
    )


@login_required
def admin_documents(request):
    if not _can_access_admin(request):
        messages.error(request, "Admin panel access allowed nahi hai.")
        return redirect("dashboard")

    if request.method == "POST":
        action = request.POST.get("action", "").strip()
        if action == "save_rule":
            name = request.POST.get("name", "").strip()
            kind = request.POST.get("kind", DocumentRule.KIND_ANY).strip()
            try:
                min_kb = max(int(request.POST.get("min_kb", "1") or "1"), 1)
            except ValueError:
                min_kb = 1
            try:
                max_kb = max(int(request.POST.get("max_kb", "500") or "500"), min_kb)
            except ValueError:
                max_kb = max(min_kb, 500)
            try:
                exact_kb_raw = int(request.POST.get("exact_kb", "") or "0")
                exact_kb = exact_kb_raw if exact_kb_raw > 0 else None
            except ValueError:
                exact_kb = None
            try:
                exact_width_raw = int(request.POST.get("exact_width", "") or "0")
                exact_width = exact_width_raw if exact_width_raw > 0 else None
            except ValueError:
                exact_width = None
            try:
                exact_height_raw = int(request.POST.get("exact_height", "") or "0")
                exact_height = exact_height_raw if exact_height_raw > 0 else None
            except ValueError:
                exact_height = None
            is_active = request.POST.get("is_active") == "on"
            if not name:
                messages.error(request, "Rule name required hai.")
                return redirect("admin_documents")
            if kind not in {DocumentRule.KIND_ANY, DocumentRule.KIND_IMAGE, DocumentRule.KIND_PDF}:
                kind = DocumentRule.KIND_ANY
            obj, created = DocumentRule.objects.get_or_create(
                name=name,
                defaults={
                    "min_kb": min_kb,
                    "max_kb": max_kb,
                    "exact_kb": exact_kb,
                    "exact_width": exact_width,
                    "exact_height": exact_height,
                    "kind": kind,
                    "is_active": is_active,
                },
            )
            if not created:
                obj.min_kb = min_kb
                obj.max_kb = max_kb
                obj.exact_kb = exact_kb
                obj.exact_width = exact_width
                obj.exact_height = exact_height
                obj.kind = kind
                obj.is_active = is_active
                obj.save(update_fields=["min_kb", "max_kb", "exact_kb", "exact_width", "exact_height", "kind", "is_active"])
                messages.success(request, f"Rule update ho gaya: {name}")
            else:
                messages.success(request, f"Rule add ho gaya: {name}")
            return redirect("admin_documents")
        if action == "delete_rule":
            rule_id = request.POST.get("rule_id", "").strip()
            if rule_id.isdigit():
                DocumentRule.objects.filter(id=int(rule_id)).delete()
                messages.success(request, "Rule remove ho gaya.")
            return redirect("admin_documents")

    rules = list(DocumentRule.objects.all())
    return render(
        request,
        "portal_main/admin_documents.html",
        {
            "rules": rules,
        },
    )


@login_required
def admin_news(request):
    if not _can_access_admin(request):
        messages.error(request, "Admin panel access allowed nahi hai.")
        return redirect("dashboard")

    if request.method == "POST":
        action = request.POST.get("action", "").strip()

        if action == "save_news":
            try:
                news_id = request.POST.get("news_id", "").strip()
                title = request.POST.get("title", "").strip()
                details = request.POST.get("details", "").strip()
                news_type = request.POST.get("news_type", PortalNews.TYPE_NOTICE).strip()
                target_portal = request.POST.get("target_portal", PortalNews.TARGET_ALL).strip()
                event_date_raw = request.POST.get("event_date", "").strip()
                display_order_raw = request.POST.get("display_order", "0").strip()
                is_active = request.POST.get("is_active") == "on"
                title_color = _safe_hex_color(request.POST.get("title_color", "#0f172a"), "#0f172a")
                details_color = _safe_hex_color(request.POST.get("details_color", "#334155"), "#334155")
                external_link = _normalize_external_link(request.POST.get("external_link", ""))
                details_pdf = request.FILES.get("details_pdf")
                image = request.FILES.get("image")

                if not title:
                    messages.error(request, "News title required hai.")
                    return redirect("admin_news")

                if news_type not in {PortalNews.TYPE_VACANCY, PortalNews.TYPE_RESULT, PortalNews.TYPE_NOTICE}:
                    news_type = PortalNews.TYPE_NOTICE

                if target_portal not in {PortalNews.TARGET_ALL, PortalNews.TARGET_GOVERNMENT, PortalNews.TARGET_STUDENT}:
                    target_portal = PortalNews.TARGET_ALL

                event_date = None
                if event_date_raw:
                    try:
                        event_date = date.fromisoformat(event_date_raw)
                    except ValueError:
                        messages.error(request, "Event date valid format me do (YYYY-MM-DD).")
                        return redirect("admin_news")

                try:
                    display_order = max(int(display_order_raw or "0"), 0)
                except ValueError:
                    display_order = 0

                if news_id.isdigit():
                    obj = get_object_or_404(PortalNews, id=int(news_id))
                    obj.title = title
                    obj.details = details
                    obj.news_type = news_type
                    obj.target_portal = target_portal
                    obj.event_date = event_date
                    obj.display_order = display_order
                    obj.is_active = is_active
                    obj.title_color = title_color
                    obj.details_color = details_color
                    obj.external_link = external_link
                    if details_pdf:
                        obj.details_pdf = details_pdf
                    if image:
                        obj.image = image
                    if request.POST.get("clear_pdf") == "on":
                        obj.details_pdf = None
                    if request.POST.get("clear_image") == "on":
                        obj.image = None
                    obj.save()
                    messages.success(request, "News update ho gayi.")
                else:
                    PortalNews.objects.create(
                        title=title,
                        details=details,
                        image=image,
                        external_link=external_link,
                        news_type=news_type,
                        target_portal=target_portal,
                        event_date=event_date,
                        display_order=display_order,
                        is_active=is_active,
                        title_color=title_color,
                        details_color=details_color,
                        details_pdf=details_pdf,
                    )
                    messages.success(request, "News add ho gayi.")
            except (OperationalError, ProgrammingError):
                messages.error(request, "News module migrate pending hai. `manage.py migrate` run karo.")
            return redirect("admin_news")

        if action == "delete_news":
            news_id = request.POST.get("news_id", "").strip()
            if news_id.isdigit():
                try:
                    PortalNews.objects.filter(id=int(news_id)).delete()
                    messages.success(request, "News remove ho gayi.")
                except (OperationalError, ProgrammingError):
                    messages.error(request, "Delete failed. Migration pending ho sakti hai.")
            return redirect("admin_news")

    try:
        # Template loop me late-failure avoid karne ke liye yahin evaluate karo.
        news_rows = list(PortalNews.objects.all())
    except (OperationalError, ProgrammingError):
        news_rows = PortalNews.objects.none()
        messages.error(request, "News table ready nahi hai. `manage.py migrate` run karo.")
    return render(
        request,
        "portal_main/admin_news.html",
        {
            "news_rows": news_rows,
            "news_type_choices": PortalNews.TYPE_CHOICES,
            "target_choices": PortalNews.TARGET_CHOICES,
        },
    )


@login_required
def user_chat(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    messages_qs = _decorate_chat_messages(profile.chat_messages.all())

    if request.method == "POST":
        is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
        if not profile.chat_enabled:
            if is_ajax:
                return JsonResponse({"ok": False, "error": "Admin ne abhi chat enable nahi kiya hai."}, status=400)
            messages.error(request, "Admin ne abhi chat enable nahi kiya hai.")
            return redirect("user_chat")
        message_text = request.POST.get("message", "").strip()
        attachment = request.FILES.get("attachment")
        if not message_text and not attachment:
            if is_ajax:
                return JsonResponse({"ok": False, "error": "Message ya attachment bhejo."}, status=400)
            messages.error(request, "Message ya attachment bhejo.")
            return redirect("user_chat")
        att_err = _validate_chat_attachment(attachment)
        if att_err:
            if is_ajax:
                return JsonResponse({"ok": False, "error": att_err}, status=400)
            messages.error(request, att_err)
            return redirect("user_chat")
        try:
            msg = ChatMessage.objects.create(
                profile=profile,
                from_admin=False,
                message=message_text,
                attachment=attachment,
            )
            
            admin_tokens = list(UserToken.objects.filter(user__is_staff=True, is_active=True).values_list('token', flat=True).distinct())
            if admin_tokens:
                sender_name = profile.full_name or profile.user.username
                _send_push_to_tokens(f"New Message from {sender_name}", message_text or "Sent an attachment", admin_tokens)
        except Exception as e:
            # Storage/upload errors (Cloudinary/FS permissions) should not crash the whole page.
            logger.exception("User chat attachment upload failed (profile_id=%s)", profile.id)
            if getattr(settings, "DEBUG", False):
                err_text = f"Attachment upload fail: {type(e).__name__}: {e}"
            else:
                err_text = "Attachment upload fail hua. File size/type check karo."
            if is_ajax:
                return JsonResponse({"ok": False, "error": err_text}, status=500)
            messages.error(request, err_text)
            return redirect("user_chat")
        if is_ajax:
            return JsonResponse({"ok": True, "message": _chat_message_payload(msg)})
        messages.success(request, "Message admin ko send ho gaya.")
        return redirect("user_chat")

    return render(
        request,
        "portal_main/user_chat.html",
        {
            "profile": profile,
            "chat_enabled": profile.chat_enabled,
            "chat_messages": messages_qs,
        },
    )


@login_required
def admin_chat(request):
    if not _can_access_admin(request):
        messages.error(request, "Admin panel access allowed nahi hai.")
        return redirect("dashboard")

    profile_id = request.GET.get("profile_id", "").strip()
    search = request.GET.get("q", "").strip()
    profiles = list(
        UserProfile.objects.select_related("user").prefetch_related("chat_messages").order_by("-id")
    )
    if search:
        search_lower = search.lower()
        profiles = [
            p
            for p in profiles
            if search_lower in (p.full_name or "").lower()
            or search_lower in p.user.username.lower()
            or search_lower in (p.mobile or "").lower()
            or (search.isdigit() and p.id == int(search))
        ]
    selected_profile = None
    if profile_id.isdigit():
        selected_profile = next((p for p in profiles if p.id == int(profile_id)), None)
    if not selected_profile:
        selected_profile = profiles[0] if profiles else None

    thread_items = []
    for p in profiles:
        all_msgs = list(p.chat_messages.all())
        last_message = all_msgs[-1] if all_msgs else None
        thread_items.append(
            {
                "profile": p,
                "last_message": last_message,
                "message_count": len(all_msgs),
            }
        )

    chat_messages_qs = (
        _decorate_chat_messages(selected_profile.chat_messages.all())
        if selected_profile
        else []
    )
    return render(
        request,
        "portal_main/admin_chat.html",
        {
            "profiles": profiles,
            "thread_items": thread_items,
            "selected_profile": selected_profile,
            "chat_messages": chat_messages_qs,
            "query": search,
        },
    )


@login_required
def admin_chat_send(request):
    if request.method != "POST" or not _can_access_admin(request):
        return redirect("admin_chat")
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    profile = get_object_or_404(UserProfile, id=request.POST.get("profile_id"))
    search = request.POST.get("q", "").strip()
    message_text = request.POST.get("message", "").strip()
    attachment = request.FILES.get("attachment")
    if not message_text and not attachment:
        if is_ajax:
            return JsonResponse({"ok": False, "error": "Message ya attachment bhejo."}, status=400)
        messages.error(request, "Message ya attachment bhejo.")
        redirect_url = f"{reverse('admin_chat')}?profile_id={profile.id}"
        if search:
            redirect_url += f"&q={search}"
        return redirect(redirect_url)
    att_err = _validate_chat_attachment(attachment)
    if att_err:
        if is_ajax:
            return JsonResponse({"ok": False, "error": att_err}, status=400)
        messages.error(request, att_err)
        redirect_url = f"{reverse('admin_chat')}?profile_id={profile.id}"
        if search:
            redirect_url += f"&q={search}"
        return redirect(redirect_url)
    try:
        msg = ChatMessage.objects.create(
            profile=profile,
            from_admin=True,
            message=message_text,
            attachment=attachment,
        )
        
        user_tokens = list(UserToken.objects.filter(user=profile.user, is_active=True).values_list('token', flat=True).distinct())
        if user_tokens:
            _send_push_to_tokens("Admin Replied", message_text or "Sent an attachment", user_tokens)
    except Exception as e:
        logger.exception("Admin chat attachment upload failed (profile_id=%s)", profile.id)
        if getattr(settings, "DEBUG", False):
            err_text = f"Attachment upload fail: {type(e).__name__}: {e}"
        else:
            err_text = "Attachment upload fail hua. File size/type check karo."
        if is_ajax:
            return JsonResponse({"ok": False, "error": err_text}, status=500)
        messages.error(request, err_text)
        redirect_url = f"{reverse('admin_chat')}?profile_id={profile.id}"
        if search:
            redirect_url += f"&q={search}"
        return redirect(redirect_url)
    if is_ajax:
        return JsonResponse({"ok": True, "message": _chat_message_payload(msg)})
    messages.success(request, "Reply send ho gayi.")
    redirect_url = f"{reverse('admin_chat')}?profile_id={profile.id}"
    if search:
        redirect_url += f"&q={search}"
    return redirect(redirect_url)


@login_required
def admin_chat_toggle(request, profile_id):
    if request.method != "POST" or not _can_access_admin(request):
        return redirect("admin_chat")
    profile = get_object_or_404(UserProfile, id=profile_id)
    profile.chat_enabled = not profile.chat_enabled
    profile.save(update_fields=["chat_enabled"])
    state = "enabled" if profile.chat_enabled else "disabled"
    messages.success(request, f"Chat {state} for #{profile.id}.")
    return redirect(f"{reverse('admin_chat')}?profile_id={profile.id}")


@login_required
def admin_chat_delete_message(request, message_id):
    if request.method != "POST" or not _can_access_admin(request):
        return redirect("admin_chat")
    msg = get_object_or_404(ChatMessage, id=message_id)
    profile_id = msg.profile_id
    search = request.POST.get("q", "").strip()
    msg.delete()
    messages.success(request, "Chat message remove ho gaya.")
    redirect_url = f"{reverse('admin_chat')}?profile_id={profile_id}"
    if search:
        redirect_url += f"&q={search}"
    return redirect(redirect_url)


@login_required
def admin_chat_delete_selected(request):
    if request.method != "POST" or not _can_access_admin(request):
        return redirect("admin_chat")
    profile_id = request.POST.get("profile_id", "").strip()
    search = request.POST.get("q", "").strip()
    if not profile_id.isdigit():
        return redirect("admin_chat")
    profile = get_object_or_404(UserProfile, id=int(profile_id))
    raw_ids = request.POST.get("selected_ids", "").strip()
    ids = []
    for part in raw_ids.split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    if not ids:
        messages.warning(request, "Select chat messages first.")
    else:
        deleted, _ = ChatMessage.objects.filter(profile=profile, id__in=ids).delete()
        messages.success(request, f"{deleted} selected messages delete ho gaye.")
    redirect_url = f"{reverse('admin_chat')}?profile_id={profile.id}"
    if search:
        redirect_url += f"&q={search}"
    return redirect(redirect_url)


@login_required
def admin_chat_clear_thread(request, profile_id):
    if request.method != "POST" or not _can_access_admin(request):
        return redirect("admin_chat")
    profile = get_object_or_404(UserProfile, id=profile_id)
    search = request.POST.get("q", "").strip()
    ChatMessage.objects.filter(profile=profile).delete()
    messages.success(request, "Chat thread delete ho gaya.")
    redirect_url = f"{reverse('admin_chat')}?profile_id={profile.id}"
    if search:
        redirect_url += f"&q={search}"
    return redirect(redirect_url)


@login_required
def user_chat_delete_message(request, message_id):
    if request.method != "POST":
        return redirect("user_chat")
    profile = get_object_or_404(UserProfile, user=request.user)
    msg = get_object_or_404(ChatMessage, id=message_id, profile=profile)
    msg.delete()
    messages.success(request, "Chat message delete ho gaya.")
    return redirect("user_chat")


@login_required
def user_chat_delete_selected(request):
    if request.method != "POST":
        return redirect("user_chat")
    profile = get_object_or_404(UserProfile, user=request.user)
    raw_ids = request.POST.get("selected_ids", "").strip()
    ids = []
    for part in raw_ids.split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    if not ids:
        messages.warning(request, "Select chat messages first.")
        return redirect("user_chat")
    deleted, _ = ChatMessage.objects.filter(profile=profile, id__in=ids).delete()
    messages.success(request, f"{deleted} selected messages delete ho gaye.")
    return redirect("user_chat")


@login_required
def user_chat_clear_thread(request):
    if request.method != "POST":
        return redirect("user_chat")
    profile = get_object_or_404(UserProfile, user=request.user)
    profile.chat_messages.all().delete()
    messages.success(request, "Chat delete ho gaya.")
    return redirect("user_chat")


@login_required
def chat_attachment_download(request, message_id):
    msg = get_object_or_404(ChatMessage, id=message_id)
    if not msg.attachment:
        return redirect("user_chat")
    is_owner = msg.profile.user_id == request.user.id
    is_admin = _can_access_admin(request)
    if not (is_owner or is_admin):
        return redirect("dashboard")
    download_name = _file_download_name(msg.attachment)
    # dl=1 => force download; else open inline (WhatsApp style "OPEN").
    force_download = request.GET.get("dl", "").strip() in {"1", "true", "yes"}
    content_type, _ = mimetypes.guess_type(download_name)
    response = FileResponse(
        msg.attachment.open("rb"),
        as_attachment=force_download,
        filename=download_name,
        content_type=content_type or "application/octet-stream",
    )
    if not force_download:
        response["Content-Disposition"] = f'inline; filename="{download_name}"'
    return response


@login_required
def admin_save_vacancy(request):
    if request.method != "POST" or not _can_access_admin(request):
        return redirect("admin_applicants")

    category = request.POST.get("category", Vacancy.CATEGORY_GOVERNMENT)
    option_scope = request.POST.get("option_scope", category)
    title = request.POST.get("title", "").strip()
    organization = request.POST.get("organization", "").strip()
    last_date = request.POST.get("last_date", "").strip()
    icon_name = request.POST.get("icon_name", "").strip() or "description"
    display_order = request.POST.get("display_order", "0").strip() or "0"
    is_active = request.POST.get("is_active") == "on"
    hidden_from_users = request.POST.get("hidden_from_users") == "on"
    visible_to_users = _collect_multi_values(request, "visible_to_users", "visible_to_users_item[]")
    mobile_page_size_raw = request.POST.get("mobile_page_size", "6").strip() or "6"
    editor_docs = _collect_doc_editor_inputs(request)
    editor_fields = _collect_field_editor_inputs(request)
    required_documents = _collect_multi_values(request, "required_documents", "required_documents_item[]")
    required_profile_fields = _collect_multi_values(request, "required_profile_fields", "required_profile_fields_item[]")
    bulk_docs, bulk_fields = _parse_bulk_requirements(request.POST.get("bulk_requirements", ""))
    extra_bulk_docs = _parse_bulk_documents(request.POST.get("bulk_documents", ""))
    required_documents = _merge_unique_casefold((required_documents or []) + (editor_docs or []) + bulk_docs)
    required_documents = _merge_unique_casefold(required_documents + extra_bulk_docs)
    required_profile_fields = _merge_unique_casefold((required_profile_fields or []) + (editor_fields or []) + bulk_fields)

    if category not in {Vacancy.CATEGORY_GOVERNMENT, Vacancy.CATEGORY_STUDENT}:
        messages.error(request, "Category valid nahi hai.")
        return redirect("admin_option_control", category=option_scope if option_scope in {Vacancy.CATEGORY_GOVERNMENT, Vacancy.CATEGORY_STUDENT} else Vacancy.CATEGORY_GOVERNMENT)
    if not title or not organization or not last_date:
        messages.error(request, "Title, organization aur last date required hai.")
        return redirect("admin_option_control", category=option_scope if option_scope in {Vacancy.CATEGORY_GOVERNMENT, Vacancy.CATEGORY_STUDENT} else Vacancy.CATEGORY_GOVERNMENT)
    if not required_documents and not required_profile_fields:
        messages.error(request, "Kam se kam ek field ya document add karo. Blank option save nahi hoga.")
        return redirect("admin_option_control", category=option_scope if option_scope in {Vacancy.CATEGORY_GOVERNMENT, Vacancy.CATEGORY_STUDENT} else Vacancy.CATEGORY_GOVERNMENT)

    try:
        parsed_date = date.fromisoformat(last_date)
    except ValueError:
        messages.error(request, "Last date valid format me do (YYYY-MM-DD).")
        return redirect("admin_option_control", category=option_scope if option_scope in {Vacancy.CATEGORY_GOVERNMENT, Vacancy.CATEGORY_STUDENT} else Vacancy.CATEGORY_GOVERNMENT)

    try:
        order_val = max(int(display_order), 0)
    except ValueError:
        order_val = 0
    try:
        mobile_page_size = max(int(mobile_page_size_raw), 1)
    except ValueError:
        mobile_page_size = 6

    vacancy = Vacancy(
        category=category,
        title=title,
        organization=organization,
        last_date=parsed_date,
        icon_name=icon_name,
        display_order=order_val,
        is_active=is_active,
        hidden_from_users=hidden_from_users,
        visible_to_users=visible_to_users,
        required_documents=required_documents,
        required_profile_fields=required_profile_fields,
        mobile_page_size=mobile_page_size,
    )
    if request.FILES.get("image"):
        vacancy.image = request.FILES["image"]
    vacancy.save()
    messages.success(
        request,
        f"New {vacancy.get_category_display()} option add ho gaya. Fields: {len(required_profile_fields)} | Docs: {len(required_documents)}.",
    )
    return redirect("admin_option_control", category=category)


@login_required
def admin_delete_vacancy(request, vacancy_id):
    if request.method != "POST" or not _can_access_admin(request):
        return redirect("admin_applicants")

    option_scope = request.POST.get("option_scope", "").strip()
    vacancy = get_object_or_404(Vacancy, id=vacancy_id)
    if option_scope not in {Vacancy.CATEGORY_GOVERNMENT, Vacancy.CATEGORY_STUDENT}:
        option_scope = vacancy.category
    if vacancy.applications.exists():
        vacancy.is_active = False
        vacancy.save(update_fields=["is_active"])
        if _is_ajax_request(request):
            return JsonResponse({"ok": True, "deactivated": True, "vacancy_id": vacancy.id})
        messages.warning(request, "Is option par applications hain, isliye inactive kiya gaya.")
        if option_scope in {Vacancy.CATEGORY_GOVERNMENT, Vacancy.CATEGORY_STUDENT}:
            return redirect("admin_option_control", category=option_scope)
        return redirect("admin_applicants")

    vacancy.delete()
    if _is_ajax_request(request):
        return JsonResponse({"ok": True, "deleted": True, "vacancy_id": vacancy_id})
    messages.success(request, "Option delete ho gaya.")
    return redirect("admin_option_control", category=option_scope)


@login_required
def admin_update_vacancy(request, vacancy_id):
    if request.method != "POST" or not _can_access_admin(request):
        return redirect("admin_applicants")

    vacancy = get_object_or_404(Vacancy, id=vacancy_id)
    option_scope = request.POST.get("option_scope", "").strip()
    if option_scope not in {Vacancy.CATEGORY_GOVERNMENT, Vacancy.CATEGORY_STUDENT}:
        option_scope = vacancy.category

    title = request.POST.get("title", "").strip()
    organization = request.POST.get("organization", "").strip()
    last_date = request.POST.get("last_date", "").strip()
    icon_name = request.POST.get("icon_name", "").strip() or "description"
    display_order = request.POST.get("display_order", "0").strip() or "0"
    is_active = vacancy.is_active if "is_active" not in request.POST else request.POST.get("is_active") == "on"
    hidden_from_users = vacancy.hidden_from_users if "hidden_from_users" not in request.POST else request.POST.get("hidden_from_users") == "on"
    visible_to_users = _collect_multi_values(request, "visible_to_users", "visible_to_users_item[]")
    mobile_page_size_raw = request.POST.get("mobile_page_size", str(getattr(vacancy, "mobile_page_size", 6))).strip() or "6"
    editor_docs = _collect_doc_editor_inputs(request)
    editor_fields = _collect_field_editor_inputs(request)
    required_documents = _collect_multi_values(request, "required_documents", "required_documents_item[]")
    required_profile_fields = _collect_multi_values(request, "required_profile_fields", "required_profile_fields_item[]")
    bulk_docs, bulk_fields = _parse_bulk_requirements(request.POST.get("bulk_requirements", ""))
    extra_bulk_docs = _parse_bulk_documents(request.POST.get("bulk_documents", ""))
    required_documents = _merge_unique_casefold((required_documents or []) + (editor_docs or []) + bulk_docs)
    required_documents = _merge_unique_casefold(required_documents + extra_bulk_docs)
    required_profile_fields = _merge_unique_casefold((required_profile_fields or []) + (editor_fields or []) + bulk_fields)

    if not title or not organization or not last_date:
        messages.error(request, "Edit ke liye title, organization, last date required hai.")
        return redirect("admin_option_control", category=option_scope)
    if not required_documents and not required_profile_fields:
        messages.error(request, "Kam se kam ek field ya document add karo. Blank option save nahi hoga.")
        return redirect("admin_option_control", category=option_scope)

    try:
        parsed_date = date.fromisoformat(last_date)
    except ValueError:
        messages.error(request, "Last date valid format me do (YYYY-MM-DD).")
        return redirect("admin_option_control", category=option_scope)

    try:
        order_val = max(int(display_order), 0)
    except ValueError:
        order_val = 0
    try:
        mobile_page_size = max(int(mobile_page_size_raw), 1)
    except ValueError:
        mobile_page_size = max(int(getattr(vacancy, "mobile_page_size", 6) or 6), 1)

    vacancy.title = title
    vacancy.organization = organization
    vacancy.last_date = parsed_date
    vacancy.icon_name = icon_name
    vacancy.display_order = order_val
    vacancy.is_active = is_active
    vacancy.hidden_from_users = hidden_from_users
    vacancy.visible_to_users = visible_to_users
    vacancy.required_documents = required_documents
    vacancy.required_profile_fields = required_profile_fields
    vacancy.mobile_page_size = mobile_page_size
    if request.FILES.get("image"):
        vacancy.image = request.FILES["image"]
    if request.POST.get("clear_image") == "on":
        vacancy.image = None
    vacancy.save()
    messages.success(
        request,
        f"Option update ho gaya. Fields: {len(required_profile_fields)} | Docs: {len(required_documents)}.",
    )
    return redirect("admin_option_control", category=option_scope)


@login_required
def admin_toggle_vacancy_active(request, vacancy_id):
    if request.method != "POST" or not _can_access_admin(request):
        return redirect("admin_applicants")
    vacancy = get_object_or_404(Vacancy, id=vacancy_id)
    explicit_state = request.POST.get("set_active", "").strip().lower()
    if explicit_state in {"1", "true", "yes", "on"}:
        vacancy.is_active = True
    elif explicit_state in {"0", "false", "no", "off"}:
        vacancy.is_active = False
    else:
        vacancy.is_active = not bool(vacancy.is_active)
    vacancy.save(update_fields=["is_active"])
    if _is_ajax_request(request):
        return JsonResponse({"ok": True, "vacancy_id": vacancy.id, "is_active": vacancy.is_active})
    messages.success(request, "Vacancy status update ho gaya.")
    option_scope = request.POST.get("option_scope", vacancy.category)
    return redirect("admin_option_control", category=option_scope)


@login_required
def admin_toggle_vacancy_user_hide(request, vacancy_id):
    if request.method != "POST" or not _can_access_admin(request):
        return redirect("admin_applicants")
    vacancy = get_object_or_404(Vacancy, id=vacancy_id)
    vacancy.hidden_from_users = not bool(vacancy.hidden_from_users)
    vacancy.save(update_fields=["hidden_from_users"])
    if _is_ajax_request(request):
        return JsonResponse({"ok": True, "vacancy_id": vacancy.id, "hidden_from_users": vacancy.hidden_from_users})
    messages.success(request, "User visibility update ho gaya.")
    option_scope = request.POST.get("option_scope", vacancy.category)
    return redirect("admin_option_control", category=option_scope)


@login_required
def admin_update_application(request, application_id):
    if request.method != "POST" or not _can_access_admin(request):
        return redirect("admin_applicants")

    app = get_object_or_404(Application, id=application_id)
    action = request.POST.get("action", "set_status")

    if action == "cancel":
        app.status = Application.STATUS_CANCELLED
        app.cancelled_at = timezone.now()
        ApplicationHistory.objects.create(
            application=app,
            action=ApplicationHistory.ACTION_CANCEL,
            profile_name=app.profile.full_name or "",
            applicant_username=app.profile.user.username,
            vacancy_title=app.vacancy.title,
            actor_username=request.user.username,
            note="Application cancelled by admin",
        )
        messages.warning(request, f"Application #{app.id} cancel kar di gayi.")
    else:
        new_status = request.POST.get("status", Application.STATUS_PENDING)
        valid_values = {value for value, _ in Application.STATUS_CHOICES}
        if new_status in valid_values:
            prev_status = app.status
            app.status = new_status
            if new_status != Application.STATUS_CANCELLED:
                app.cancelled_at = None
            if new_status != prev_status:
                ApplicationHistory.objects.create(
                    application=app,
                    action=ApplicationHistory.ACTION_STATUS,
                    profile_name=app.profile.full_name or "",
                    applicant_username=app.profile.user.username,
                    vacancy_title=app.vacancy.title,
                    actor_username=request.user.username,
                    note=f"Status changed: {prev_status} -> {new_status}",
                )
            messages.success(request, f"Application #{app.id} status update ho gaya.")
    app.save(update_fields=["status", "cancelled_at", "updated_at"])
    if _is_ajax_request(request):
        return JsonResponse(
            {
                "ok": True,
                "application_id": app.id,
                "status": app.status,
                "status_label": _status_label(app.status),
            }
        )
    return redirect("admin_applicants")


@login_required
def admin_remove_application(request, application_id):
    if request.method != "POST" or not _can_access_admin(request):
        return redirect("admin_applicants")
    app = get_object_or_404(Application, id=application_id)
    ApplicationHistory.objects.create(
        application=app,
        action=ApplicationHistory.ACTION_REMOVE,
        profile_name=app.profile.full_name or "",
        applicant_username=app.profile.user.username,
        vacancy_title=app.vacancy.title,
        actor_username=request.user.username,
        note="Application removed by admin",
    )
    app.delete()
    if _is_ajax_request(request):
        return JsonResponse({"ok": True, "application_id": application_id})
    messages.success(request, f"Application #{application_id} remove ho gayi.")
    return redirect("admin_applicants")


@login_required
def admin_remove_history_entry(request, history_id):
    if request.method != "POST" or not _can_access_admin(request):
        return redirect("admin_applicants")
    ApplicationHistory.objects.filter(id=history_id).delete()
    if _is_ajax_request(request):
        return JsonResponse({"ok": True, "history_id": history_id})
    messages.success(request, "History entry remove ho gayi.")
    return redirect("admin_applicants")


@login_required
def admin_clear_history(request):
    if request.method != "POST" or not _can_access_admin(request):
        return redirect("admin_applicants")
    ApplicationHistory.objects.all().delete()
    if _is_ajax_request(request):
        return JsonResponse({"ok": True})
    messages.success(request, "Applicants history clear ho gayi.")
    return redirect("admin_applicants")


@login_required
def admin_applicant_detail_json(request, application_id):
    if not _can_access_admin(request):
        return JsonResponse({"error": "forbidden"}, status=403)

    app = get_object_or_404(_application_base_queryset(), id=application_id)
    profile = app.profile
    step_data = _profile_step_data(profile)
    payload = _extract_payload_from_remarks(app.remarks)
    use_payload_only = bool(payload)
    docs = []
    vacancy_extra = []
    for item in payload.get("vacancy_required_documents", []):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        value = str(item.get("value", "")).strip()
        if not label and not value:
            continue
        vacancy_extra.append((label or "Extra Field", value))
        if value and (str(value).startswith("/media/") or str(value).startswith("http://") or str(value).startswith("https://")):
            docs.append({"title": label or "Document", "url": value})

    if not docs and not use_payload_only:
        docs = _collect_document_links(app)

    data = {
        "applicationId": app.id,
        "applicantId": profile.id,
        "vacancy": app.vacancy.title,
        "organization": app.vacancy.organization,
        "status": _status_label(app.status),
        "appliedAt": app.applied_at.strftime("%Y-%m-%d %H:%M"),
        "personal": _rows_from_payload(payload, "personal", [] if use_payload_only else step_data.get("personal", [])),
        "address": _rows_from_payload(payload, "address", [] if use_payload_only else step_data.get("address", [])),
        "academic": _rows_from_payload(payload, "academic", [] if use_payload_only else step_data.get("academic", [])),
        "college": _rows_from_payload(payload, "college", [] if use_payload_only else step_data.get("college", [])),
        "bank": _rows_from_payload(payload, "bank", [] if use_payload_only else step_data.get("bank", [])),
        "vacancy_extra": vacancy_extra,
        "documents": docs,
    }
    return JsonResponse(data)


@login_required
def admin_demo_document_download(request, application_id, doc_type):
    if not _can_access_admin(request):
        return redirect("dashboard")

    app = get_object_or_404(_application_base_queryset(), id=application_id)
    safe_doc_type = doc_type.lower()
    doc_names = {
        "aadhaar": "aadhaar_card_demo.txt",
        "marksheet": "marksheet_demo.txt",
    }
    filename = doc_names.get(safe_doc_type, "document_demo.txt")
    content = (
        f"Demo document\n"
        f"Application ID: {app.id}\n"
        f"Applicant ID: {app.profile.id}\n"
        f"Applicant: {app.profile.full_name or app.profile.user.username}\n"
        f"Document Type: {safe_doc_type}\n"
        f"Generated At: {timezone.now().isoformat()}\n"
    )
    response = HttpResponse(content, content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
def admin_export_csv(request):
    if not _can_access_admin(request):
        return redirect("dashboard")

    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "all").strip() or "all"
    applications = _filtered_applications(query, status)
    return _csv_response(applications, "applicants_export.csv")


@login_required
def admin_export_single_csv(request, application_id):
    if not _can_access_admin(request):
        return redirect("dashboard")
    app = get_object_or_404(_application_base_queryset(), id=application_id)
    return _csv_response([app], f"applicant_{app.id}.csv")


def _csv_response(applications, filename):
    rows = [_flatten_application_row(app) for app in applications]
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.DictWriter(response, fieldnames=list(rows[0].keys()) if rows else ["No Data"])
    writer.writeheader()
    if rows:
        writer.writerows(rows)
    else:
        writer.writerow({"No Data": "No matching records"})
    return response


@login_required
def admin_applicant_pdf(request, application_id):
    if not _can_access_admin(request):
        return redirect("dashboard")
    app = get_object_or_404(_application_base_queryset(), id=application_id)
    return render(
        request,
        "portal_main/applicant_pdf.html",
        {
            "application": app,
            "profile": app.profile,
            "documents": app.profile.documents.all(),
            "status_label": _status_label(app.status),
        },
    )


@login_required
def admin_applicant_extension_file(request, application_id):
    if not _can_access_admin(request):
        return redirect("dashboard")

    app = get_object_or_404(_application_base_queryset(), id=application_id)
    row = _flatten_application_row(app)
    payload = {
        "meta": {
            "generatedAt": timezone.now().isoformat(),
            "format": "chrome-autofill-compatible",
            "applicationId": app.id,
        },
        "applicant": row,
    }

    response = HttpResponse(json.dumps(payload, indent=2), content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename="applicant_{app.id}_extension.json"'
    return response


@login_required
def admin_download_all_documents(request, application_id):
    if not _can_access_admin(request):
        return redirect("dashboard")
    app = get_object_or_404(_application_base_queryset(), id=application_id)
    profile = app.profile

    applicant_name = _slug_name(profile.full_name or profile.user.username)
    zip_name = f"{applicant_name}_documents.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        idx = 1
        if profile.photo:
            ext = (profile.photo.name.rsplit(".", 1)[-1] if "." in profile.photo.name else "jpg")
            profile.photo.open("rb")
            zf.writestr(f"{idx:02d}_passport_photo.{ext}", profile.photo.read())
            profile.photo.close()
            idx += 1
        if profile.signature:
            ext = (profile.signature.name.rsplit(".", 1)[-1] if "." in profile.signature.name else "png")
            profile.signature.open("rb")
            zf.writestr(f"{idx:02d}_signature.{ext}", profile.signature.read())
            profile.signature.close()
            idx += 1
        for doc in profile.documents.all():
            file_name = doc.file.name.rsplit("/", 1)[-1]
            safe_title = _slug_name(doc.title or "document")
            doc.file.open("rb")
            zf.writestr(f"{idx:02d}_{safe_title}_{file_name}", doc.file.read())
            doc.file.close()
            idx += 1

    buffer.seek(0)
    response = HttpResponse(buffer.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{zip_name}"'
    return response


@login_required
def enter_admin_panel(request):
    if not _can_access_admin(request):
        messages.error(request, "Admin panel access allowed nahi hai.")
        return redirect("role_select")
    return redirect("admin_applicants")
