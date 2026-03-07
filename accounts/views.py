from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.files.storage import default_storage
from django.db import OperationalError, ProgrammingError
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import io
import zipfile

from .models import DocumentRule, MasterDataField, PortalNews, UserDocument, UserProfile, WalletTransaction
from PIL import Image, ImageOps


STEPS = [
    ("personal", "Personal Details", "master_data_personal"),
    ("address", "Address Details", "master_data_address"),
    ("academic", "Academic Details", "master_data_academic"),
    ("college", "College Details", "master_data_college"),
    ("bank", "Bank Details", "master_data_bank"),
    ("documents", "Document Upload", "master_data_documents"),
]

DOCUMENT_SPECS = [
    ("aadhaar_card", "Aadhaar Card", "Yes"),
    ("tenth_marksheet", "10th Marksheet", "Yes"),
    ("twelfth_marksheet", "12th Marksheet", "Yes"),
    ("caste_certificate", "Caste Certificate", "If applicable"),
    ("income_certificate", "Income Certificate", "If applicable"),
    ("domicile_certificate", "Domicile Certificate", "No"),
]

STEP_EXTRA_META = {
    "personal": ("personal_extra_label[]", "personal_extra_value[]", "personal_extra_permanent[]", "personal_extra_rows"),
    "address": ("address_extra_label[]", "address_extra_value[]", "address_extra_permanent[]", "address_extra_rows"),
    "academic": ("academic_extra_label[]", "academic_extra_value[]", "academic_extra_permanent[]", "academic_extra_rows"),
    "college": ("college_extra_label[]", "college_extra_value[]", "college_extra_permanent[]", "college_extra_rows"),
    "bank": ("bank_extra_label[]", "bank_extra_value[]", "bank_extra_permanent[]", "bank_extra_rows"),
}

MASK_AFTER_HOURS = 24
UNMASK_WINDOW_MINUTES = 10
UNMASK_DAILY_LIMIT = 2


