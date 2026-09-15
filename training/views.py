from datetime import timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

from django import forms as django_forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import F, QuerySet, Sum
from django.http import HttpRequest, HttpResponse
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views import generic
from django.views.decorators.http import require_POST

from .forms import (
    BalanceAdjustmentForm, ProfileForm, SessionForm, SignUpForm, TrainerForm,
    TrainerSessionForm,
)
from .models import (
    BalanceTransaction, Membership, MembershipPlan, MembershipUsage,
    SalaryAccrual, Specialization, Trainer, TrainingSession, User,
)


def is_trainer(user: User) -> bool:
    return user.role == User.Role.TRAINER and hasattr(user, "trainer_profile")


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


@login_required
def home(request: HttpRequest) -> HttpResponse:
    visits = request.session.get("num_visits", 0)
    request.session["num_visits"] = visits + 1
    return render(request, "home.html", {
        "num_visits": visits,
        "sessions": TrainingSession.objects.filter(
            starts_at__gt=timezone.now(),
        ).select_related("trainer", "specialization")[:4],
        "session_count": TrainingSession.objects.count(),
        "trainer_count": Trainer.objects.count(),
        "member_count": get_user_model().objects.count(),
    })


class SearchList(LoginRequiredMixin, generic.ListView):
    template_name = "list.html"
    paginate_by = 5
    search_field = "name"

    def get_queryset(self) -> QuerySet[Any]:
        queryset = super().get_queryset()
        query = self.request.GET.get("q", "").strip()
        return queryset.filter(**{f"{self.search_field}__icontains": query})

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        params = self.request.GET.copy()
        params.pop("page", None)
        context.update(
            query_string=params.urlencode(),
            query=self.request.GET.get("q", ""),
            kind=self.kind, title=self.title,
            can_manage_catalog=self.request.user.is_superuser,
            can_create_session=(
                self.request.user.is_superuser
                or is_trainer(self.request.user)
            ),
        )
        return context


class SessionList(SearchList):
    model = TrainingSession
    queryset = TrainingSession.objects.select_related("trainer", "specialization")
    search_field = "title"
    kind = "session"
    title = "Training sessions"


class TrainerList(SearchList):
    model = Trainer
    kind = "trainer"
    title = "Our trainers"


class SpecializationList(SearchList):
    model = Specialization
    kind = "specialization"
    title = "Explore disciplines"


class SessionDetail(LoginRequiredMixin, generic.DetailView):
    model = TrainingSession
    template_name = "session_detail.html"
    queryset = TrainingSession.objects.select_related(
        "trainer", "specialization",
    ).prefetch_related("participants")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["can_manage_session"] = (
            self.request.user.is_superuser
            or self.object.trainer.user_id == self.request.user.pk
        )
        return context


class EditorMixin(LoginRequiredMixin):
    template_name = "form.html"


class AdminRequiredMixin(EditorMixin, UserPassesTestMixin):
    def test_func(self) -> bool:
        return self.request.user.is_superuser


class ClientList(AdminRequiredMixin, generic.ListView):
    model = User
    template_name = "client_list.html"
    context_object_name = "clients"
    paginate_by = 10

    def get_queryset(self) -> QuerySet[User]:
        return User.objects.filter(role=User.Role.CLIENT).order_by("username", "pk")


@login_required
def adjust_client_balance(request: HttpRequest, pk: int) -> HttpResponse:
    if not request.user.is_superuser:
        raise PermissionDenied
    client = get_object_or_404(User, pk=pk, role=User.Role.CLIENT)
    form = BalanceAdjustmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        amount = form.cleaned_data["amount"]
        with transaction.atomic():
            client = User.objects.select_for_update().get(pk=client.pk)
            if client.balance + amount < 0:
                form.add_error("amount", "This would make the client balance negative.")
            else:
                client.balance += amount
                client.save(update_fields=["balance"])
                BalanceTransaction.objects.create(
                    user=client,
                    amount=amount,
                    kind=BalanceTransaction.Kind.ADMIN,
                    description=f"Admin adjustment: {form.cleaned_data['description']}",
                )
                messages.success(request, "Client balance was adjusted.")
                return redirect("training:client-list")
    return render(
        request,
        "form.html",
        {"form": form, "title": f"Adjust balance: {client.username}"},
    )

    def get_queryset(self) -> QuerySet[User]:
        query = self.request.GET.get("q", "").strip()
        return User.objects.filter(
            role=User.Role.CLIENT, is_superuser=False, username__icontains=query,
        ).prefetch_related("training_sessions").order_by("username")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["query"] = self.request.GET.get("q", "")
        return context


