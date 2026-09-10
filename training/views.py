from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F, QuerySet, Sum
from django.http import HttpRequest, HttpResponse
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import generic
from django.views.decorators.http import require_POST

from .forms import ProfileForm, SessionForm, SignUpForm, TrainerForm, TrainerSessionForm
from .models import (
    BalanceTransaction, Membership, MembershipPlan, MembershipUsage,
    SalaryAccrual, Specialization, Trainer, TrainingSession, User,
)


def is_trainer(user: User) -> bool:
    return user.role == User.Role.TRAINER and hasattr(user, "trainer_profile")


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
        context["total_accrued"] = self.get_queryset().exclude(
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


class SafeDelete(LoginRequiredMixin, generic.DeleteView):
    template_name = "confirm_delete.html"

    def form_valid(self, form: Any) -> HttpResponse:
        try:
            return super().form_valid(form)
        except ProtectedError:
            messages.error(self.request, "Remove linked sessions first.")
            return redirect(self.success_url)


class SessionDelete(TrainerSessionMixin, SafeDelete):
    model = TrainingSession
    success_url = reverse_lazy("training:session-list")


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
    success_url = reverse_lazy("login")


class Profile(EditorMixin, generic.UpdateView):
    form_class = ProfileForm
    success_url = reverse_lazy("training:profile")

    def get_object(self, queryset: QuerySet[User] | None = None) -> User:
        return self.request.user


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
                messages.success(request, "Your place is reserved.")
    return redirect(session)


@login_required
@require_POST
def cancel(request: HttpRequest, pk: int) -> HttpResponse:
    session = get_object_or_404(TrainingSession, pk=pk)
    usage = MembershipUsage.objects.filter(
        session=session, user=request.user,
    ).select_related("membership").first()
    session.participants.remove(request.user)
    if usage:
        Membership.objects.filter(pk=usage.membership_id).update(
            sessions_remaining=F("sessions_remaining") + 1,
        )
        usage.delete()
    messages.success(request, "Your booking was cancelled.")
    return redirect(session)
