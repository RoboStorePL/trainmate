from django.conf import settings
from django.db import models
from django.utils import timezone

from .sessions import TrainingSession


class MembershipPlan(models.Model):
    title = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    sessions_count = models.PositiveIntegerField()
    duration_days = models.PositiveIntegerField(default=30)
    price = models.DecimalField(max_digits=8, decimal_places=2)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["price", "pk"]

    def __str__(self) -> str:
        return self.title


class Membership(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="memberships",
    )
    plan = models.ForeignKey(
        MembershipPlan, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="purchases",
    )
    title = models.CharField(max_length=100, default="Standard membership")
    sessions_total = models.PositiveIntegerField()
    sessions_remaining = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=8, decimal_places=2)
    starts_on = models.DateField(default=timezone.localdate)
    expires_on = models.DateField()
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-expires_on", "-pk"]

    def __str__(self) -> str:
        return f"{self.user} — {self.title}"


class BalanceTransaction(models.Model):
    class Kind(models.TextChoices):
        TOP_UP = "top_up", "Demo top-up"
        MEMBERSHIP = "membership", "Membership purchase"
        ADMIN = "admin", "Admin adjustment"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="balance_transactions",
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    description = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.user}: {self.amount} PLN"


class MembershipUsage(models.Model):
    membership = models.ForeignKey(Membership, on_delete=models.PROTECT)
    session = models.ForeignKey(TrainingSession, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["session", "user"], name="unique_session_membership_usage",
            ),
        ]



