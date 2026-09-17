import os
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create or update the initial Django administrator from environment variables."

    def handle(self, *args: Any, **options: Any) -> None:
        username = os.environ.get("ADMIN_USERNAME")
        password = os.environ.get("ADMIN_PASSWORD")
        if not username or not password:
            self.stdout.write("Initial admin skipped: ADMIN_USERNAME and ADMIN_PASSWORD are not set.")
            return

        user_model = get_user_model()
        user, created = user_model.objects.get_or_create(
            username=username,
            defaults={
                "email": os.environ.get("ADMIN_EMAIL", "admin@trainmate.local"),
                "is_active": True,
                "is_staff": True,
                "is_superuser": True,
                "role": "client",
            },
        )
        changed_fields: list[str] = []
        for field in ("is_active", "is_staff", "is_superuser"):
            if not getattr(user, field):
                setattr(user, field, True)
                changed_fields.append(field)
        if not user.check_password(password):
            user.set_password(password)
            changed_fields.append("password")
        if changed_fields:
            user.save(update_fields=changed_fields)
        action = "created" if created else "ready"
        self.stdout.write(self.style.SUCCESS(f"Initial admin {action}: {username}"))
