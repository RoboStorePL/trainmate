from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from .core import Trainer
from .sessions import TrainingSession


class TrainerPayout(models.Model):
    """One real-world transfer covering multiple ready salary accruals."""

    trainer = models.ForeignKey(Trainer, on_delete=models.PROTECT, related_name="payouts")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    note = models.CharField(max_length=255, blank=True)
    paid_at = models.DateTimeField(auto_now_add=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="recorded_payouts")
    method = models.CharField(max_length=12, choices=[("cash", "Cash"), ("transfer", "Bank transfer"), ("unknown", "Not recorded")], default="unknown")

    class Meta:
        ordering = ["-paid_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.trainer}: {self.amount} PLN on {self.paid_at:%d %b %Y}"

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Recorded payments cannot be edited.")
        super().save(*args, **kwargs)


class SalaryAccrual(models.Model):
    class Status(models.TextChoices):
        ACCRUED = "accrued", "Expected"
        READY = "ready", "Ready for payout"
        PAID = "paid", "Paid"

    trainer = models.ForeignKey(Trainer, on_delete=models.PROTECT, related_name="accruals")
    session = models.OneToOneField(TrainingSession, on_delete=models.PROTECT, related_name="salary_accrual")
    participant_count = models.PositiveIntegerField()
    rate_per_participant = models.DecimalField(max_digits=8, decimal_places=2)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACCRUED)
    payout = models.ForeignKey(
        TrainerPayout, on_delete=models.PROTECT, null=True, blank=True,
        related_name="accruals",
    )
    admin_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.trainer}: {self.amount} PLN"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Keep the payout lifecycle one-way and retain its key timestamps."""
        previous_status = None
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            if previous.status == self.Status.PAID and (previous.payout_id != self.payout_id or previous.paid_at != self.paid_at):
                raise ValidationError("A paid record cannot be moved to another payment.")
            if previous.status in {self.Status.READY, self.Status.PAID}:
                frozen = ("trainer_id", "session_id", "participant_count", "rate_per_participant", "amount")
                if any(getattr(previous, field) != getattr(self, field) for field in frozen):
                    raise ValidationError("Confirmed salary amounts are fixed. Record a separate correction instead.")
            previous_status = type(self).objects.filter(pk=self.pk).values_list(
                "status", flat=True,
            ).first()

        allowed_transitions = {
            self.Status.ACCRUED: {self.Status.ACCRUED, self.Status.READY},
            self.Status.READY: {self.Status.READY, self.Status.PAID},
            self.Status.PAID: {self.Status.PAID},
        }
        if previous_status and self.status not in allowed_transitions[previous_status]:
            raise ValidationError("A salary payout status cannot be reversed or skipped.")

        now = timezone.now()
        timestamp_fields: set[str] = {"updated_at"}
        if self.status == self.Status.READY and self.confirmed_at is None:
            self.confirmed_at = now
            timestamp_fields.add("confirmed_at")
        if self.status == self.Status.PAID and self.paid_at is None:
            self.paid_at = now
            timestamp_fields.add("paid_at")
        if update_fields := kwargs.get("update_fields"):
            kwargs["update_fields"] = set(update_fields) | timestamp_fields
        super().save(*args, **kwargs)


class SalaryAdjustment(models.Model):
    accrual = models.ForeignKey(SalaryAccrual, on_delete=models.PROTECT, related_name="adjustments")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.CharField(max_length=255)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    payout = models.ForeignKey(TrainerPayout, on_delete=models.PROTECT, null=True, blank=True, related_name="adjustments")

    class Meta:
        ordering = ["-created_at", "-pk"]



