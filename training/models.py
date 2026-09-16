from datetime import datetime, timedelta
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
    recurring_schedule = models.ForeignKey(
        "RecurringSchedule", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sessions",
    )
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
        constraints = [
            models.UniqueConstraint(
                fields=("recurring_schedule", "starts_at"),
                name="unique_recurring_schedule_occurrence",
            ),
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def weekday_labels(self) -> str:
        labels = dict(self.DAYS_OF_WEEK)
        return ", ".join(labels[day] for day in self.weekdays)

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


class RecurringSchedule(models.Model):
    """A trainer rule that materializes bookable sessions for the next 21 days."""

    DAYS_OF_WEEK = (
        (0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"),
        (4, "Friday"), (5, "Saturday"), (6, "Sunday"),
    )
    HORIZON_DAYS = 21

    title = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    trainer = models.ForeignKey(Trainer, on_delete=models.PROTECT)
    specialization = models.ForeignKey(Specialization, on_delete=models.PROTECT)
    weekdays = models.JSONField(default=list)
    start_time = models.TimeField()
    duration_minutes = models.PositiveIntegerField(
        default=60, validators=[MinValueValidator(1)],
    )
    capacity = models.PositiveIntegerField(
        default=10, validators=[MinValueValidator(1)],
    )
    location = models.CharField(max_length=200)
    starts_on = models.DateField(default=timezone.localdate)
    ends_on = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["trainer__name", "title", "pk"]

    def __str__(self) -> str:
        return self.title

    def clean(self) -> None:
        super().clean()
        if not self.weekdays:
            raise ValidationError({"weekdays": "Choose at least one day."})
        if any(day not in range(7) for day in self.weekdays):
            raise ValidationError({"weekdays": "Choose valid weekdays."})
        if self.ends_on and self.ends_on < self.starts_on:
            raise ValidationError({"ends_on": "End date must be after the start date."})
        if self.trainer_id and self.specialization_id and not self.trainer.specializations.filter(
            pk=self.specialization_id,
        ).exists():
            raise ValidationError({"trainer": "Trainer must teach this specialization."})

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.weekdays = sorted({int(day) for day in self.weekdays})
        super().save(*args, **kwargs)

    def sync_sessions(self, *, refresh: bool = False) -> int:
        """Create missing occurrences, optionally refreshing safe unbooked future ones."""
        if not self.is_active:
            return 0

        today = timezone.localdate()
        window_end = today + timedelta(days=self.HORIZON_DAYS - 1)
        if self.ends_on:
            window_end = min(window_end, self.ends_on)
        window_start = max(today, self.starts_on)
        if window_start > window_end:
            return 0

        if refresh:
            # An unbooked future occurrence can safely follow changed rule details.
            self.sessions.filter(
                starts_at__gte=timezone.now(), status=TrainingSession.Status.SCHEDULED,
                starts_at__date__lte=window_end, participants__isnull=True,
            ).delete()

        existing_starts = set(self.sessions.filter(
            starts_at__date__gte=window_start, starts_at__date__lte=window_end,
        ).values_list("starts_at", flat=True))

        occurrences = []
        current = window_start
        while current <= window_end:
            if current.weekday() in self.weekdays:
                starts_at = timezone.make_aware(datetime.combine(current, self.start_time))
                if starts_at not in existing_starts:
                    occurrences.append(TrainingSession(
                        title=self.title, description=self.description, trainer=self.trainer,
                        recurring_schedule=self, specialization=self.specialization,
                        starts_at=starts_at, duration_minutes=self.duration_minutes,
                        capacity=self.capacity, location=self.location,
                    ))
            current += timedelta(days=1)
        TrainingSession.objects.bulk_create(occurrences, ignore_conflicts=True)
        return len(occurrences)


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