class MembershipDashboard(LoginRequiredMixin, generic.ListView):
    model = Membership
    template_name = "membership_dashboard.html"
    context_object_name = "memberships"

    def get_queryset(self) -> QuerySet[Membership]:
        return Membership.objects.filter(user=self.request.user)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["plans"] = MembershipPlan.objects.filter(is_active=True)
        context["transactions"] = BalanceTransaction.objects.filter(
            user=self.request.user,
        )[:5]
        return context


class TrainerEarnings(LoginRequiredMixin, UserPassesTestMixin, generic.ListView):
    model = SalaryAccrual
    template_name = "trainer_earnings.html"
    context_object_name = "accruals"

    def test_func(self) -> bool:
        return is_trainer(self.request.user)

    def get_queryset(self) -> QuerySet[SalaryAccrual]:
        return SalaryAccrual.objects.filter(
            trainer=self.request.user.trainer_profile,
        ).select_related("session")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        accruals = self.get_queryset()
        context["hold_total"] = accruals.filter(
            status=SalaryAccrual.Status.ACCRUED,
        ).aggregate(total=Sum("amount"))["total"] or 0
        context["confirmed_total"] = accruals.filter(
            status=SalaryAccrual.Status.READY,
        ).aggregate(total=Sum("amount"))["total"] or 0
        context["paid_total"] = accruals.filter(
            status=SalaryAccrual.Status.PAID,
        ).aggregate(total=Sum("amount"))["total"] or 0
        return context


class PayoutList(AdminRequiredMixin, generic.ListView):
    model = SalaryAccrual
    template_name = "payout_list.html"
    context_object_name = "accruals"

    def get_queryset(self) -> QuerySet[SalaryAccrual]:
        return SalaryAccrual.objects.select_related("trainer", "session")


