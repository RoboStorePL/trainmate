from django.shortcuts import redirect
from django.urls import Resolver404, resolve

from .models import User


class KioskAccessMiddleware:
    """Keep a reception tablet account inside its small, purpose-built workflow."""

    allowed_url_names = {
        "login", "logout", "reception-session-list", "reception-check-in", "kiosk-check-in",
        "kiosk-undo-check-in",
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user and user.is_authenticated and user.role == User.Role.KIOSK:
            try:
                match = resolve(request.path_info)
            except Resolver404:
                match = None
            if match is None or match.url_name not in self.allowed_url_names:
                return redirect("training:reception-session-list")
        return self.get_response(request)