def _mask_text_value(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 2:
        return "***"
    if len(text) <= 6:
        return text[:1] + "***"
    return text[:2] + "***" + text[-2:]


def _is_master_data_masked(profile):
    # Master data masking फिलहाल user request ke hisab se disabled hai.
    return False


def _mark_master_data_saved(profile):
    profile.master_data_last_saved_at = timezone.now()
    profile.save(update_fields=["master_data_last_saved_at"])


def _grant_unmask_window(profile):
    now = timezone.now()
    today = timezone.localdate()
    if profile.master_data_unmask_date != today:
        profile.master_data_unmask_date = today
        profile.master_data_unmask_count = 0
    if profile.master_data_unmask_count >= UNMASK_DAILY_LIMIT:
        return False, 0
    profile.master_data_unmask_count += 1
    profile.master_data_unmask_until = now + timedelta(minutes=UNMASK_WINDOW_MINUTES)
    profile.save(
        update_fields=[
            "master_data_unmask_date",
            "master_data_unmask_count",
            "master_data_unmask_until",
        ]
    )
    return True, max(UNMASK_DAILY_LIMIT - profile.master_data_unmask_count, 0)


def _mask_profile_for_display(profile):
    text_fields = [
        "full_name", "father_name", "mother_name", "category", "mobile", "email", "aadhar", "samagra_id",
        "present_state", "present_district", "present_city", "present_pincode", "present_address",
        "permanent_state", "permanent_district", "permanent_pincode", "permanent_full_address", "permanent_address",
        "district", "state", "pincode",
        "tenth_board", "tenth_roll_number", "tenth_percentage", "tenth_result",
        "twelfth_board", "twelfth_roll_number", "twelfth_percentage", "twelfth_result", "graduation",
        "college_name", "university_name", "course", "year_semester", "enrollment_number",
        "account_holder_name", "bank_name", "account_number", "ifsc_code", "branch_name",
    ]
    for field_name in text_fields:
        setattr(profile, field_name, _mask_text_value(getattr(profile, field_name, "")))
    profile.dob = None
    for list_field in [
        "personal_extra_rows",
        "address_extra_rows",
        "academic_extra_rows",
        "college_extra_rows",
        "bank_extra_rows",
    ]:
        current = getattr(profile, list_field, []) or []
        masked_rows = []
        for row in current:
            masked_rows.append(
                {
                    "label": str((row or {}).get("label", "")).strip(),
                    "value": _mask_text_value((row or {}).get("value", "")),
                    "is_permanent": bool((row or {}).get("is_permanent")),
                }
            )
        setattr(profile, list_field, masked_rows)


def _reject_if_masked_post(request, profile):
    return False


def _norm_label(value):
    return str(value or "").strip().lower()


def _active_master_fields(step_key, field_kind):
    return list(
        MasterDataField.objects.filter(
            step=step_key,
            field_kind=field_kind,
            is_active=True,
        ).order_by("display_order", "label", "id")
    )


def _step_extra_values_map(profile_rows):
    out = {}
    for row in profile_rows or []:
        label = str((row or {}).get("label", "")).strip()
        if not label:
            continue
        out[_norm_label(label)] = str((row or {}).get("value", "")).strip()
    return out


def _merge_admin_and_custom_rows(request, step_key, profile):
    meta = STEP_EXTRA_META.get(step_key)
    if not meta:
        return []
    label_key, value_key, permanent_key, profile_attr = meta
    admin_fields = _active_master_fields(step_key, MasterDataField.KIND_TEXT)
    admin_labels = {_norm_label(field.label) for field in admin_fields}

    merged = []
    for field in admin_fields:
        merged.append(
            {
                "label": field.label,
                "value": request.POST.get(f"{step_key}_admin_value__{field.id}", "").strip(),
                "is_permanent": True,
            }
        )

    extra_labels = request.POST.getlist(label_key)
    extra_values = request.POST.getlist(value_key)
    extra_permanent = request.POST.getlist(permanent_key)
    custom_rows = _build_extra_rows(extra_labels, extra_values, extra_permanent)
    for row in custom_rows:
        key = _norm_label((row or {}).get("label", ""))
        if key and key in admin_labels:
            continue
        merged.append(row)

    # Keep old permanent rows when admin field is currently removed/inactive.
    existing_rows = getattr(profile, profile_attr, []) or []
    merged_keys = {_norm_label((r or {}).get("label", "")) for r in merged}
    for row in existing_rows:
        key = _norm_label((row or {}).get("label", ""))
        if not key:
            continue
        if not (row or {}).get("is_permanent"):
            continue
        if key in merged_keys:
            continue
        merged.append(
            {
                "label": str((row or {}).get("label", "")).strip(),
                "value": str((row or {}).get("value", "")).strip(),
                "is_permanent": True,
            }
        )
    return merged


def _document_specs_for_profile(profile):
    specs = [{"field_name": field_name, "title": title, "required": required} for field_name, title, required in DOCUMENT_SPECS]
    seen = {_norm_label(title) for _, title, _ in DOCUMENT_SPECS}
    for field in _active_master_fields(MasterDataField.STEP_DOCUMENTS, MasterDataField.KIND_DOCUMENT):
        key = _norm_label(field.label)
        if not key or key in seen:
            continue
        specs.append({"field_name": f"admin_document_{field.id}", "title": field.label, "required": "Admin"})
        seen.add(key)
    return specs


def _normalize_doc_name(value):
    return str(value or "").strip().lower()


def _find_rule_for_title(title, rules):
    key = _normalize_doc_name(title)
    if not key:
        return None
    if key in rules:
        return rules[key]
    for name_key, rule in rules.items():
        if key in name_key or name_key in key:
            return rule
    return None


def _validate_file_rule(title, file_obj, rules):
    rule = _find_rule_for_title(title, rules)
    if not rule or not file_obj:
        return None
    size_kb = max(int(file_obj.size / 1024), 1)
    if rule.exact_kb:
        if size_kb != rule.exact_kb:
            return f"{title}: size exactly {rule.exact_kb} KB hona chahiye. Current {size_kb} KB."
    elif size_kb < rule.min_kb or size_kb > rule.max_kb:
        return f"{title}: size {size_kb} KB allowed range {rule.min_kb}-{rule.max_kb} KB."
    kind = rule.kind
    if kind == DocumentRule.KIND_IMAGE:
        if not (file_obj.content_type or "").startswith("image/"):
            return f"{title}: only image file allowed."
    if kind == DocumentRule.KIND_PDF:
        if (file_obj.content_type or "") != "application/pdf":
            return f"{title}: only PDF allowed."
    if (rule.exact_width or rule.exact_height) and (file_obj.content_type or "").startswith("image/"):
        try:
            pos = file_obj.tell()
        except Exception:
            pos = None
        try:
            with Image.open(file_obj) as img:
                w, h = img.size
            if rule.exact_width and w != rule.exact_width:
                return f"{title}: width exactly {rule.exact_width}px hona chahiye. Current {w}px."
            if rule.exact_height and h != rule.exact_height:
                return f"{title}: height exactly {rule.exact_height}px hona chahiye. Current {h}px."
        except Exception:
            return f"{title}: image size read nahi hua."
        finally:
            try:
                if pos is not None:
                    file_obj.seek(pos)
                else:
                    file_obj.seek(0)
            except Exception:
                pass
    return None


def _step_context(profile, current_step_key):
    step_keys = [key for key, _, _ in STEPS]
    current_index = step_keys.index(current_step_key)
    prev_url_name = STEPS[current_index - 1][2] if current_index > 0 else None
    next_url_name = STEPS[current_index + 1][2] if current_index < len(STEPS) - 1 else None
    extra_maps = {
        "personal": _step_extra_values_map(profile.personal_extra_rows),
        "address": _step_extra_values_map(profile.address_extra_rows),
        "academic": _step_extra_values_map(profile.academic_extra_rows),
        "college": _step_extra_values_map(profile.college_extra_rows),
        "bank": _step_extra_values_map(profile.bank_extra_rows),
    }
    admin_text_fields = []
    if current_step_key in extra_maps:
        for field in _active_master_fields(current_step_key, MasterDataField.KIND_TEXT):
            admin_text_fields.append(
                {
                    "id": field.id,
                    "label": field.label,
                    "value": extra_maps[current_step_key].get(_norm_label(field.label), ""),
                }
            )
    masking_active = _is_master_data_masked(profile)
    if masking_active:
        _mask_profile_for_display(profile)
        for item in admin_text_fields:
            item["value"] = _mask_text_value(item.get("value"))
    now = timezone.now()
    reveal_until = getattr(profile, "master_data_unmask_until", None)
    reveal_active = bool(reveal_until and now < reveal_until)
    today = timezone.localdate()
    used_today = profile.master_data_unmask_count if profile.master_data_unmask_date == today else 0
    unmask_remaining_today = max(UNMASK_DAILY_LIMIT - used_today, 0)

    return {
        "profile": profile,
        "current_step": current_step_key,
        "current_step_number": current_index + 1,
        "total_steps": len(STEPS),
        "progress_percent": int(((current_index + 1) / len(STEPS)) * 100),
        "prev_url_name": prev_url_name,
        "next_url_name": next_url_name,
        "steps": [
            {
                "key": key,
                "label": label,
                "url_name": url_name,
                "active": key == current_step_key,
            }
            for key, label, url_name in STEPS
        ],
        "personal_extra_rows": profile.personal_extra_rows or [],
        "address_extra_rows": profile.address_extra_rows or [],
        "academic_extra_rows": profile.academic_extra_rows or [],
        "college_extra_rows": profile.college_extra_rows or [],
        "bank_extra_rows": profile.bank_extra_rows or [],
        "admin_text_fields": admin_text_fields,
        "masking_active": masking_active,
        "reveal_active": reveal_active,
        "reveal_until": reveal_until,
        "unmask_remaining_today": unmask_remaining_today,
    }


def _news_for_master_page():
    try:
        qs = PortalNews.objects.filter(is_active=True).filter(
            Q(target_portal=PortalNews.TARGET_ALL)
            | Q(target_portal=PortalNews.TARGET_GOVERNMENT)
            | Q(target_portal=PortalNews.TARGET_STUDENT)
        )
        qs.exists()
        return qs[:6]
    except (OperationalError, ProgrammingError):
        # News table migrate na ho to page normal render hona chahiye.
        return PortalNews.objects.none()


def _mobile_key(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _find_profile_by_mobile(raw_mobile):
    key = _mobile_key(raw_mobile)
    if not key:
        return None
    for profile in UserProfile.objects.select_related("user").exclude(mobile=""):
        if _mobile_key(profile.mobile) == key:
            return profile
    return None


def _build_extra_rows(labels, values, permanents=None):
    output = []
    permanents = permanents or []
    max_len = max(len(labels), len(values))
    for idx in range(max_len):
        label = labels[idx].strip() if idx < len(labels) else ""
        value = values[idx].strip() if idx < len(values) else ""
        is_permanent = idx < len(permanents) and str(permanents[idx]).strip() in {"1", "true", "on", "yes"}
        if not label and not value:
            continue
        output.append({"label": label, "value": value, "is_permanent": is_permanent})
    return output


def _safe_media_url(file_field):
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


def _profile_image_url(profile, title, profile_field):
    direct = _safe_media_url(profile_field)
    if direct:
        return direct
    doc = profile.documents.filter(title__iexact=title).order_by("-id").first()
    if not doc:
        return ""
    return _safe_media_url(doc.file)


def _document_file_info(profile, title):
    doc = profile.documents.filter(title__iexact=title).order_by("-id").first()
    if not doc:
        return {"uploaded": False, "url": "", "name": ""}
    url = _safe_media_url(doc.file)
    if not url:
        return {"uploaded": False, "url": "", "name": ""}
    name = (doc.file.name or "").split("/")[-1]
    return {"uploaded": True, "url": url, "name": name}


def login_view(request):
    if request.user.is_authenticated:
        return redirect("role_select")
    if request.method == "POST":
        user = authenticate(
            request,
            username=request.POST.get("username"),
            password=request.POST.get("password"),
        )
        if user:
            login(request, user)
            UserProfile.objects.get_or_create(user=user)
            profile = UserProfile.objects.filter(user=user).first()
            if profile and profile.full_name:
                return redirect("role_select")
            return redirect("master_data_option")
        messages.error(request, "Username ya Password galat hai!")
    return render(request, "accounts/login.html")


def register_view(request):
    if request.user.is_authenticated:
        return redirect("role_select")
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        mobile = request.POST.get("mobile", "").strip()
        password = request.POST.get("password", "")
        confirm = request.POST.get("confirm", "")
        mobile_norm = _mobile_key(mobile)
        if not username or not mobile_norm or not password:
            messages.error(request, "Username, mobile number aur password required hai.")
        elif password != confirm:
            messages.error(request, "Password match nahi kiya!")
        elif User.objects.filter(username=username).exists():
            messages.error(request, "Username pehle se exist karta hai!")
        elif _find_profile_by_mobile(mobile_norm):
            messages.error(request, "Ye mobile number already registered hai.")
        else:
            user = User.objects.create_user(username=username, password=password)
            UserProfile.objects.create(user=user, mobile=mobile_norm)
            login(request, user)
            messages.success(request, "Account ban gaya! Ab Master Data bharo.")
            return redirect("master_data_option")
    return render(request, "accounts/register.html")


def logout_view(request):
    logout(request)
    return redirect("login")


def forgot_password_view(request):
    if request.user.is_authenticated:
        return redirect("role_select")
    if request.method == "POST":
        mobile = request.POST.get("mobile", "").strip()
        new_password = request.POST.get("new_password", "")
        confirm = request.POST.get("confirm_password", "")
        if not mobile or not new_password:
            messages.error(request, "Mobile number aur new password required hai.")
            return redirect("forgot_password")
        if new_password != confirm:
            messages.error(request, "New password aur confirm password same hona chahiye.")
            return redirect("forgot_password")
        profile = _find_profile_by_mobile(mobile)
        if not profile or not profile.user:
            messages.error(request, "Mobile number register nahi hai.")
            return redirect("forgot_password")

        user = profile.user
        user.set_password(new_password)
        user.save(update_fields=["password"])
        return render(
            request,
            "accounts/forgot_password_success.html",
            {
                "username": user.username,
            },
        )
    return render(request, "accounts/forgot_password.html")


@login_required
def master_data_option_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            return redirect("master_data_personal")
        return redirect("role_select")
    return render(
        request,
        "accounts/master_data_option.html",
        {
            "has_profile": bool(profile.full_name),
            "news_items": _news_for_master_page(),
        },
    )


@login_required
def wallet_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
        amount_raw = request.POST.get("amount", "").strip()
        note = request.POST.get("note", "").strip()
        try:
            amount = Decimal(amount_raw)
            if amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            messages.error(request, "Valid amount dalo.")
            return redirect("wallet")
        WalletTransaction.objects.create(
            profile=profile,
            tx_type=WalletTransaction.TYPE_ADD,
            amount=amount.quantize(Decimal("0.01")),
            note=note,
        )
        messages.success(request, "Wallet me amount add ho gaya.")
        return redirect("wallet")

    history = profile.wallet_transactions.all()
    balance = WalletTransaction.balance_for_profile(profile)
    return render(
        request,
        "accounts/wallet.html",
        {
            "profile": profile,
            "history": history,
            "balance": balance,
        },
    )


@login_required
def document_converter_view(request):
    return render(request, "accounts/document_converter.html")


def _encode_with_pillow(image, mime_type, quality):
    output = io.BytesIO()
    save_kwargs = {"optimize": True}
    if mime_type == "image/jpeg":
        save_kwargs.update({"format": "JPEG", "quality": quality, "progressive": True})
    elif mime_type == "image/webp":
        save_kwargs.update({"format": "WEBP", "quality": quality, "method": 6})
    else:
        save_kwargs.update({"format": "PNG", "compress_level": 9})
    image.save(output, **save_kwargs)
    return output.getvalue()


def _target_dimensions(src_w, src_h, req_w, req_h):
    if req_w and req_h:
        return max(req_w, 1), max(req_h, 1)
    if req_w and not req_h:
        ratio = req_w / max(src_w, 1)
        return max(req_w, 1), max(int(src_h * ratio), 1)
    if req_h and not req_w:
        ratio = req_h / max(src_h, 1)
        return max(int(src_w * ratio), 1), max(req_h, 1)
    return src_w, src_h


def _flatten_on_white(image):
    # Keep transparent PNG/WebP background clean in JPEG/WebP output.
    if image.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", image.size, (255, 255, 255))
        alpha = image.getchannel("A")
        bg.paste(image.convert("RGB"), mask=alpha)
        return bg
    if image.mode == "P":
        if "transparency" in image.info:
            rgba = image.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba.convert("RGB"), mask=rgba.getchannel("A"))
            return bg
        return image.convert("RGB")
    if image.mode not in ("RGB", "L"):
        return image.convert("RGB")
    return image


def _resize_no_stretch(image, req_w, req_h):
    src_w, src_h = image.size
    out_w, out_h = _target_dimensions(src_w, src_h, req_w, req_h)
    if (out_w, out_h) == (src_w, src_h):
        return image

    # If both dimensions are given, fit inside target box and pad on white.
    if req_w and req_h:
        scale = min(req_w / max(src_w, 1), req_h / max(src_h, 1))
        fit_w = max(int(src_w * scale), 1)
        fit_h = max(int(src_h * scale), 1)
        fitted = image.resize((fit_w, fit_h), Image.Resampling.LANCZOS)
        if fitted.mode not in ("RGB", "L"):
            fitted = _flatten_on_white(fitted)
        if fitted.mode == "L":
            canvas = Image.new("L", (req_w, req_h), 255)
        else:
            canvas = Image.new("RGB", (req_w, req_h), (255, 255, 255))
        x = (req_w - fit_w) // 2
        y = (req_h - fit_h) // 2
        canvas.paste(fitted, (x, y))
        return canvas

    # Single-side resize keeps aspect ratio naturally.
    return image.resize((out_w, out_h), Image.Resampling.LANCZOS)


def _clamp_crop_box(img_w, img_h, x, y, w, h):
    x = max(0, min(x, img_w - 1))
    y = max(0, min(y, img_h - 1))
    w = max(1, min(w, img_w - x))
    h = max(1, min(h, img_h - y))
    return x, y, w, h


def _open_image_from_upload(file_obj):
    image = Image.open(file_obj)
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", image.size, (255, 255, 255))
        if image.mode == "P":
            image = image.convert("RGBA")
        if image.mode in ("RGBA", "LA"):
            alpha = image.getchannel("A")
            bg.paste(image.convert("RGB"), mask=alpha)
        else:
            bg.paste(image.convert("RGB"))
        return bg
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


@login_required
@require_POST
def document_converter_process_view(request):
    file_obj = request.FILES.get("file")
    if not file_obj:
        return JsonResponse({"error": "file missing"}, status=400)
    if not (file_obj.content_type or "").startswith("image/"):
        return JsonResponse({"error": "Only image conversion supported."}, status=400)

    try:
        target_kb = max(int(request.POST.get("target_kb", "200")), 10)
    except ValueError:
        target_kb = 200
    target_bytes = max((target_kb - 1) * 1024, 1024)
    out_type = request.POST.get("out_type", "keep")
    quality_lock = request.POST.get("quality_lock", "1") == "1"
    strict_kb = request.POST.get("strict_kb", "1") == "1"
    try:
        req_w = int(request.POST.get("width", "0") or "0")
    except ValueError:
        req_w = 0
    try:
        req_h = int(request.POST.get("height", "0") or "0")
    except ValueError:
        req_h = 0
    crop_mode = request.POST.get("crop_mode", "none")
    try:
        crop_x = int(request.POST.get("crop_x", "0") or "0")
    except ValueError:
        crop_x = 0
    try:
        crop_y = int(request.POST.get("crop_y", "0") or "0")
    except ValueError:
        crop_y = 0
    try:
        crop_w = int(request.POST.get("crop_w", "0") or "0")
    except ValueError:
        crop_w = 0
    try:
        crop_h = int(request.POST.get("crop_h", "0") or "0")
    except ValueError:
        crop_h = 0

    mime_type = out_type
    if out_type == "keep":
        mime_type = file_obj.content_type if file_obj.content_type in {"image/jpeg", "image/png", "image/webp"} else "image/jpeg"
    if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        mime_type = "image/jpeg"

    image = Image.open(file_obj)
    image = ImageOps.exif_transpose(image)
    if mime_type in {"image/jpeg", "image/webp"}:
        image = _flatten_on_white(image)
    if mime_type == "image/png" and image.mode not in ("RGB", "RGBA", "L", "LA"):
        image = image.convert("RGBA")

    # Optional crop before resize/compress to preserve quality better.
    if crop_mode in {"center", "custom"}:
        img_w, img_h = image.size
        if crop_mode == "center":
            target_ratio = 1.0
            if crop_w > 0 and crop_h > 0:
                target_ratio = crop_w / max(crop_h, 1)
            src_ratio = img_w / max(img_h, 1)
            if src_ratio > target_ratio:
                new_w = int(img_h * target_ratio)
                new_h = img_h
            else:
                new_w = img_w
                new_h = int(img_w / max(target_ratio, 0.01))
            start_x = max((img_w - new_w) // 2, 0)
            start_y = max((img_h - new_h) // 2, 0)
            image = image.crop((start_x, start_y, start_x + new_w, start_y + new_h))
        else:
            if crop_w > 0 and crop_h > 0:
                x, y, w, h = _clamp_crop_box(img_w, img_h, crop_x, crop_y, crop_w, crop_h)
                image = image.crop((x, y, x + w, y + h))

    image = _resize_no_stretch(image, req_w, req_h)

    best_bytes = b""
    best_size = None
    work_image = image

    if quality_lock:
        # Keep visual quality high and avoid aggressive re-compression/downscale.
        if mime_type == "image/png":
            best_bytes = _encode_with_pillow(work_image, mime_type, 100)
            best_size = len(best_bytes)
        else:
            for q in (98, 96, 94):
                encoded = _encode_with_pillow(work_image, mime_type, q)
                if best_size is None or len(encoded) < best_size:
                    best_bytes = encoded
                    best_size = len(encoded)
                if len(encoded) <= target_bytes:
                    best_bytes = encoded
                    best_size = len(encoded)
                    break
    else:
        min_quality = 62
        for _ in range(10):
            local_best = None
            if mime_type == "image/png":
                png_bytes = _encode_with_pillow(work_image, mime_type, 100)
                local_best = png_bytes
            else:
                for q in range(96, min_quality - 1, -2):
                    encoded = _encode_with_pillow(work_image, mime_type, q)
                    if local_best is None or len(encoded) < len(local_best):
                        local_best = encoded
                    if len(encoded) <= target_bytes:
                        local_best = encoded
                        break

            current_size = len(local_best or b"")
            if best_size is None or (current_size and current_size < best_size):
                best_size = current_size
                best_bytes = local_best or b""
            if current_size and current_size <= target_bytes:
                best_bytes = local_best or b""
                best_size = current_size
                break
            if not strict_kb and best_bytes:
                break

            w, h = work_image.size
            next_w = max(int(w * 0.95), 120)
            next_h = max(int(h * 0.95), 120)
            if (next_w, next_h) == (w, h):
                break
            work_image = work_image.resize((next_w, next_h), Image.Resampling.LANCZOS)

    if not best_bytes:
        return JsonResponse({"error": "Unable to convert image."}, status=500)

    ext = "jpg"
    if mime_type == "image/png":
        ext = "png"
    elif mime_type == "image/webp":
        ext = "webp"
    base_name = (file_obj.name or "image").rsplit(".", 1)[0]
    download_name = f"{base_name}_converted.{ext}"

    response = HttpResponse(best_bytes, content_type=mime_type)
    response["Content-Disposition"] = f'attachment; filename="{download_name}"'
    response["X-Original-Size"] = str(file_obj.size)
    response["X-Converted-Size"] = str(len(best_bytes))
    response["X-Output-Width"] = str(work_image.size[0])
    response["X-Output-Height"] = str(work_image.size[1])
    response["X-Output-Name"] = download_name
    return response


@login_required
@require_POST
def document_converter_images_to_pdf_view(request):
    image_files = request.FILES.getlist("images")
    if not image_files:
        return JsonResponse({"error": "Kam se kam 1 image select karo."}, status=400)

    opened = []
    for f in image_files:
        if not (f.content_type or "").startswith("image/"):
            return JsonResponse({"error": f"{f.name}: sirf image files allowed hain."}, status=400)
        try:
            opened.append(_open_image_from_upload(f))
        except Exception:
            return JsonResponse({"error": f"{f.name}: image read nahi hui."}, status=400)

    if not opened:
        return JsonResponse({"error": "Valid images nahi mili."}, status=400)

    pdf_buffer = io.BytesIO()
    first, rest = opened[0], opened[1:]
    first.save(pdf_buffer, format="PDF", save_all=True, append_images=rest)
    pdf_bytes = pdf_buffer.getvalue()
    for im in opened:
        try:
            im.close()
        except Exception:
            pass

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="merged_documents.pdf"'
    response["X-Output-Name"] = "merged_documents.pdf"
    response["X-Output-Size"] = str(len(pdf_bytes))
    return response


@login_required
@require_POST
def document_converter_pdf_to_images_view(request):
    pdf_file = request.FILES.get("pdf")
    if not pdf_file:
        return JsonResponse({"error": "PDF file missing."}, status=400)
    if (pdf_file.content_type or "").lower() != "application/pdf":
        return JsonResponse({"error": "Sirf PDF file upload karo."}, status=400)

    try:
        import fitz  # PyMuPDF
    except Exception:
        return JsonResponse(
            {
                "error": "PDF to image ke liye server dependency missing hai (PyMuPDF). Install hone ke baad ye feature chalega."
            },
            status=400,
        )

    try:
        data = pdf_file.read()
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return JsonResponse({"error": "PDF read nahi ho payi."}, status=400)

    if doc.page_count == 0:
        return JsonResponse({"error": "PDF me pages nahi mile."}, status=400)

    image_format = request.POST.get("format", "jpg").strip().lower()
    if image_format not in {"jpg", "png"}:
        image_format = "jpg"
    ext = "jpg" if image_format == "jpg" else "png"

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx in range(doc.page_count):
            page = doc.load_page(idx)
            pix = page.get_pixmap(alpha=False)
            if ext == "jpg":
                img_bytes = pix.tobytes("jpeg")
            else:
                img_bytes = pix.tobytes("png")
            zf.writestr(f"page_{idx + 1}.{ext}", img_bytes)
    doc.close()

    zip_bytes = zip_buffer.getvalue()
    response = HttpResponse(zip_bytes, content_type="application/zip")
    response["Content-Disposition"] = 'attachment; filename="pdf_pages_images.zip"'
    response["X-Output-Name"] = "pdf_pages_images.zip"
    response["X-Output-Size"] = str(len(zip_bytes))
    return response


@login_required
@require_POST
def document_converter_ocr_view(request):
    src_file = request.FILES.get("file")
    if not src_file:
        return JsonResponse({"error": "File missing."}, status=400)

    try:
        import pytesseract
    except Exception:
        return JsonResponse(
            {"error": "OCR dependency missing hai (pytesseract). Install hone ke baad OCR chalega."},
            status=400,
        )

    image = None
    if (src_file.content_type or "").startswith("image/"):
        try:
            image = _open_image_from_upload(src_file)
        except Exception:
            return JsonResponse({"error": "Image read nahi hui."}, status=400)
    elif (src_file.content_type or "").lower() == "application/pdf":
        try:
            import fitz
        except Exception:
            return JsonResponse({"error": "PDF OCR ke liye PyMuPDF required hai."}, status=400)
        try:
            data = src_file.read()
            doc = fitz.open(stream=data, filetype="pdf")
            if doc.page_count == 0:
                return JsonResponse({"error": "PDF me pages nahi mile."}, status=400)
            pix = doc.load_page(0).get_pixmap(alpha=False)
            image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
            doc.close()
        except Exception:
            return JsonResponse({"error": "PDF read nahi hui."}, status=400)
    else:
        return JsonResponse({"error": "Image ya PDF upload karo."}, status=400)

    try:
        lang = request.POST.get("lang", "eng").strip() or "eng"
        text = pytesseract.image_to_string(image, lang=lang)
    except Exception:
        return JsonResponse({"error": "OCR process fail ho gaya."}, status=500)

    return JsonResponse({"text": text or "", "chars": len(text or "")})


@login_required
def role_select_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    return render(
        request,
        "accounts/role_select.html",
        {
            "profile": profile,
            "is_admin_user": bool(request.user.is_superuser or request.user.is_staff),
        },
    )


@login_required
def master_data_view(request):
    return redirect("master_data_personal")


@login_required
def master_data_personal_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
        if _reject_if_masked_post(request, profile):
            return redirect("master_data_documents")
        p = request.POST
        profile.full_name = p.get("full_name", "")
        profile.father_name = p.get("father_name", "")
        profile.mother_name = p.get("mother_name", "")
        profile.dob = p.get("dob") or None
        profile.gender = p.get("gender", "")
        profile.category = p.get("category", "")
        profile.mobile = p.get("mobile", "")
        profile.email = p.get("email", "")
        profile.aadhar = p.get("aadhar", "")
        profile.samagra_id = p.get("samagra_id", "")
        profile.personal_extra_rows = _merge_admin_and_custom_rows(request, "personal", profile)
        profile.save()
        _mark_master_data_saved(profile)
        return redirect("master_data_address")
    return render(request, "accounts/master_data_step.html", _step_context(profile, "personal"))


@login_required
def master_data_address_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
        if _reject_if_masked_post(request, profile):
            return redirect("master_data_documents")
        p = request.POST
        profile.present_state = p.get("present_state", "")
        profile.present_district = p.get("present_district", "")
        profile.present_city = p.get("present_city", "")
        profile.present_pincode = p.get("present_pincode", "")
        profile.present_address = p.get("present_address", "")
        profile.permanent_same_as_present = p.get("same_as_present") == "on"

        if profile.permanent_same_as_present:
            profile.permanent_state = profile.present_state
            profile.permanent_district = profile.present_district
            profile.permanent_pincode = profile.present_pincode
            profile.permanent_full_address = profile.present_address
        else:
            profile.permanent_state = p.get("permanent_state", "")
            profile.permanent_district = p.get("permanent_district", "")
            profile.permanent_pincode = p.get("permanent_pincode", "")
            profile.permanent_full_address = p.get("permanent_full_address", "")

        # compatibility with existing fields
        profile.state = profile.present_state
        profile.district = profile.present_district
        profile.pincode = profile.present_pincode
        profile.permanent_address = profile.permanent_full_address or profile.present_address
        profile.address_extra_rows = _merge_admin_and_custom_rows(request, "address", profile)
        profile.save()
        _mark_master_data_saved(profile)
        return redirect("master_data_academic")
    return render(request, "accounts/master_data_step.html", _step_context(profile, "address"))


@login_required
def master_data_academic_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
        if _reject_if_masked_post(request, profile):
            return redirect("master_data_documents")
        p = request.POST
        profile.tenth_board = p.get("tenth_board", "")
        profile.tenth_roll_number = p.get("tenth_roll_number", "")
        profile.tenth_percentage = p.get("tenth_percentage", "")
        profile.tenth_result = profile.tenth_percentage
        profile.twelfth_board = p.get("twelfth_board", "")
        profile.twelfth_roll_number = p.get("twelfth_roll_number", "")
        profile.twelfth_percentage = p.get("twelfth_percentage", "")
        profile.twelfth_result = profile.twelfth_percentage
        profile.graduation = p.get("graduation", "")
        profile.academic_extra_rows = _merge_admin_and_custom_rows(request, "academic", profile)
        profile.save()
        _mark_master_data_saved(profile)
        return redirect("master_data_college")
    return render(request, "accounts/master_data_step.html", _step_context(profile, "academic"))


@login_required
def master_data_college_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
        if _reject_if_masked_post(request, profile):
            return redirect("master_data_documents")
        p = request.POST
        profile.college_name = p.get("college_name", "")
        profile.university_name = p.get("university_name", "")
        profile.course = p.get("course", "")
        profile.year_semester = p.get("year_semester", "")
        profile.enrollment_number = p.get("enrollment_number", "")
        profile.university = profile.university_name
        profile.college_extra_rows = _merge_admin_and_custom_rows(request, "college", profile)
        profile.save()
        _mark_master_data_saved(profile)
        return redirect("master_data_bank")
    return render(request, "accounts/master_data_step.html", _step_context(profile, "college"))


@login_required
def master_data_bank_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
        if _reject_if_masked_post(request, profile):
            return redirect("master_data_documents")
        p = request.POST
        profile.account_holder_name = p.get("account_holder_name", "")
        profile.bank_name = p.get("bank_name", "")
        profile.account_number = p.get("account_number", "")
        profile.ifsc_code = p.get("ifsc_code", "")
        profile.branch_name = p.get("branch_name", "")
        profile.aadhaar_linked = p.get("aadhaar_linked", "")
        profile.bank_extra_rows = _merge_admin_and_custom_rows(request, "bank", profile)
        profile.save()
        _mark_master_data_saved(profile)
        return redirect("master_data_documents")
    return render(request, "accounts/master_data_step.html", _step_context(profile, "bank"))


@login_required
def master_data_documents_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    rule_map = {_normalize_doc_name(r.name): r for r in DocumentRule.objects.filter(is_active=True)}
    document_specs = _document_specs_for_profile(profile)
    if request.method == "POST":
        if request.POST.get("action") == "reveal_10min":
            granted, remaining = _grant_unmask_window(profile)
            if granted:
                messages.success(
                    request,
                    f"Full data view 10 minute ke liye unlock ho gaya. Aaj ke remaining unlock: {remaining}.",
                )
            else:
                messages.error(request, "Aaj ka 2-time view limit complete ho gaya.")
            return redirect("master_data_personal")
        errors = []
        photo = request.FILES.get("passport_photo")
        signature = request.FILES.get("signature")
        # Passport photo/signature profile core fields hain:
        # inhe hard-block na karein, warna user ko lagta hai save nahi hua.
        if photo:
            err = _validate_file_rule("Passport Size Photo", photo, rule_map)
            if err:
                messages.warning(request, f"Photo rule warning: {err} (photo save continue hoga)")
        if signature:
            err = _validate_file_rule("Signature", signature, rule_map)
            if err:
                messages.warning(request, f"Signature rule warning: {err} (signature save continue hoga)")
        for spec in document_specs:
            field_name = spec["field_name"]
            title = spec["title"]
            file_obj = request.FILES.get(field_name)
            if not file_obj:
                continue
            err = _validate_file_rule(title, file_obj, rule_map)
            if err:
                errors.append(err)

        extra_titles = request.POST.getlist("doc_title[]")
        extra_files = request.FILES.getlist("doc_file[]")
        for idx, file_obj in enumerate(extra_files):
            title = "Additional Document"
            if idx < len(extra_titles) and extra_titles[idx].strip():
                title = extra_titles[idx].strip()
            err = _validate_file_rule(title, file_obj, rule_map)
            if err:
                errors.append(err)

        if errors:
            for msg in errors:
                messages.error(request, msg)
            return redirect("master_data_documents")

        if photo:
            profile.photo = photo
        if signature:
            profile.signature = signature
        profile.save()
        # Keep mirrored photo/signature entries in UserDocument so preview areas
        # that read documents also always show latest uploaded image.
        if photo:
            photo_doc = profile.documents.filter(title__iexact="Passport Photo").first()
            if photo_doc:
                photo_doc.file = photo
                photo_doc.save()
            else:
                UserDocument.objects.create(profile=profile, title="Passport Photo", file=photo)
        if signature:
            sign_doc = profile.documents.filter(title__iexact="Signature").first()
            if sign_doc:
                sign_doc.file = signature
                sign_doc.save()
            else:
                UserDocument.objects.create(profile=profile, title="Signature", file=signature)

        saved_count = 0
        for spec in document_specs:
            field_name = spec["field_name"]
            title = spec["title"]
            file_obj = request.FILES.get(field_name)
            if not file_obj:
                continue
            existing = profile.documents.filter(title__iexact=title).first()
            if existing:
                existing.file = file_obj
                existing.save()
            else:
                UserDocument.objects.create(profile=profile, title=title, file=file_obj)
            saved_count += 1

        for idx, file_obj in enumerate(extra_files):
            title = "Additional Document"
            if idx < len(extra_titles) and extra_titles[idx].strip():
                title = extra_titles[idx].strip()
            existing = profile.documents.filter(title__iexact=title).first()
            if existing:
                existing.file = file_obj
                existing.save()
            else:
                UserDocument.objects.create(profile=profile, title=title, file=file_obj)
            saved_count += 1

        photo_saved = "Yes" if photo else "No"
        sign_saved = "Yes" if signature else "No"
        _mark_master_data_saved(profile)
        messages.success(
            request,
            f"Master Data save ho gaya. Photo updated: {photo_saved}, Signature updated: {sign_saved}, Other docs: {saved_count}.",
        )
        return redirect("role_select")

    uploaded_map = {doc.title: doc for doc in profile.documents.all()}
    ctx = _step_context(profile, "documents")
    rendered_specs = []
    for spec in document_specs:
        info = _document_file_info(profile, spec["title"])
        rendered_specs.append(
            {
                "field_name": spec["field_name"],
                "title": spec["title"],
                "required": spec["required"],
                "uploaded": info["uploaded"],
                "preview_url": info["url"],
                "uploaded_name": info["name"],
            }
        )
    ctx["document_specs"] = rendered_specs
    ctx["uploaded_map"] = uploaded_map
    ctx["passport_photo_url"] = _profile_image_url(profile, "Passport Photo", profile.photo)
    ctx["signature_url"] = _profile_image_url(profile, "Signature", profile.signature)
    return render(request, "accounts/master_data_step.html", ctx)
