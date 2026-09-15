from secrets import token_urlsafe

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from training.models import VisionDevice


class Command(BaseCommand):
    help = "Register a Raspberry Pi and print its one-time API token."

    def add_arguments(self, parser: object) -> None:
        parser.add_argument("--name", required=True)
        parser.add_argument("--owner", required=True)

    def handle(self, *args: object, **options: object) -> None:
        username = str(options["owner"])
        owner = get_user_model().objects.filter(username=username).first()
        if owner is None:
            raise CommandError(f"User '{username}' does not exist.")
        name = str(options["name"])
        if VisionDevice.objects.filter(name=name).exists():
            raise CommandError(f"A device named '{name}' already exists.")

        token = token_urlsafe(32)
        device = VisionDevice(name=name, owner=owner)
        device.set_token(token)
        device.save()
        self.stdout.write(self.style.SUCCESS(f"Created device '{name}'."))
        self.stdout.write("Copy this token to the Pi now; it will not be shown again:")
        self.stdout.write(token)
