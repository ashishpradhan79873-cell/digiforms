from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST
from datetime import date
import io

from .models import DocumentRule, UserDocument, UserProfile
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
    }


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
        email = request.POST.get("email", "").strip()
        password = request.POST.get("password", "")
        confirm = request.POST.get("confirm", "")
        if password != confirm:
            messages.error(request, "Password match nahi kiya!")
        elif User.objects.filter(username=username).exists():
            messages.error(request, "Username pehle se exist karta hai!")
        else:
            user = User.objects.create_user(username=username, email=email, password=password)
            UserProfile.objects.create(user=user, email=email)
            login(request, user)
            messages.success(request, "Account ban gaya! Ab Master Data bharo.")
            return redirect("master_data_option")
    return render(request, "accounts/register.html")


def logout_view(request):
    logout(request)
    return redirect("login")


@login_required
def master_data_option_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            return redirect("master_data_personal")
        if action == "demo":
            profile.full_name = profile.full_name or "Rajesh Kumar"
            profile.father_name = "Mahesh Kumar"
            profile.mother_name = "Suman Devi"
            profile.dob = date(2000, 3, 15)
            profile.gender = "M"
            profile.category = "OBC"
            profile.mobile = "9876543210"
            profile.email = "rajesh.kumar@example.com"
            profile.aadhar = "1234-5678-9012"
            profile.samagra_id = "SGR00012345"

            profile.present_state = "Chhattisgarh"
            profile.present_district = "Raipur"
            profile.present_city = "Raipur"
            profile.present_pincode = "492001"
            profile.present_address = "Ward 12, Shanti Nagar, Raipur"
            profile.permanent_state = "Chhattisgarh"
            profile.permanent_district = "Raipur"
            profile.permanent_pincode = "492001"
            profile.permanent_full_address = "Ward 12, Shanti Nagar, Raipur"
            profile.state = "Chhattisgarh"
            profile.district = "Raipur"
            profile.pincode = "492001"
            profile.permanent_address = profile.permanent_full_address

            profile.tenth_board = "CGBSE"
            profile.tenth_roll_number = "CGBSE10-22441"
            profile.tenth_percentage = "82.4"
            profile.tenth_result = "82.4"
            profile.twelfth_board = "CGBSE"
            profile.twelfth_roll_number = "CGBSE12-77882"
            profile.twelfth_percentage = "79.6"
            profile.twelfth_result = "79.6"
            profile.graduation = "B.Sc. Final Year"

            profile.college_name = "Govt Science College Raipur"
            profile.university_name = "Pt. Ravishankar Shukla University"
            profile.course = "B.Sc."
            profile.year_semester = "Final Year"
            profile.enrollment_number = "PRSU-2022-001245"
            profile.university = profile.university_name

            profile.account_holder_name = "Rajesh Kumar"
            profile.bank_name = "State Bank of India"
            profile.account_number = "112233445566"
            profile.ifsc_code = "SBIN0001234"
            profile.branch_name = "Raipur Main"
            profile.aadhaar_linked = "yes"

            profile.personal_extra_rows = [{"label": "Nationality", "value": "Indian"}]
            profile.address_extra_rows = [{"label": "Landmark", "value": "Near City Post Office"}]
            profile.academic_extra_rows = [{"label": "Current Backlogs", "value": "0"}]
            profile.college_extra_rows = [{"label": "Section", "value": "A"}]
            profile.bank_extra_rows = [{"label": "Account Type", "value": "Savings"}]
            profile.save()
            messages.success(request, "Demo master data auto-fill ho gaya.")
            return redirect("role_select")
        return redirect("role_select")
    return render(request, "accounts/master_data_option.html", {"has_profile": bool(profile.full_name)})


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
        extra_labels = p.getlist("personal_extra_label[]")
        extra_values = p.getlist("personal_extra_value[]")
        profile.personal_extra_rows = [
            {"label": label.strip(), "value": value.strip()}
            for label, value in zip(extra_labels, extra_values)
            if label.strip() or value.strip()
        ]
        profile.save()
        return redirect("master_data_address")
    return render(request, "accounts/master_data_step.html", _step_context(profile, "personal"))


