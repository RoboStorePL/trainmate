from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand
from django.utils import timezone

from training.models import MembershipPlan, Specialization, Trainer, TrainingSession


class Command(BaseCommand):
    help = "Create sample disciplines, coaches and upcoming sessions."

    def handle(self, *args: Any, **options: Any) -> None:
        for plan in [
            ("Starter", "A flexible start for trying TrainMate.", 4, 30, 80),
            ("Active", "A regular monthly training rhythm.", 8, 30, 140),
            ("Committed", "More sessions for consistent progress.", 12, 45, 180),
        ]:
            MembershipPlan.objects.get_or_create(
                title=plan[0],
                defaults={
                    "description": plan[1], "sessions_count": plan[2],
                    "duration_days": plan[3], "price": plan[4],
                },
            )
        for index, name in enumerate(["Fitness", "Rehabilitation", "Jiu-jitsu", "Yoga"]):
            discipline, _ = Specialization.objects.get_or_create(
                name=name, defaults={"description": f"Explore guided {name.lower()} sessions."},
            )
            trainer, _ = Trainer.objects.get_or_create(
                name=["Alex Morgan", "Sam Rivera", "Jordan Lee", "Taylor Blake"][index],
                defaults={"experience_years": 5, "bio": "Supportive coaching for your next step."},
            )
            trainer.specializations.add(discipline)
            TrainingSession.objects.get_or_create(
                title=f"{name} essentials",
                defaults={
                    "trainer": trainer, "specialization": discipline,
                    "starts_at": timezone.now() + timedelta(days=index + 1),
                    "location": "TrainMate Studio", "capacity": 8,
                },
            )
        self.stdout.write(self.style.SUCCESS("Demo sessions ready."))
