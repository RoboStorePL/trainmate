from datetime import date
from decimal import Decimal
from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core import signing
from django.db import transaction
from django.db.models import Count, Q, QuerySet, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse_lazy
from django.views import generic

from ..forms import SalaryAdjustmentForm, TrainerPayoutForm
from ..models import (
    SalaryAccrual,
    SalaryAdjustment,
    Trainer,
    TrainerPayout,
    TrainingSession,
)
from .common import AdminRequiredMixin, is_trainer

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
        adjustments = SalaryAdjustment.objects.filter(accrual__in=accruals).select_related("accrual__session", "created_by", "payout")
        context["adjustments"] = adjustments
        context["confirmed_total"] += adjustments.filter(payout__isnull=True).aggregate(total=Sum("amount"))["total"] or 0
        context["paid_total"] += adjustments.filter(payout__isnull=False).aggregate(total=Sum("amount"))["total"] or 0
        context["outstanding_total"] = context["confirmed_total"]
        context["total_earned"] = context["outstanding_total"] + context["paid_total"]
        context["payouts"] = TrainerPayout.objects.filter(trainer=self.request.user.trainer_profile).select_related("recorded_by", "trainer")
        context["missing_accrual_sessions"] = TrainingSession.objects.filter(
            trainer=self.request.user.trainer_profile, status=TrainingSession.Status.COMPLETED,
            salary_accrual__isnull=True,
        )
        return context


class PayoutList(AdminRequiredMixin, generic.ListView):
    model = SalaryAccrual
    template_name = "payout_list.html"
    context_object_name = "accruals"

    def get_queryset(self) -> QuerySet[SalaryAccrual]:
        return SalaryAccrual.objects.select_related("trainer", "session__completed_by")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        accruals = self.get_queryset()
        totals = accruals.aggregate(
            accrued_total=Sum("amount", filter=Q(status=SalaryAccrual.Status.ACCRUED)),
            ready_total=Sum("amount", filter=Q(status=SalaryAccrual.Status.READY)),
            paid_total=Sum("amount", filter=Q(status=SalaryAccrual.Status.PAID)),
        )
        context.update({key: value or 0 for key, value in totals.items()})
        adjustments = SalaryAdjustment.objects.select_related("accrual__trainer", "accrual__session", "created_by", "payout")
        context["adjustments"] = adjustments
        adjustment_totals = {
            row["accrual__trainer_id"]: row for row in adjustments.values("accrual__trainer_id").annotate(
                due=Sum("amount", filter=Q(payout__isnull=True)),
                paid=Sum("amount", filter=Q(payout__isnull=False)),
            )
        }
        context["ready_total"] += sum(row["due"] or 0 for row in adjustment_totals.values())
        context["paid_total"] += sum(row["paid"] or 0 for row in adjustment_totals.values())
        context["outstanding_total"] = context["ready_total"]

        trainer_totals = Trainer.objects.filter(accruals__isnull=False).annotate(
            session_count=Count("accruals"),
            accrued_total=Sum(
                "accruals__amount",
                filter=Q(accruals__status=SalaryAccrual.Status.ACCRUED),
            ),
            ready_total=Sum(
                "accruals__amount",
                filter=Q(accruals__status=SalaryAccrual.Status.READY),
            ),
            paid_total=Sum(
                "accruals__amount",
                filter=Q(accruals__status=SalaryAccrual.Status.PAID),
            ),
        ).order_by("name")
        for trainer in trainer_totals:
            trainer.accrued_total = trainer.accrued_total or 0
            trainer.ready_total = trainer.ready_total or 0
            trainer.paid_total = trainer.paid_total or 0
            trainer_adjustments = adjustment_totals.get(trainer.pk, {})
            trainer.ready_total += trainer_adjustments.get("due") or 0
            trainer.paid_total += trainer_adjustments.get("paid") or 0
            trainer.outstanding_total = trainer.ready_total
            trainer.total_earned = trainer.outstanding_total + trainer.paid_total
        context["trainer_totals"] = trainer_totals
        context["payouts"] = TrainerPayout.objects.select_related("trainer", "recorded_by").prefetch_related("accruals")
        return context


