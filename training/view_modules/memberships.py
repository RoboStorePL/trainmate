from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import generic
from django.views.decorators.http import require_POST

from ..forms import BalanceAdjustmentForm
from ..models import BalanceTransaction, Membership, MembershipPlan, User
from .common import AdminRequiredMixin

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




