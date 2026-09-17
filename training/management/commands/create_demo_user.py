import os
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create or update the low-privilege public demo account."

    def handle(self, *args: Any, **options: Any) -> None:
        username = os.environ.get("DEMO_USERNAME", "user")
        password = os.environ.get("DEMO_PASSWORD", "user12345")
        user_model = get_user_model()
        user, created = user_model.objects.get_or_create(
            username=username,
            defaults={
                "email": "demo@trainmate.local",
                "is_active": True,
                "role": "client",
            },
        )
        changed_fields: list[str] = []
        if not user.is_active:
            user.is_active = True
            changed_fields.append("is_active")
        if user.role != "client":
            user.role = "client"
            changed_fields.append("role")
        if created or not user.check_password(password):
            user.set_password(password)
            changed_fields.append("password")
        if changed_fields:
            user.save(update_fields=changed_fields)
        self.stdout.write(self.style.SUCCESS(f"Demo account ready: {username}"))