class TrainerPayoutCreate(AdminRequiredMixin, generic.FormView):
    template_name = "trainer_payout_confirm.html"
    form_class = TrainerPayoutForm
    success_url = reverse_lazy("training:payout-list")

    def get_trainer(self) -> Trainer:
        return get_object_or_404(Trainer, pk=self.kwargs["trainer_pk"])

    def get_ready_accruals(self) -> QuerySet[SalaryAccrual]:
        records = SalaryAccrual.objects.filter(
            trainer=self.get_trainer(), status=SalaryAccrual.Status.READY,
            payout__isnull=True, amount__gt=0,
        ).select_related("session").order_by("session__starts_at", "pk")
        values = self.request.POST if self.request.method == "POST" else self.request.GET
        try:
            start = date.fromisoformat(values["starts_on"]) if values.get("starts_on") else None
            end = date.fromisoformat(values["ends_on"]) if values.get("ends_on") else None
        except ValueError:
            return records.none()
        if start and end and start > end:
            return records.none()
        if start:
            records = records.filter(session__starts_at__date__gte=start)
        if end:
            records = records.filter(session__starts_at__date__lte=end)
        return records

    def get_adjustments(self):
        records = SalaryAdjustment.objects.filter(accrual__trainer=self.get_trainer(), payout__isnull=True)
        values = self.request.POST if self.request.method == "POST" else self.request.GET
        try:
            start = date.fromisoformat(values["starts_on"]) if values.get("starts_on") else None
            end = date.fromisoformat(values["ends_on"]) if values.get("ends_on") else None
        except ValueError:
            return records.none()
        if start and end and start > end:
            return records.none()
        if start:
            records = records.filter(accrual__session__starts_at__date__gte=start)
        if end:
            records = records.filter(accrual__session__starts_at__date__lte=end)
        return records.select_related("accrual__session").order_by("pk")

    @staticmethod
    def selection_for(records, adjustments):
        return [["earning", record.pk, str(record.amount)] for record in records] + [["correction", record.pk, str(record.amount)] for record in adjustments]

    def get_initial(self):
        return {"starts_on": self.request.GET.get("starts_on", ""), "ends_on": self.request.GET.get("ends_on", ""), "method": "transfer"}

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        ready_accruals = list(self.get_ready_accruals())
        adjustments = list(self.get_adjustments())
        context["form"].initial["selection"] = signing.dumps(
            self.selection_for(ready_accruals, adjustments), salt="payout-selection",
        )
        context.update({
            "trainer": self.get_trainer(),
            "ready_accruals": ready_accruals,
            "adjustments": adjustments,
            "payout_amount": sum((record.amount for record in ready_accruals + adjustments), Decimal("0.00")),
        })
        return context

    def form_valid(self, form: TrainerPayoutForm) -> HttpResponse:
        trainer = self.get_trainer()
        with transaction.atomic():
            Trainer.objects.select_for_update().get(pk=trainer.pk)
            ready_accruals = list(self.get_ready_accruals().select_for_update())
            adjustments = list(self.get_adjustments().select_for_update())
            try:
                selection = signing.loads(form.cleaned_data["selection"], salt="payout-selection", max_age=3600)
            except signing.BadSignature:
                selection = None
            if selection != self.selection_for(ready_accruals, adjustments):
                form.add_error(None, "The selection changed or expired. Reload this page to review the payment again.")
                return self.form_invalid(form)
            if not ready_accruals and not adjustments:
                form.add_error(None, "There are no ready salary records left to pay.")
                return self.form_invalid(form)
            amount = sum((record.amount for record in ready_accruals + adjustments), Decimal("0.00"))
            if amount <= 0:
                form.add_error(None, "The payment must be positive. Negative corrections offset future earnings.")
                return self.form_invalid(form)
            payout = TrainerPayout.objects.create(
                trainer=trainer, amount=amount, note=form.cleaned_data["note"],
                method=form.cleaned_data["method"], recorded_by=self.request.user,
            )
            for accrual in ready_accruals:
                accrual.status = SalaryAccrual.Status.PAID
                accrual.payout = payout
                accrual.save(update_fields=["status", "payout"])
            SalaryAdjustment.objects.filter(pk__in=[record.pk for record in adjustments]).update(payout=payout)
        messages.success(
            self.request,
            f"Recorded a payment of {amount} PLN to {trainer.name} for {len(ready_accruals)} sessions.",
        )
        return super().form_valid(form)


class TrainerPayoutDetail(LoginRequiredMixin, generic.DetailView):
    model = TrainerPayout
    template_name = "trainer_payout_detail.html"

    def get_queryset(self):
        records = TrainerPayout.objects.select_related("trainer", "recorded_by").prefetch_related("accruals__session__completed_by", "adjustments__accrual__session", "adjustments__created_by")
        if not self.request.user.is_superuser:
            records = records.filter(trainer__user=self.request.user)
        return records


class SalaryAdjustmentCreate(AdminRequiredMixin, generic.FormView):
    template_name = "form.html"
    form_class = SalaryAdjustmentForm
    success_url = reverse_lazy("training:payout-list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        record = get_object_or_404(SalaryAccrual, pk=self.kwargs["pk"], status__in=["ready", "paid"])
        context["title"] = f"Correction: {record.trainer} · {record.session.title}"
        return context

    def form_valid(self, form):
        with transaction.atomic():
            record = get_object_or_404(SalaryAccrual, pk=self.kwargs["pk"], status__in=["ready", "paid"])
            Trainer.objects.select_for_update().get(pk=record.trainer_id)
            SalaryAdjustment.objects.create(accrual=record, created_by=self.request.user, **form.cleaned_data)
        messages.success(self.request, "Correction recorded. It will be included in a future payment for this session period.")
        return super().form_valid(form)




