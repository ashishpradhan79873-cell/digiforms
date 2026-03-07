import csv
import io
import json
import zipfile
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import quote_plus
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.db import OperationalError, ProgrammingError
from django.db.models import Q
from django.http import FileResponse, HttpResponse, JsonResponse
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
    UserDocument,
    UserProfile,
    Vacancy,
)
from PIL import Image


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
    ("bank", "Bank Details"),
    ("documents", "Document Upload"),
]

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
DEFAULT_REQUIRED_DOCS = []
APPLY_PENDING_TIMEOUT_MINUTES = 30
AUTOFILL_LOCK_HOURS = 24
APPLY_PROFILE_DAILY_VIEW_LIMIT = 5
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


def _parse_required_doc_name(raw_value):
    value = str(raw_value or "").strip()
    if value.startswith("DATA|"):
        return "Data", value[5:].strip()
    if value.startswith("PHOTO|"):
        return "Photo", value[6:].strip()
    if value.startswith("DOC|"):
        return "Document", value[4:].strip()
    return "Document", value


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
        req_norm = _norm_field_key(req)
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
            grouped[match["step"]].append((req, match["value"]))
        else:
            grouped["personal"].append((req, ""))

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
        if len(cols) == 1:
            field_name = cols[0]
            section_name = current_section
        else:
            section_name = cols[0] or current_section
            field_name = cols[1] if len(cols) > 1 else ""

        if section_name:
            current_section = section_name
        field_name = re.sub(r"\s+", " ", str(field_name or "")).strip().strip('"')
        if not field_name:
            continue
        if field_name.startswith("(") and field_name.endswith(")"):
            continue

        kind, clean_field = _classify_bulk_requirement(current_section, field_name)
        if not clean_field:
            continue
        if kind == "DATA":
            profile_fields.append(clean_field)
        else:
            docs.append(f"{kind}|{clean_field}")

    return _merge_unique_casefold(docs), _merge_unique_casefold(profile_fields)


def home_router(request):
    if request.user.is_authenticated:
        return redirect("master_data_option")
    return redirect("login")


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
        if not vacancy.is_active:
            vacancy.is_active = True
            updates.append("is_active")
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
            ("Account Holder", profile.account_holder_name),
            ("Bank Name", profile.bank_name),
            ("Account Number", profile.account_number),
            ("IFSC", profile.ifsc_code),
            ("Branch", profile.branch_name),
            ("Aadhaar Linked", profile.aadhaar_linked),
        ],
        "documents": (
            [("Passport Photo", profile.photo.url)] if profile.photo else []
        )
        + ([("Signature", profile.signature.url)] if profile.signature else [])
        + [(doc.title or "Document", doc.file.url) for doc in profile.documents.all()],
    }
    _append_extra_rows(step_data["personal"], profile.personal_extra_rows)
    _append_extra_rows(step_data["address"], profile.address_extra_rows)
    _append_extra_rows(step_data["academic"], profile.academic_extra_rows)
    _append_extra_rows(step_data["college"], profile.college_extra_rows)
    _append_extra_rows(step_data["bank"], profile.bank_extra_rows)
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


def _attachment_kind(file_name):
    lower_name = (file_name or "").lower()
    if lower_name.endswith(".pdf"):
        return "pdf"
    if lower_name.endswith(IMAGE_EXTENSIONS):
        return "image"
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
}


