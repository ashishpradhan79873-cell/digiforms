from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import Http404


class AdminAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path or ""
        if path == "/admin" or path.startswith("/admin/"):
            user = getattr(request, "user", None)
            if not user or not user.is_authenticated:
                return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
            if not (user.is_staff or user.is_superuser):
                raise Http404("Page not found")
        return self.get_response(request)
