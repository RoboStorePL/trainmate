from decimal import Decimal
from hashlib import sha256
from secrets import compare_digest
from typing import Any

from django.contrib.auth.models import AbstractUser
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone


class User(AbstractUser):
    class Role(models.TextChoices):
        CLIENT = "client", "Client"
        TRAINER = "trainer", "Trainer"

    role = models.CharField(max_length=10, choices=Role.choices, default=Role.CLIENT)
    balance = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    fitness_level = models.CharField(
        max_length=20, default="beginner",
        choices=[("beginner", "Beginner"), ("intermediate", "Intermediate"),
                 ("advanced", "Advanced")],
    )

    def get_absolute_url(self) -> str:
        return reverse("training:profile")


class Specialization(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Trainer(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="trainer_profile",
    )
    name = models.CharField(max_length=150)
    bio = models.TextField(blank=True)
    experience_years = models.PositiveIntegerField(default=0)
    rate_per_participant = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("10.00"),
        help_text="PLN paid for each participant in a completed session.",
    )
    specializations = models.ManyToManyField(
        Specialization, related_name="trainers",
    )

    class Meta:
        ordering = ["name", "pk"]

    def __str__(self) -> str:
        return self.name


class TrainingSession(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        COMPLETED = "completed", "Completed"

    title = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    trainer = models.ForeignKey(Trainer, on_delete=models.PROTECT)
    specialization = models.ForeignKey(
        Specialization, on_delete=models.PROTECT,
    )
    starts_at = models.DateTimeField()
    duration_minutes = models.PositiveIntegerField(
        default=60, validators=[MinValueValidator(1)],
    )
    capacity = models.PositiveIntegerField(
        default=10, validators=[MinValueValidator(1)],
    )
    location = models.CharField(max_length=200)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.SCHEDULED,
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    participants = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="training_sessions",
    )

    class Meta:
        ordering = ["starts_at", "pk"]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return reverse("training:session-detail", args=[self.pk])

    def clean(self) -> None:
        if self.starts_at and self.starts_at <= timezone.now():
            raise ValidationError({"starts_at": "Choose a future time."})
        if self.pk and self.capacity < self.participants.count():
            raise ValidationError({"capacity": "Already booked beyond this limit."})
        if self.trainer_id and self.specialization_id:
            if not self.trainer.specializations.filter(
                pk=self.specialization_id,
            ).exists():
                raise ValidationError(
                    {"trainer": "Trainer must teach this specialization."},
                )

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.status == self.Status.COMPLETED and self.completed_at is None:
            self.completed_at = timezone.now()
        super().save(*args, **kwargs)
        if self.status == self.Status.COMPLETED:
            participant_count = self.participants.count()
            if participant_count == 0:
                return
            accrual, created = SalaryAccrual.objects.get_or_create(
                session=self,
                defaults={
                    "trainer": self.trainer,
                    "participant_count": participant_count,
                    "rate_per_participant": self.trainer.rate_per_participant,
                    "amount": participant_count * self.trainer.rate_per_participant,
                    "status": SalaryAccrual.Status.READY,
                },
            )
            if not created and accrual.status == SalaryAccrual.Status.ACCRUED:
                accrual.participant_count = participant_count
                accrual.rate_per_participant = self.trainer.rate_per_participant
                accrual.amount = participant_count * self.trainer.rate_per_participant
                accrual.status = SalaryAccrual.Status.READY
                accrual.save(update_fields=[
                    "participant_count", "rate_per_participant", "amount", "status",
                ])


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


class SalaryAccrual(models.Model):
    class Status(models.TextChoices):
        ACCRUED = "accrued", "Accrued"
        READY = "ready", "Ready for payout"
        PAID = "paid", "Paid"

    trainer = models.ForeignKey(Trainer, on_delete=models.PROTECT, related_name="accruals")
    session = models.OneToOneField(TrainingSession, on_delete=models.CASCADE, related_name="salary_accrual")
    participant_count = models.PositiveIntegerField()
    rate_per_participant = models.DecimalField(max_digits=8, decimal_places=2)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACCRUED)
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


class VisionDevice(models.Model):
    """A Raspberry Pi authorised to submit local pose-analysis results."""

    name = models.CharField(max_length=100, unique=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="vision_devices",
    )
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def set_token(self, raw_token: str) -> None:
        self.token_hash = sha256(raw_token.encode()).hexdigest()

    def check_token(self, raw_token: str) -> bool:
        return compare_digest(
            self.token_hash, sha256(raw_token.encode()).hexdigest(),
        )


class VisionAnalysis(models.Model):
    """A privacy-preserving pose-analysis result produced on a Pi device."""

    class Activity(models.TextChoices):
        YOGA = "yoga", "Yoga"
        FITNESS = "fitness", "Fitness"
        REHABILITATION = "rehabilitation", "Rehabilitation"
        JIU_JITSU = "jiu-jitsu", "Jiu-jitsu"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="vision_analyses",
    )
    device = models.ForeignKey(
        VisionDevice, on_delete=models.PROTECT, related_name="analyses",
    )
    activity = models.CharField(max_length=20, choices=Activity.choices)
    pose_score = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    feedback = models.CharField(max_length=500, blank=True)
    landmarks = models.JSONField(default=dict, blank=True)
    snapshot = models.FileField(
        upload_to="vision_snapshots/%Y/%m/%d", blank=True,
        help_text="An optional user-approved progress frame from the Pi.",
    )
    captured_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-captured_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.user} — {self.get_activity_display()} analysis"