def _save_payload_to_master_data(profile, payload):
    changed_fields = set()
    section_extra_map = {
        "personal": "personal_extra_rows",
        "address": "address_extra_rows",
        "academic": "academic_extra_rows",
        "college": "college_extra_rows",
        "bank": "bank_extra_rows",
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
            item.attachment_kind = _attachment_kind(item.attachment.name)
            item.attachment_name = item.attachment.name.split("/")[-1]
        else:
            item.attachment_kind = ""
            item.attachment_name = ""
    return decorated


def _chat_message_payload(msg):
    return {
        "id": msg.id,
        "from_admin": bool(msg.from_admin),
        "message": msg.message or "",
        "time": msg.created_at.strftime("%H:%M"),
        "attachment": {
            "url": msg.attachment.url if msg.attachment else "",
            "name": _file_download_name(msg.attachment) if msg.attachment else "",
            "kind": _attachment_kind(msg.attachment.name) if msg.attachment else "",
            "download_url": reverse("chat_attachment_download", args=[msg.id]) if msg.attachment else "",
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


def _selected_payload(profile, selected_steps):
    all_data = _profile_step_data(profile)
    payload = {}
    for key in selected_steps:
        rows = all_data.get(key, [])
        payload[key] = [{"label": label, "value": value or ""} for label, value in rows]
    return payload


def _inject_required_docs_rows(profile, vacancy, step_data):
    required_docs = vacancy.required_documents or DEFAULT_REQUIRED_DOCS
    document_rows = list(step_data.get("documents", []))
    existing_labels = {str(label).strip().lower() for label, _ in document_rows}

    available = {}
    if profile.photo:
        available["passport photo"] = profile.photo.url
    if profile.signature:
        available["signature"] = profile.signature.url
    for doc in profile.documents.all():
        title = (doc.title or "").strip().lower()
        if title:
            available[title] = doc.file.url

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
        if not key:
            return ""
        if key in available:
            return available[key]
        for title, url in available.items():
            if key in title or title in key:
                return url
        return "Not uploaded yet"

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
        if doc_kind in {"Document", "Photo"} and _find_doc_value(doc_name) not in {"", "Not uploaded yet"}:
            continue
        row_label = f"Required ({doc_kind}): {clean_name}"
        if row_label.strip().lower() in existing_labels:
            continue
        document_rows.append((row_label, _find_doc_value(clean_name)))

    step_data["documents"] = document_rows
    return required_docs


def _build_required_doc_rows(profile, required_docs, step_data=None, required_profile_fields=None):
    available = {}
    if profile.photo:
        available["passport photo"] = profile.photo.url
    if profile.signature:
        available["signature"] = profile.signature.url
    for doc in profile.documents.all():
        title = (doc.title or "").strip().lower()
        if title:
            available[title] = doc.file.url

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
        value = available.get(key, "")
        if not value:
            for title, url in available.items():
                if key in title or title in key:
                    value = url
                    break
        if not value:
            value = "Not uploaded yet"
        rows.append(
            {
                "idx": idx,
                "label": clean_name,
                "kind": doc_kind,
                "value": value,
                "input_name": f"vacdoc__{idx}",
                "checkbox_name": f"vacdoc_select__{idx}",
                "file_input_name": f"vacdoc_file__{idx}",
                "checked": True,
                "exists": value not in {"", "Not uploaded yet"},
            }
        )
    return rows


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
    if profile.photo:
        docs.append({"title": "Passport Photo", "url": profile.photo.url})
    if profile.signature:
        docs.append({"title": "Signature", "url": profile.signature.url})
    docs.extend([{"title": d.title or "Document", "url": d.file.url} for d in profile.documents.all()])
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
        "Bank Name": profile.bank_name,
        "Account Holder": profile.account_holder_name,
        "Account Number": profile.account_number,
        "IFSC": profile.ifsc_code,
        "Branch": profile.branch_name,
        "Personal Extra Rows": _extra_rows_as_text(profile.personal_extra_rows),
        "Address Extra Rows": _extra_rows_as_text(profile.address_extra_rows),
        "Academic Extra Rows": _extra_rows_as_text(profile.academic_extra_rows),
        "College Extra Rows": _extra_rows_as_text(profile.college_extra_rows),
        "Bank Extra Rows": _extra_rows_as_text(profile.bank_extra_rows),
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
    services = Vacancy.objects.filter(is_active=True, category=Vacancy.CATEGORY_STUDENT).order_by(
        "display_order", "last_date", "id"
    )
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
        is_active=True,
        category=Vacancy.CATEGORY_STUDENT,
    )
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
    vacancies = Vacancy.objects.filter(is_active=True, category=Vacancy.CATEGORY_GOVERNMENT).order_by(
        "display_order", "last_date", "id"
    )
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
        is_active=True,
        category=Vacancy.CATEGORY_GOVERNMENT,
    )
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
    now = timezone.now()
    lock_until = getattr(profile, "apply_autofill_locked_until", None)
    lock_active = bool(lock_until and now < lock_until)
    timed_out_active = _is_pending_apply_timed_out(pending)
    if timed_out_active and not lock_active:
        profile.apply_autofill_locked_until = now + timedelta(hours=AUTOFILL_LOCK_HOURS)
        profile.save(update_fields=["apply_autofill_locked_until"])
        lock_until = profile.apply_autofill_locked_until
        lock_active = True
    vacancy = get_object_or_404(Vacancy, id=pending.get("vacancy_id"))
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
        selected_steps = request.POST.getlist("steps")
        if not selected_steps:
            messages.error(request, "Kam se kam ek data step select karo.")
            return redirect("confirm_send_to_admin")
        if submit_mode not in {"save_only", "save_master"}:
            consent_1 = request.POST.get("consent_data_usage") == "1"
            consent_2 = request.POST.get("consent_user_responsibility") == "1"
            if not (consent_1 and consent_2):
                messages.error(request, "Form send karne se pehle dono disclaimer tick karna zaroori hai.")
                return redirect("confirm_send_to_admin")

        step_data = _profile_step_data(profile)
        if timed_out_active or lock_active:
            step_data = _mask_step_rows(step_data)
        required_docs = _inject_required_docs_rows(profile, vacancy, step_data)
        required_doc_rows = _build_required_doc_rows(
            profile,
            required_docs,
            step_data=step_data,
            required_profile_fields=vacancy.required_profile_fields,
        )
        requested_profile_rows = _build_requested_profile_rows(step_data, vacancy.required_profile_fields)
        use_requested_only = bool(vacancy.required_profile_fields)

        payload = {}
        for step_key, _ in PROFILE_DATA_STEPS:
            if step_key not in selected_steps:
                continue
            if use_requested_only and step_key != "documents":
                rows = requested_profile_rows.get(step_key, [])
            else:
                rows = step_data.get(step_key, [])
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
        if submit_mode == "save_only":
            pending["draft_payload"] = payload
            pending["draft_vacancy_docs"] = selected_vac_docs
            pending["last_edit_at"] = timezone.now().isoformat()
            request.session["pending_form_apply"] = pending
            _save_profile_draft(profile, vacancy.id, payload, selected_vac_docs, pending.get("started_at", ""))
            for item in selected_vac_docs:
                value = str(item.get("value", "")).strip()
                if value and value != "Not uploaded yet":
                    continue
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
        if not payload:
            messages.error(request, "Kam se kam ek field select karo.")
            return redirect("confirm_send_to_admin")
        step_names = {
            key: label for key, label in PROFILE_DATA_STEPS
        }
        selected_labels = [step_names.get(key, key) for key in selected_steps]
        summary_line = "Selected Data: " + ", ".join(selected_labels)
        if selected_vac_docs:
            summary_line += " | Vacancy Docs: " + ", ".join([x["label"] for x in selected_vac_docs])
        payload_line = "Payload JSON: " + json.dumps(payload, ensure_ascii=True)

        app, created = Application.objects.get_or_create(profile=profile, vacancy=vacancy)
        if app.status == Application.STATUS_CANCELLED:
            app.status = Application.STATUS_PENDING
            app.cancelled_at = None
        app.remarks = (summary_line + "\n" + payload_line)[:4000]
        app.save()

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
    if timed_out_active or lock_active:
        step_data = _mask_step_rows(step_data)
    required_docs = _inject_required_docs_rows(profile, vacancy, step_data)
    required_doc_rows = _build_required_doc_rows(
        profile,
        required_docs,
        step_data=step_data,
        required_profile_fields=vacancy.required_profile_fields,
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

    requested_profile_rows = _build_requested_profile_rows(step_data, vacancy.required_profile_fields)
    use_requested_only = bool(vacancy.required_profile_fields)
    selected_default = []
    step_cards = []
    for key, label in PROFILE_DATA_STEPS:
        if use_requested_only and key != "documents":
            rows = requested_profile_rows.get(key, [])
        else:
            rows = step_data.get(key, [])
        if use_requested_only and key == "documents":
            rows = []
        if draft_payload:
            rows = _rows_from_payload(draft_payload, key, rows)
        row_items = []
        for idx, row in enumerate(rows):
            row_label = row[0]
            row_value = row[1] or ""
            should_check = True
            row_items.append(
                {
                    "label": row_label,
                    "value": row_value,
                    "input_name": f"field__{key}__{idx}",
                    "checkbox_name": f"select__{key}__{idx}",
                    "checked": should_check,
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
            "use_requested_only": use_requested_only,
            "autofill_lock_active": lock_active,
            "autofill_lock_until": lock_until,
            "apply_timed_out": timed_out_active,
            "apply_timeout_minutes": APPLY_PENDING_TIMEOUT_MINUTES,
        },
    )


@login_required
def apply_profile_preview(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    pending = request.session.get("pending_form_apply") or {}
    vacancy = None
    if pending.get("vacancy_id"):
        vacancy = Vacancy.objects.filter(id=pending.get("vacancy_id")).first()
    timed_out_active = _is_pending_apply_timed_out(pending)
    now = timezone.now()
    lock_until = getattr(profile, "apply_autofill_locked_until", None)
    if timed_out_active and (not lock_until or now >= lock_until):
        profile.apply_autofill_locked_until = now + timedelta(hours=AUTOFILL_LOCK_HOURS)
        profile.save(update_fields=["apply_autofill_locked_until"])
        lock_until = profile.apply_autofill_locked_until

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
        messages.error(request, "Aaj ka apply profile view limit (5) complete ho gaya.")
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

    step_data = dict(all_step_data)
    if draft_payload:
        for key, rows in all_step_data.items():
            if not isinstance(rows, list):
                continue
            step_data[key] = _rows_from_payload(draft_payload, key, rows)
    elif payload:
        for key, rows in all_step_data.items():
            if not isinstance(rows, list):
                continue
            step_data[key] = _rows_from_payload(payload, key, rows)

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
        lower_url = url.lower()
        kind = "image" if any(lower_url.endswith(ext) for ext in IMAGE_EXTENSIONS) else "file"
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
        for row in required_doc_rows:
            url = _resolve_value_url(row.get("value"))
            if not url:
                continue
            _push(row.get("label") or "Document", url)

        if payload and isinstance(payload.get("vacancy_required_documents"), list):
            for item in payload.get("vacancy_required_documents", []):
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or "Document").strip()
                url = _resolve_value_url(item.get("value"))
                if url:
                    _push(label, url)

        if not document_links:
            photo_url = _safe_file_url(profile.photo)
            sign_url = _safe_file_url(profile.signature)
            _push("Passport Photo", photo_url)
            _push("Signature", sign_url)
            for doc in profile.documents.all():
                url = _safe_file_url(doc.file)
                if not url:
                    continue
                _push(doc.title or "Document", url)

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
        msg = ChatMessage.objects.create(
            profile=profile,
            from_admin=False,
            message=message_text,
            attachment=attachment,
        )
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
    msg = ChatMessage.objects.create(
        profile=profile,
        from_admin=True,
        message=message_text,
        attachment=attachment,
    )
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
    return FileResponse(msg.attachment.open("rb"), as_attachment=True, filename=download_name)


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
    required_documents = _collect_multi_values(request, "required_documents", "required_documents_item[]")
    required_profile_fields = _collect_multi_values(request, "required_profile_fields", "required_profile_fields_item[]")
    bulk_docs, bulk_fields = _parse_bulk_requirements(request.POST.get("bulk_requirements", ""))
    required_documents = _merge_unique_casefold((required_documents or []) + bulk_docs)
    required_profile_fields = _merge_unique_casefold((required_profile_fields or []) + bulk_fields)

    if category not in {Vacancy.CATEGORY_GOVERNMENT, Vacancy.CATEGORY_STUDENT}:
        messages.error(request, "Category valid nahi hai.")
        return redirect("admin_option_control", category=option_scope if option_scope in {Vacancy.CATEGORY_GOVERNMENT, Vacancy.CATEGORY_STUDENT} else Vacancy.CATEGORY_GOVERNMENT)
    if not title or not organization or not last_date:
        messages.error(request, "Title, organization aur last date required hai.")
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

    vacancy = Vacancy(
        category=category,
        title=title,
        organization=organization,
        last_date=parsed_date,
        icon_name=icon_name,
        display_order=order_val,
        is_active=is_active,
        required_documents=required_documents,
        required_profile_fields=required_profile_fields,
    )
    if request.FILES.get("image"):
        vacancy.image = request.FILES["image"]
    vacancy.save()
    messages.success(request, f"New {vacancy.get_category_display()} option add ho gaya.")
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
    is_active = request.POST.get("is_active") == "on"
    required_documents = _collect_multi_values(request, "required_documents", "required_documents_item[]")
    required_profile_fields = _collect_multi_values(request, "required_profile_fields", "required_profile_fields_item[]")
    bulk_docs, bulk_fields = _parse_bulk_requirements(request.POST.get("bulk_requirements", ""))
    required_documents = _merge_unique_casefold((required_documents or []) + bulk_docs)
    required_profile_fields = _merge_unique_casefold((required_profile_fields or []) + bulk_fields)

    if not title or not organization or not last_date:
        messages.error(request, "Edit ke liye title, organization, last date required hai.")
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

    vacancy.title = title
    vacancy.organization = organization
    vacancy.last_date = parsed_date
    vacancy.icon_name = icon_name
    vacancy.display_order = order_val
    vacancy.is_active = is_active
    vacancy.required_documents = required_documents
    vacancy.required_profile_fields = required_profile_fields
    if request.FILES.get("image"):
        vacancy.image = request.FILES["image"]
    if request.POST.get("clear_image") == "on":
        vacancy.image = None
    vacancy.save()
    messages.success(request, "Option update ho gaya.")
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
    docs = _collect_document_links(app)
    step_data = _profile_step_data(profile)
    payload = _extract_payload_from_remarks(app.remarks)
    vacancy_extra = []
    for item in payload.get("vacancy_required_documents", []):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        value = str(item.get("value", "")).strip()
        if not label and not value:
            continue
        vacancy_extra.append((label or "Extra Field", value))

    data = {
        "applicationId": app.id,
        "applicantId": profile.id,
        "vacancy": app.vacancy.title,
        "organization": app.vacancy.organization,
        "status": _status_label(app.status),
        "appliedAt": app.applied_at.strftime("%Y-%m-%d %H:%M"),
        "personal": _rows_from_payload(payload, "personal", step_data.get("personal", [])),
        "address": _rows_from_payload(payload, "address", step_data.get("address", [])),
        "academic": _rows_from_payload(payload, "academic", step_data.get("academic", [])),
        "college": _rows_from_payload(payload, "college", step_data.get("college", [])),
        "bank": _rows_from_payload(payload, "bank", step_data.get("bank", [])),
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
