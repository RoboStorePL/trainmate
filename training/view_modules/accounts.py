from hashlib import sha256
from typing import Any

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core.mail import send_mail
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse_lazy
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views import generic

from ..forms import ProfileForm, SignUpForm
from ..models import User
from .common import EditorMixin


LOGIN_RATE_LIMIT = 5
RATE_LIMIT_SECONDS = 15 * 60


def rate_limit_key(request: HttpRequest, action: str, identifier: str) -> str:
    raw_key = f"{action}:{request.META.get('REMOTE_ADDR', '')}:{identifier.lower()}"
    return f"trainmate-rate-limit:{sha256(raw_key.encode()).hexdigest()}"


class RateLimitedLoginView(auth_views.LoginView):
    template_name = "registration/login.html"

    def get_limit_key(self) -> str:
        return rate_limit_key(
            self.request, "login", self.request.POST.get("username", ""),
        )

    def dispatch(
        self, request: HttpRequest, *args: Any, **kwargs: Any,
    ) -> HttpResponse:
        if request.method == "POST" and cache.get(self.get_limit_key(), 0) >= LOGIN_RATE_LIMIT:
            messages.error(request, "Too many failed attempts. Try again in 15 minutes.")
            return redirect("login")
        return super().dispatch(request, *args, **kwargs)

    def form_invalid(self, form: Any) -> HttpResponse:
        cache.add(self.get_limit_key(), 0, RATE_LIMIT_SECONDS)
        cache.incr(self.get_limit_key())
        return super().form_invalid(form)

    def form_valid(self, form: Any) -> HttpResponse:
        cache.delete(self.get_limit_key())
        return super().form_valid(form)

    def get_success_url(self) -> str:
        if self.request.user.role == User.Role.KIOSK:
            return reverse_lazy("training:reception-session-list")
        return super().get_success_url()


class RateLimitedPasswordResetView(auth_views.PasswordResetView):
    template_name = "registration/password_reset_form.html"
    email_template_name = "registration/password_reset_email.html"
    subject_template_name = "registration/password_reset_subject.txt"

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        key = rate_limit_key(request, "password-reset", request.POST.get("email", ""))
        if cache.get(key, 0) >= LOGIN_RATE_LIMIT:
            messages.error(request, "Too many reset requests. Try again in 15 minutes.")
            return redirect("password_reset")
        cache.add(key, 0, RATE_LIMIT_SECONDS)
        cache.incr(key)
        return super().post(request, *args, **kwargs)


class SignUp(generic.CreateView):
    form_class = SignUpForm
    template_name = "form.html"

    def form_valid(self, form: SignUpForm) -> HttpResponse:
        self.object = form.save(commit=False)
        self.object.is_active = False
        self.object.save()
        uid = urlsafe_base64_encode(force_bytes(self.object.pk))
        token = default_token_generator.make_token(self.object)
        activation_url = self.request.build_absolute_uri(
            reverse_lazy("training:activate-account", args=[uid, token]),
        )
        message = render_to_string(
            "registration/activation_email.txt",
            {"user": self.object, "activation_url": activation_url},
        )
        send_mail(
            "Activate your TrainMate account",
            message,
            None,
            [self.object.email],
        )
        return redirect("training:activation-sent")


def activate_account(request: HttpRequest, uidb64: str, token: str) -> HttpResponse:
    try:
        user_id = urlsafe_base64_decode(uidb64).decode()
        user = User.objects.get(pk=user_id)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None
    if user is not None and default_token_generator.check_token(user, token):
        if not user.is_active:
            user.is_active = True
            user.save(update_fields=["is_active"])
        messages.success(request, "Your email is confirmed. You can now log in.")
        return redirect("login")
    return render(request, "registration/activation_invalid.html", status=400)


def activation_sent(request: HttpRequest) -> HttpResponse:
    return render(request, "registration/activation_sent.html")


class Profile(EditorMixin, generic.UpdateView):
    form_class = ProfileForm
    success_url = reverse_lazy("training:profile")

    def get_object(self, queryset: QuerySet[User] | None = None) -> User:
        return self.request.user