@login_required
def master_data_address_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
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
        extra_labels = p.getlist("address_extra_label[]")
        extra_values = p.getlist("address_extra_value[]")
        profile.address_extra_rows = [
            {"label": label.strip(), "value": value.strip()}
            for label, value in zip(extra_labels, extra_values)
            if label.strip() or value.strip()
        ]
        profile.save()
        return redirect("master_data_academic")
    return render(request, "accounts/master_data_step.html", _step_context(profile, "address"))


@login_required
def master_data_academic_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
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
        extra_labels = p.getlist("academic_extra_label[]")
        extra_values = p.getlist("academic_extra_value[]")
        profile.academic_extra_rows = [
            {"label": label.strip(), "value": value.strip()}
            for label, value in zip(extra_labels, extra_values)
            if label.strip() or value.strip()
        ]
        profile.save()
        return redirect("master_data_college")
    return render(request, "accounts/master_data_step.html", _step_context(profile, "academic"))


@login_required
def master_data_college_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
        p = request.POST
        profile.college_name = p.get("college_name", "")
        profile.university_name = p.get("university_name", "")
        profile.course = p.get("course", "")
        profile.year_semester = p.get("year_semester", "")
        profile.enrollment_number = p.get("enrollment_number", "")
        profile.university = profile.university_name
        extra_labels = p.getlist("college_extra_label[]")
        extra_values = p.getlist("college_extra_value[]")
        profile.college_extra_rows = [
            {"label": label.strip(), "value": value.strip()}
            for label, value in zip(extra_labels, extra_values)
            if label.strip() or value.strip()
        ]
        profile.save()
        return redirect("master_data_bank")
    return render(request, "accounts/master_data_step.html", _step_context(profile, "college"))


@login_required
def master_data_bank_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
        p = request.POST
        profile.account_holder_name = p.get("account_holder_name", "")
        profile.bank_name = p.get("bank_name", "")
        profile.account_number = p.get("account_number", "")
        profile.ifsc_code = p.get("ifsc_code", "")
        profile.branch_name = p.get("branch_name", "")
        profile.aadhaar_linked = p.get("aadhaar_linked", "")
        extra_labels = p.getlist("bank_extra_label[]")
        extra_values = p.getlist("bank_extra_value[]")
        profile.bank_extra_rows = [
            {"label": label.strip(), "value": value.strip()}
            for label, value in zip(extra_labels, extra_values)
            if label.strip() or value.strip()
        ]
        profile.save()
        return redirect("master_data_documents")
    return render(request, "accounts/master_data_step.html", _step_context(profile, "bank"))


@login_required
def master_data_documents_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    rule_map = {_normalize_doc_name(r.name): r for r in DocumentRule.objects.filter(is_active=True)}
    if request.method == "POST":
        errors = []
        photo = request.FILES.get("passport_photo")
        signature = request.FILES.get("signature")
        if photo:
            err = _validate_file_rule("Passport Size Photo", photo, rule_map)
            if err:
                errors.append(err)
        if signature:
            err = _validate_file_rule("Signature", signature, rule_map)
            if err:
                errors.append(err)
        for field_name, title, _ in DOCUMENT_SPECS:
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

        for field_name, title, _ in DOCUMENT_SPECS:
            file_obj = request.FILES.get(field_name)
            if not file_obj:
                continue
            existing = profile.documents.filter(title=title).first()
            if existing:
                existing.file = file_obj
                existing.save()
            else:
                UserDocument.objects.create(profile=profile, title=title, file=file_obj)

        for idx, file_obj in enumerate(extra_files):
            title = "Additional Document"
            if idx < len(extra_titles) and extra_titles[idx].strip():
                title = extra_titles[idx].strip()
            existing = profile.documents.filter(title=title).first()
            if existing:
                existing.file = file_obj
                existing.save()
            else:
                UserDocument.objects.create(profile=profile, title=title, file=file_obj)

        messages.success(request, "Master Data complete ho gaya.")
        return redirect("role_select")

    uploaded_map = {doc.title: doc for doc in profile.documents.all()}
    ctx = _step_context(profile, "documents")
    ctx["document_specs"] = DOCUMENT_SPECS
    ctx["uploaded_map"] = uploaded_map
    return render(request, "accounts/master_data_step.html", ctx)
