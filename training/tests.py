from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Specialization, Trainer, TrainingSession


class TrainMateTests(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = get_user_model().objects.create_user(username="member")
        cls.discipline = Specialization.objects.create(name="Yoga")
        cls.trainer = Trainer.objects.create(name="Taylor")
        cls.trainer.specializations.add(cls.discipline)
        cls.session = TrainingSession.objects.create(
            title="Morning flow", trainer=cls.trainer,
            specialization=cls.discipline, capacity=1,
            starts_at=timezone.now() + timedelta(days=1),
            location="Studio",
        )

    def setUp(self) -> None:
        self.client.force_login(self.user)

    def test_pages_render(self) -> None:
        names = ["home", "session-list", "trainer-list", "specialization-list",
                 "profile"]
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(
                    reverse(f"training:{name}"),
                ).status_code, 200)

    def test_booking_and_cancellation(self) -> None:
        url = reverse("training:book", args=[self.session.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.post(url)
        self.client.post(url)
        self.assertEqual(self.session.participants.count(), 1)
        self.client.post(reverse("training:cancel", args=[self.session.pk]))
        self.assertEqual(self.session.participants.count(), 0)

    def test_full_session(self) -> None:
        other = get_user_model().objects.create_user(username="other")
        self.session.participants.add(other)
        self.client.post(reverse("training:book", args=[self.session.pk]))
        self.assertFalse(self.session.participants.filter(pk=self.user.pk).exists())

    def test_past_session(self) -> None:
        self.session.starts_at = timezone.now() - timedelta(days=1)
        self.session.save()
        self.client.post(reverse("training:book", args=[self.session.pk]))
        self.assertEqual(self.session.participants.count(), 0)

    def test_search(self) -> None:
        response = self.client.get(reverse("training:session-list"), {"q": "FLOW"})
        self.assertContains(response, "Morning flow")
        response = self.client.get(reverse("training:session-list"), {"q": "missing"})
        self.assertEqual(response.context["paginator"].count, 0)

    def test_client_cannot_manage_catalog(self) -> None:
        self.assertEqual(self.client.get(reverse("training:session-create")).status_code, 403)
        self.assertEqual(self.client.get(reverse("training:trainer-create")).status_code, 403)
        self.assertEqual(self.client.get(reverse("training:specialization-create")).status_code, 403)

    def test_trainer_can_manage_only_own_sessions(self) -> None:
        trainer_user = get_user_model().objects.create_user(
            username="taylor", role="trainer",
        )
        self.trainer.user = trainer_user
        self.trainer.save()
        self.client.force_login(trainer_user)
        response = self.client.post(
            reverse("training:trainer-update", args=[self.trainer.pk]),
            {"name": "New name", "bio": "", "experience_years": 3,
             "specializations": [self.discipline.pk]},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get(
            reverse("training:session-update", args=[self.session.pk]),
        ).status_code, 200)

    def test_superuser_can_manage_catalog(self) -> None:
        admin = get_user_model().objects.create_superuser(
            username="Mixon", password="test-password",
        )
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse("training:trainer-create")).status_code, 200)
        self.assertEqual(self.client.get(reverse("training:specialization-create")).status_code, 200)

    def test_only_superuser_can_view_clients(self) -> None:
        self.assertEqual(self.client.get(reverse("training:client-list")).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="Mixon", password="test-password",
        )
        self.client.force_login(admin)
        response = self.client.get(reverse("training:client-list"))
        self.assertContains(response, "member")

    def test_login_required(self) -> None:
        self.client.logout()
        self.assertEqual(self.client.get("/sessions/").status_code, 302)
