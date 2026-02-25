import csv
import io
import json
import zipfile
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.models import Application, ChatMessage, DocumentRule, UserDocument, UserProfile, Vacancy
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
    for field_name in required_profile_fields or []:
        clean_field = str(field_name or "").strip()
        if not clean_field:
            continue
        merged_items.append(f"DATA|{clean_field}")

    for idx, doc_name in enumerate(merged_items):
        doc_kind, clean_name = _parse_required_doc_name(doc_name)
        if not clean_name:
            continue
        key = clean_name.lower()
        has_data_duplicate = key in existing_master or any(
            key in k or k in key for k in existing_master.keys()
        )
        if doc_kind == "Data" and has_data_duplicate:
            continue
        value = available.get(key, "")
        if not value:
            for title, url in available.items():
                if key in title or title in key:
                    value = url
                    break
        if doc_kind in {"Document", "Photo"} and value:
            continue
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
    return render(
        request,
        "portal_main/student_services.html",
        {
            "profile": profile,
            "service_cards": service_cards,
            "my_applications": my_applications,
            "is_admin_user": _can_access_admin(request),
        },
    )


@login_required
def apply_student_service(request, vacancy_id):
    if request.method != "POST":
        return redirect("student_services_dashboard")

    vacancy = get_object_or_404(
        Vacancy,
        id=vacancy_id,
        is_active=True,
        category=Vacancy.CATEGORY_STUDENT,
    )

    request.session["pending_form_apply"] = {
        "kind": "student",
        "vacancy_id": vacancy.id,
        "title": vacancy.title,
        "organization": vacancy.organization,
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

    return render(
        request,
        "portal_main/dashboard.html",
        {
            "profile": profile,
            "vacancy_cards": vacancy_cards,
            "my_applications": my_applications,
            "is_admin_user": _can_access_admin(request),
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
    request.session["pending_form_apply"] = {
        "kind": "government",
        "vacancy_id": vacancy.id,
        "title": vacancy.title,
        "organization": vacancy.organization,
    }
    return redirect("confirm_send_to_admin")


@login_required
def confirm_send_to_admin(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    pending = request.session.get("pending_form_apply")
    if not pending:
        messages.info(request, "Pehle koi form select karke Apply click karo.")
        return redirect("role_select")
    vacancy = get_object_or_404(Vacancy, id=pending.get("vacancy_id"))

    if request.method == "POST":
        selected_steps = request.POST.getlist("steps")
        if not selected_steps:
            messages.error(request, "Kam se kam ek data step select karo.")
            return redirect("confirm_send_to_admin")

        step_data = _profile_step_data(profile)
        required_docs = _inject_required_docs_rows(profile, vacancy, step_data)
        required_doc_rows = _build_required_doc_rows(
            profile,
            required_docs,
            step_data=step_data,
            required_profile_fields=vacancy.required_profile_fields,
        )
        payload = {}
        for step_key, _ in PROFILE_DATA_STEPS:
            if step_key not in selected_steps:
                continue
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
            posted_value = request.POST.get(row["input_name"], row["value"] or "")
            uploaded = request.FILES.get(row["file_input_name"])
            if uploaded:
                file_url = _save_profile_document(profile, row["label"], uploaded)
                posted_value = file_url or posted_value or "Uploaded"
            selected_vac_docs.append({"label": row["label"], "value": posted_value})
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

        request.session.pop("pending_form_apply", None)
        if pending.get("kind") == "student":
            messages.success(request, "Student form data admin ko send kar diya gaya.")
            return redirect("student_services_dashboard")

        if created:
            messages.success(request, "Government form data admin ko send kar diya gaya.")
        else:
            messages.success(request, "Government form request update karke admin ko resend kar diya gaya.")
        return redirect("dashboard")

    step_data = _profile_step_data(profile)
    required_docs = _inject_required_docs_rows(profile, vacancy, step_data)
    required_doc_rows = _build_required_doc_rows(
        profile,
        required_docs,
        step_data=step_data,
        required_profile_fields=vacancy.required_profile_fields,
    )
    selected_default = [key for key, _ in PROFILE_DATA_STEPS]
    step_cards = []
    for key, label in PROFILE_DATA_STEPS:
        rows = step_data.get(key, [])
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

    context = {
        "applications": applications,
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
def user_chat(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    messages_qs = _decorate_chat_messages(profile.chat_messages.all())

    if request.method == "POST":
        if not profile.chat_enabled:
            messages.error(request, "Admin ne abhi chat enable nahi kiya hai.")
            return redirect("user_chat")
        message_text = request.POST.get("message", "").strip()
        attachment = request.FILES.get("attachment")
        if not message_text and not attachment:
            messages.error(request, "Message ya attachment bhejo.")
            return redirect("user_chat")
        ChatMessage.objects.create(
            profile=profile,
            from_admin=False,
            message=message_text,
            attachment=attachment,
        )
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
    profile = get_object_or_404(UserProfile, id=request.POST.get("profile_id"))
    message_text = request.POST.get("message", "").strip()
    attachment = request.FILES.get("attachment")
    if not message_text and not attachment:
        messages.error(request, "Message ya attachment bhejo.")
        return redirect(f"{reverse('admin_chat')}?profile_id={profile.id}")
    ChatMessage.objects.create(
        profile=profile,
        from_admin=True,
        message=message_text,
        attachment=attachment,
    )
    messages.success(request, "Reply send ho gayi.")
    return redirect(f"{reverse('admin_chat')}?profile_id={profile.id}")


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
    msg.delete()
    messages.success(request, "Chat message remove ho gaya.")
    return redirect(f"{reverse('admin_chat')}?profile_id={profile_id}")


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
        messages.warning(request, "Is option par applications hain, isliye inactive kiya gaya.")
        if option_scope in {Vacancy.CATEGORY_GOVERNMENT, Vacancy.CATEGORY_STUDENT}:
            return redirect("admin_option_control", category=option_scope)
        return redirect("admin_applicants")

    vacancy.delete()
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
        messages.warning(request, f"Application #{app.id} cancel kar di gayi.")
    else:
        new_status = request.POST.get("status", Application.STATUS_PENDING)
        valid_values = {value for value, _ in Application.STATUS_CHOICES}
        if new_status in valid_values:
            app.status = new_status
            if new_status != Application.STATUS_CANCELLED:
                app.cancelled_at = None
            messages.success(request, f"Application #{app.id} status update ho gaya.")
    app.save(update_fields=["status", "cancelled_at", "updated_at"])
    return redirect("admin_applicants")


@login_required
def admin_remove_application(request, application_id):
    if request.method != "POST" or not _can_access_admin(request):
        return redirect("admin_applicants")
    app = get_object_or_404(Application, id=application_id)
    app.delete()
    messages.success(request, f"Application #{application_id} remove ho gayi.")
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
