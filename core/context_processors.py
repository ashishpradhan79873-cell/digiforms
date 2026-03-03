from django.urls import Resolver404, resolve


def _humanize_slug(text):
    value = str(text or "").strip().replace("-", " ").replace("_", " ")
    if value.isdigit():
        return f"#{value}"
    return value.title() if value else "Page"


NAME_LABELS = {
    "home_router": "Home",
    "master_data_option": "Home",
    "role_select": "Choose Portal",
    "dashboard": "Government Dashboard",
    "student_services_dashboard": "Student Dashboard",
    "news_hub": "News",
    "news_detail": "News Detail",
    "admin_applicants": "Admin",
    "admin_option_control": "Vacancy Control",
    "admin_documents": "Document Rules",
    "admin_chat": "Chat",
    "admin_news": "News Control",
    "admin_payment": "Payment",
    "profile_step": "Master Data",
    "confirm_send_to_admin": "Apply Form",
    "user_chat": "Chat With Admin",
    "wallet": "Wallet",
}


def breadcrumbs(request):
    path = (getattr(request, "path", "") or "/").split("?", 1)[0]
    if not path.startswith("/"):
        path = f"/{path}"

    crumbs = [{"label": "Home", "url": "/"}]
    if path == "/":
        return {"breadcrumbs": crumbs}

    running = ""
    for part in [p for p in path.strip("/").split("/") if p]:
        running += f"/{part}"
        url = f"{running}/"
        label = _humanize_slug(part)
        try:
            match = resolve(url)
            label = NAME_LABELS.get(match.url_name, label)
        except Resolver404:
            pass
        crumbs.append({"label": label, "url": url})

    # duplicate Home cleanup (when first route is already Home-labeled)
    deduped = [crumbs[0]]
    for item in crumbs[1:]:
        if item["label"] == deduped[-1]["label"] and item["url"] == deduped[-1]["url"]:
            continue
        deduped.append(item)
    return {"breadcrumbs": deduped}