@login_required
@require_POST
def add_balance(request: HttpRequest) -> HttpResponse:
    try:
        amount = Decimal(request.POST.get("amount", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        amount = Decimal("0.00")
    if not Decimal("0.00") < amount <= Decimal("10000.00"):
        messages.error(request, "Enter a top-up amount between 0.01 and 10,000.00 PLN.")
        return redirect("training:membership")
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        user.balance += amount
        user.save(update_fields=["balance"])
        BalanceTransaction.objects.create(
            user=user, amount=amount, kind=BalanceTransaction.Kind.TOP_UP,
            description="Demo balance top-up",
        )
    messages.success(request, f"{amount} PLN was added to your demo balance.")
    return redirect("training:membership")


@login_required
@require_POST
def purchase_membership(request: HttpRequest, pk: int) -> HttpResponse:
    plan = get_object_or_404(MembershipPlan, pk=pk, is_active=True)
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        if user.balance < plan.price:
            messages.error(request, "Your balance is too low for this membership.")
            return redirect("training:membership")
        user.balance -= plan.price
        user.save(update_fields=["balance"])
        Membership.objects.filter(user=user, is_active=True).update(is_active=False)
        starts_on = timezone.localdate()
        Membership.objects.create(
            user=user, plan=plan, title=plan.title,
            sessions_total=plan.sessions_count,
            sessions_remaining=plan.sessions_count,
            price=plan.price, starts_on=starts_on,
            expires_on=starts_on + timedelta(days=plan.duration_days),
        )
        BalanceTransaction.objects.create(
            user=user, amount=-plan.price,
            kind=BalanceTransaction.Kind.MEMBERSHIP,
            description=f"Purchased {plan.title}",
        )
    messages.success(request, f"{plan.title} is now active.")
    return redirect("training:membership")


class TrainerSessionMixin(EditorMixin, UserPassesTestMixin):
    """Lets a trainer manage only sessions assigned to their profile."""

    trainer = None

    def test_func(self) -> bool:
        return self.request.user.is_superuser or is_trainer(self.request.user)

    def dispatch(
        self, request: HttpRequest, *args: Any, **kwargs: Any,
    ) -> HttpResponse:
        if not request.user.is_superuser:
            if not is_trainer(request.user):
                raise PermissionDenied
            self.trainer = request.user.trainer_profile
        return super().dispatch(request, *args, **kwargs)

    def get_form_class(self) -> type[SessionForm]:
        return SessionForm if self.request.user.is_superuser else TrainerSessionForm

    def get_form(
        self, form_class: type[SessionForm] | None = None,
    ) -> SessionForm:
        form = super().get_form(form_class)
        if self.trainer:
            form.instance.trainer = self.trainer
            form.fields["specialization"].queryset = self.trainer.specializations.all()
        return form

    def get_queryset(self) -> QuerySet[TrainingSession]:
        queryset = super().get_queryset()
        if self.request.user.is_superuser:
            return queryset
        return queryset.filter(trainer=self.trainer)


class SessionCreate(TrainerSessionMixin, generic.CreateView):
    model = TrainingSession


class SessionUpdate(TrainerSessionMixin, generic.UpdateView):
    model = TrainingSession

    def get_queryset(self) -> QuerySet[TrainingSession]:
        return super().get_queryset().filter(status=TrainingSession.Status.SCHEDULED)


class SafeDelete(LoginRequiredMixin, generic.DeleteView):
    def get_template_names(self) -> list[str]:
        """Use confirmation UI despite the editor mixin's form template."""
        return ["confirm_delete.html"]

    def form_valid(self, form: Any) -> HttpResponse:
        object_name = str(self.get_object())
        try:
            response = super().form_valid(form)
            messages.success(self.request, f"{object_name} was deleted.")
            return response
        except ProtectedError:
            messages.error(self.request, "Remove linked sessions first.")
            return redirect(self.success_url)


class SessionDelete(TrainerSessionMixin, SafeDelete):
    model = TrainingSession
    success_url = reverse_lazy("training:session-list")

    def get_form_class(self) -> type[django_forms.Form]:
        """DeleteView needs its empty confirmation form, not SessionForm."""
        return django_forms.Form

    def form_valid(self, form: Any) -> HttpResponse:
        session = self.get_object()
        if SalaryAccrual.objects.filter(session=session).exists():
            messages.error(
                self.request,
                "This session cannot be deleted because it has a salary record.",
            )
            return redirect("training:session-list")
        return super().form_valid(form)


class TrainerCreate(AdminRequiredMixin, generic.CreateView):
    model = Trainer
    form_class = TrainerForm
    success_url = reverse_lazy("training:trainer-list")


class TrainerUpdate(AdminRequiredMixin, generic.UpdateView):
    model = Trainer
    form_class = TrainerForm
    success_url = reverse_lazy("training:trainer-list")


class TrainerDelete(AdminRequiredMixin, SafeDelete):
    model = Trainer
    success_url = reverse_lazy("training:trainer-list")


class SpecializationCreate(AdminRequiredMixin, generic.CreateView):
    model = Specialization
    fields = ("name", "description")
    success_url = reverse_lazy("training:specialization-list")


class SpecializationUpdate(AdminRequiredMixin, generic.UpdateView):
    model = Specialization
    fields = ("name", "description")
    success_url = reverse_lazy("training:specialization-list")


class SpecializationDelete(AdminRequiredMixin, SafeDelete):
    model = Specialization
    success_url = reverse_lazy("training:specialization-list")


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


@login_required
@require_POST
def complete_session(request: HttpRequest, pk: int) -> HttpResponse:
    if not request.user.is_superuser:
        raise PermissionDenied
    with transaction.atomic():
        session = get_object_or_404(
            TrainingSession.objects.select_for_update(), pk=pk,
        )
        if session.status == TrainingSession.Status.COMPLETED:
            messages.info(request, "This session is already completed.")
        else:
            session.status = TrainingSession.Status.COMPLETED
            session.save()
            messages.success(request, "Session confirmed and salary accrual updated.")
    return redirect("training:session-list")


@login_required
@require_POST
def book(request: HttpRequest, pk: int) -> HttpResponse:
    with transaction.atomic():
        # Acquire a SQLite write lock before checking capacity.
        TrainingSession.objects.filter(pk=pk).update(capacity=F("capacity"))
        session = get_object_or_404(TrainingSession, pk=pk)
        if session.status == TrainingSession.Status.COMPLETED:
            messages.error(request, "This session has already been completed.")
        elif session.starts_at <= timezone.now():
            messages.error(request, "This session has already started.")
        elif session.participants.filter(pk=request.user.pk).exists():
            messages.info(request, "You already booked this session.")
        elif session.participants.count() >= session.capacity:
            messages.error(request, "This session is full.")
        else:
            membership = Membership.objects.select_for_update().filter(
                user=request.user,
                is_active=True,
                starts_on__lte=timezone.localdate(),
                expires_on__gte=timezone.localdate(),
                sessions_remaining__gt=0,
            ).order_by("expires_on", "pk").first()
            if membership is None:
                messages.error(request, "An active membership with sessions is required.")
            else:
                membership.sessions_remaining = F("sessions_remaining") - 1
                membership.save(update_fields=["sessions_remaining"])
                MembershipUsage.objects.create(
                    membership=membership, session=session, user=request.user,
                )
                session.participants.add(request.user)
                participant_count = session.participants.count()
                accrual, created = SalaryAccrual.objects.get_or_create(
                    session=session,
                    defaults={
                        "trainer": session.trainer,
                        "participant_count": participant_count,
                        "rate_per_participant": session.trainer.rate_per_participant,
                        "amount": participant_count * session.trainer.rate_per_participant,
                        "status": SalaryAccrual.Status.ACCRUED,
                    },
                )
                if not created and accrual.status == SalaryAccrual.Status.ACCRUED:
                    accrual.participant_count = participant_count
                    accrual.rate_per_participant = session.trainer.rate_per_participant
                    accrual.amount = participant_count * session.trainer.rate_per_participant
                    accrual.save(update_fields=[
                        "participant_count", "rate_per_participant", "amount",
                    ])
                messages.success(request, "Your place is reserved.")
    return redirect(session)


@login_required
@require_POST
def cancel(request: HttpRequest, pk: int) -> HttpResponse:
    with transaction.atomic():
        session = get_object_or_404(
            TrainingSession.objects.select_for_update(), pk=pk,
        )
        usage = MembershipUsage.objects.filter(
            session=session, user=request.user,
        ).select_related("membership").first()
        accrual = SalaryAccrual.objects.filter(session=session).first()
        if usage is None:
            messages.info(request, "You do not have a booking for this session.")
        elif session.status == TrainingSession.Status.COMPLETED or (
            accrual is not None and accrual.status != SalaryAccrual.Status.ACCRUED
        ):
            messages.error(
                request,
                "This booking cannot be cancelled after the session is confirmed.",
            )
        else:
            session.participants.remove(request.user)
            if accrual:
                participant_count = session.participants.count()
                if participant_count == 0:
                    accrual.delete()
                else:
                    accrual.participant_count = participant_count
                    accrual.amount = participant_count * accrual.rate_per_participant
                    accrual.save(update_fields=["participant_count", "amount"])
            Membership.objects.filter(pk=usage.membership_id).update(
                sessions_remaining=F("sessions_remaining") + 1,
            )
            usage.delete()
            messages.success(request, "Your booking was cancelled.")
    return redirect(session)
