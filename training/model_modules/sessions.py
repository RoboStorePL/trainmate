from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone

from .core import Specialization, Trainer


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
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="confirmed_sessions")
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

    @property
    def ends_in_future(self) -> bool:
        return self.starts_at + timedelta(minutes=self.duration_minutes) > timezone.now()

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

    @transaction.atomic
    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.pk:
            previous = type(self).objects.select_for_update().get(pk=self.pk)
            if previous.status == self.Status.COMPLETED:
                frozen = ("status", "trainer_id", "starts_at", "duration_minutes", "completed_at", "completed_by_id")
                if any(getattr(previous, field) != getattr(self, field) for field in frozen):
                    raise ValidationError("Confirmed sessions are fixed. Record an earnings correction instead.")
        if self.status == self.Status.COMPLETED and self.completed_at is None:
            self.completed_at = timezone.now()
            if kwargs.get("update_fields"):
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"completed_at"}
        super().save(*args, **kwargs)
        if self.status == self.Status.COMPLETED:
            from .payroll import SalaryAccrual

            participant_count = self.attendance_records.filter(
                status=SessionAttendance.Status.ATTENDED, user__in=self.participants.all(),
            ).count()
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
                accrual.trainer = self.trainer
                accrual.participant_count = participant_count
                accrual.rate_per_participant = self.trainer.rate_per_participant
                accrual.amount = participant_count * self.trainer.rate_per_participant
                accrual.status = SalaryAccrual.Status.READY
                accrual.save(update_fields=[
                    "trainer", "participant_count", "rate_per_participant", "amount", "status",
                ])


class SessionAttendance(models.Model):
    class Status(models.TextChoices):
        BOOKED = "booked", "Booked"
        ATTENDED = "attended", "Attended"
        ABSENT = "absent", "Absent"
        CANCELLED = "cancelled", "Cancelled"

    session = models.ForeignKey(
        TrainingSession, on_delete=models.CASCADE, related_name="attendance_records",
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.BOOKED)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("session", "user"), name="unique_session_attendance"),
        ]
        ordering = ["user__username", "pk"]

    def mark_attended(self) -> None:
        self.status = self.Status.ATTENDED
        self.checked_in_at = timezone.now()
        self.save(update_fields=["status", "checked_in_at", "updated_at"])


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



