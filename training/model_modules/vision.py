from decimal import Decimal
from hashlib import sha256
from secrets import compare_digest

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


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

