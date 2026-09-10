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
    fitness_level = models.CharField(
        max_length=20, default="beginner",
        choices=[("beginner", "Beginner"), ("intermediate", "Intermediate"),
                 ("advanced", "Advanced")],
    )

    def get_absolute_url(self):
        return reverse("training:profile")


class Specialization(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Trainer(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="trainer_profile",
    )
    name = models.CharField(max_length=150)
    bio = models.TextField(blank=True)
    experience_years = models.PositiveIntegerField(default=0)
    specializations = models.ManyToManyField(
        Specialization, related_name="trainers",
    )

    class Meta:
        ordering = ["name", "pk"]

    def __str__(self):
        return self.name


class TrainingSession(models.Model):
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
    participants = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="training_sessions",
    )

    class Meta:
        ordering = ["starts_at", "pk"]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("training:session-detail", args=[self.pk])

    def clean(self):
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
