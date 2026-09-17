from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.urls import reverse


class User(AbstractUser):
    class Role(models.TextChoices):
        CLIENT = "client", "Client"
        TRAINER = "trainer", "Trainer"
        KIOSK = "kiosk", "Reception kiosk"

    role = models.CharField(max_length=10, choices=Role.choices, default=Role.CLIENT)
    balance = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    fitness_level = models.CharField(
        max_length=20, default="beginner",
        choices=[("beginner", "Beginner"), ("intermediate", "Intermediate"),
                 ("advanced", "Advanced")],
    )

    def get_absolute_url(self) -> str:
        return reverse("training:profile")

    @property
    def is_trainer(self) -> bool:
        """A linked profile grants trainer access; kiosk accounts stay restricted."""
        return self.is_active and self.role != self.Role.KIOSK and hasattr(self, "trainer_profile")


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



