from datetime import time, timedelta
from decimal import Decimal
import json
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import (
    BalanceTransaction, Membership, MembershipPlan, SalaryAccrual,
    RecurringSchedule, SessionAttendance, Specialization, Trainer, TrainingSession,
    TrainerPayout, SalaryAdjustment, VisionAnalysis, VisionDevice,
)


class TrainMateTests(TestCase):
    def confirm_session(self):
        url = reverse("training:session-complete", args=[self.session.pk])
        preview = self.client.get(url)
        return self.client.post(url, {"selection": preview.context["selection"]})

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
        cls.membership = Membership.objects.create(
            user=cls.user, title="Test membership", sessions_total=5,
            sessions_remaining=5, price=100,
            expires_on=timezone.localdate() + timedelta(days=30),
        )

    def setUp(self) -> None:
        self.client.force_login(self.user)

    def test_pages_render(self) -> None:
        names = ["home", "session-list", "trainer-list", "specialization-list",
                 "profile", "vision-dashboard"]
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(
                    reverse(f"training:{name}"),
                ).status_code, 200)

    def test_regular_schedule_creates_only_three_weeks_of_bookable_sessions(self) -> None:
        today = timezone.localdate()
        first_date = today + timedelta(days=1)
        schedule = RecurringSchedule.objects.create(
            title="Weekly yoga", trainer=self.trainer, specialization=self.discipline,
            weekdays=[first_date.weekday()], start_time=time(12, 0),
            duration_minutes=60, capacity=5, location="Studio", starts_on=first_date,
        )

        self.assertEqual(schedule.sync_sessions(), 3)
        self.assertEqual(schedule.sync_sessions(), 0)
        sessions = list(schedule.sessions.order_by("starts_at"))
        self.assertEqual(len(sessions), 3)
        self.assertTrue(all(session.recurring_schedule_id == schedule.pk for session in sessions))
        self.assertTrue(all(
            timezone.localtime(session.starts_at).date() <= today + timedelta(days=20)
            for session in sessions
        ))

        response = self.client.post(reverse("training:book", args=[sessions[0].pk]))
        self.assertRedirects(response, sessions[0].get_absolute_url())
        self.assertTrue(sessions[0].participants.filter(pk=self.user.pk).exists())

    def test_reception_kiosk_checks_in_a_booked_client(self) -> None:
        kiosk = get_user_model().objects.create_user(
            username="reception", password="tablet-password", role="kiosk",
        )
        today_session = TrainingSession.objects.create(
            title="Evening yoga", trainer=self.trainer, specialization=self.discipline,
            starts_at=timezone.now(), location="Studio",
        )
        today_session.participants.add(self.user)
        self.client.force_login(kiosk)

        response = self.client.get(reverse("training:reception-session-list"))
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            reverse("training:kiosk-check-in", args=[today_session.pk, self.user.pk]),
        )
        self.assertRedirects(response, reverse("training:reception-check-in", args=[today_session.pk]))
        self.assertEqual(
            SessionAttendance.objects.get(session=today_session, user=self.user).status,
            SessionAttendance.Status.ATTENDED,
        )
        response = self.client.post(
            reverse("training:kiosk-undo-check-in", args=[today_session.pk, self.user.pk]),
        )
        self.assertRedirects(response, reverse("training:reception-check-in", args=[today_session.pk]))
        attendance = SessionAttendance.objects.get(session=today_session, user=self.user)
        self.assertEqual(attendance.status, SessionAttendance.Status.BOOKED)
        self.assertIsNone(attendance.checked_in_at)
        self.assertRedirects(self.client.get(reverse("training:home")), reverse("training:reception-session-list"))

    def test_manager_can_correct_attendance(self) -> None:
        self.session.participants.add(self.user)
        admin = get_user_model().objects.create_superuser(
            username="attendance-admin", password="test-password",
        )
        self.client.force_login(admin)
        response = self.client.post(
            reverse("training:attendance-update", args=[self.session.pk, self.user.pk]),
            {"status": "absent"},
        )
        self.assertRedirects(response, self.session.get_absolute_url())
        self.assertEqual(
            SessionAttendance.objects.get(session=self.session, user=self.user).status,
            SessionAttendance.Status.ABSENT,
        )

    def test_vision_device_can_submit_pose_result_for_its_owner(self) -> None:
        device = VisionDevice(name="test-pi", owner=self.user)
        device.set_token("test-device-token")
        device.save()

        response = self.client.post(
            reverse("training:vision-ingest"),
            data=json.dumps({
                "activity": "yoga", "pose_score": 88.5,
                "feedback": "Pose captured clearly.",
                "landmarks": {"left_shoulder": {"visibility": 0.9}},
            }),
            content_type="application/json",
            headers={"Authorization": "Bearer test-device-token"},
        )

        self.assertEqual(response.status_code, 201)
        analysis = VisionAnalysis.objects.get(device=device)
        self.assertEqual(analysis.user, self.user)
        self.assertEqual(analysis.pose_score, Decimal("88.50"))
        self.assertIsNotNone(VisionDevice.objects.get(pk=device.pk).last_seen_at)

    def test_vision_endpoint_rejects_an_invalid_token(self) -> None:
        response = self.client.post(
            reverse("training:vision-ingest"), data="{}",
            content_type="application/json",
            headers={"Authorization": "Bearer invalid-token"},
        )
        self.assertEqual(response.status_code, 401)

    def test_vision_owner_can_view_a_short_lived_live_frame(self) -> None:
        device = VisionDevice(name="live-pi", owner=self.user)
        device.set_token("live-device-token")
        device.save()
        frame = b"\xff\xd8test-jpeg-frame\xff\xd9"

        upload = self.client.post(
            reverse("training:vision-live-ingest"), data=frame,
            content_type="image/jpeg",
            headers={"Authorization": "Bearer live-device-token"},
        )

        self.assertEqual(upload.status_code, 204)
        preview = self.client.get(
            reverse("training:vision-live-frame", args=[device.pk]),
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.content, frame)
        self.assertEqual(preview["Cache-Control"], "no-store, max-age=0")

    def test_vision_device_can_attach_one_progress_snapshot(self) -> None:
        device = VisionDevice(name="snapshot-pi", owner=self.user)
        device.set_token("snapshot-device-token")
        device.save()
        analysis = VisionAnalysis.objects.create(
            user=self.user, device=device, activity=VisionAnalysis.Activity.YOGA,
            pose_score=Decimal("90.00"),
        )
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            upload = self.client.post(
                reverse("training:vision-snapshot", args=[analysis.pk]),
                data=b"\xff\xd8saved-frame\xff\xd9", content_type="image/jpeg",
                headers={"Authorization": "Bearer snapshot-device-token"},
            )
            self.assertEqual(upload.status_code, 201)
            analysis.refresh_from_db()
            self.assertTrue(analysis.snapshot.name)
            snapshot = self.client.get(
                reverse("training:vision-snapshot-view", args=[analysis.pk]),
            )
            self.assertEqual(snapshot.status_code, 200)
            self.assertEqual(b"".join(snapshot.streaming_content), b"\xff\xd8saved-frame\xff\xd9")

    def test_booking_and_cancellation(self) -> None:
        url = reverse("training:book", args=[self.session.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.post(url)
        self.client.post(url)
        self.assertEqual(self.session.participants.count(), 1)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.sessions_remaining, 4)
        accrual = SalaryAccrual.objects.get(session=self.session)
        self.assertEqual(accrual.status, SalaryAccrual.Status.ACCRUED)
        self.assertEqual(accrual.amount, 10)
        self.client.post(reverse("training:cancel", args=[self.session.pk]))
        self.assertEqual(self.session.participants.count(), 0)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.sessions_remaining, 5)
        self.assertFalse(SalaryAccrual.objects.filter(session=self.session).exists())

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
        self.assertEqual(self.client.get(reverse("training:recurring-schedule-list")).status_code, 200)

    def test_admin_can_link_only_trainer_user_to_profile(self) -> None:
        trainer_user = get_user_model().objects.create_user(
            username="new_trainer", role="trainer",
        )
        client_user = get_user_model().objects.create_user(username="new_client")
        admin = get_user_model().objects.create_superuser(
            username="Mixon", password="test-password",
        )
        self.client.force_login(admin)
        response = self.client.get(reverse("training:trainer-create"))
        self.assertContains(response, "new_trainer")
        self.assertNotContains(response, "new_client")
        response = self.client.post(
            reverse("training:trainer-create"),
            {"user": trainer_user.pk, "name": "New trainer", "bio": "",
             "experience_years": 1, "rate_per_participant": "10.00",
             "specializations": [self.discipline.pk]},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Trainer.objects.filter(user=trainer_user).exists())

    def test_completed_session_creates_salary_accrual(self) -> None:
        self.session.participants.add(self.user)
        SessionAttendance.objects.create(session=self.session, user=self.user, status="attended")
        self.session.status = TrainingSession.Status.COMPLETED
        self.session.save()
        accrual = SalaryAccrual.objects.get(session=self.session)
        self.assertEqual(accrual.participant_count, 1)
        self.assertEqual(accrual.amount, 10)
        self.assertEqual(accrual.status, SalaryAccrual.Status.READY)

    def test_admin_or_assigned_trainer_can_confirm_session_and_salary(self) -> None:
        self.client.post(reverse("training:book", args=[self.session.pk]))
        self.assertEqual(
            self.client.post(
                reverse("training:session-complete", args=[self.session.pk]),
            ).status_code,
            403,
        )
        trainer_user = get_user_model().objects.create_user(
            username="assigned-trainer", role="trainer",
        )
        self.trainer.user = trainer_user
        self.trainer.save()
        self.client.force_login(trainer_user)
        response = self.confirm_session()
        self.assertEqual(response.status_code, 302)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, TrainingSession.Status.COMPLETED)
        self.assertEqual(
            SalaryAccrual.objects.get(session=self.session).status,
            SalaryAccrual.Status.READY,
        )

    def test_admin_can_confirm_session_and_salary(self) -> None:
        self.client.post(reverse("training:book", args=[self.session.pk]))
        admin = get_user_model().objects.create_superuser(
            username="Mixon", password="test-password",
        )
        self.client.force_login(admin)
        response = self.confirm_session()
        self.assertEqual(response.status_code, 302)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, TrainingSession.Status.COMPLETED)
        self.assertEqual(
            SalaryAccrual.objects.get(session=self.session).status,
            SalaryAccrual.Status.READY,
        )
        self.assertIsNotNone(
            SalaryAccrual.objects.get(session=self.session).confirmed_at,
        )

    def test_session_manager_sees_confirm_button_for_scheduled_session(self) -> None:
        admin = get_user_model().objects.create_superuser(
            username="Mixon", password="test-password",
        )
        self.client.force_login(admin)
        response = self.client.get(
            reverse("training:session-detail", args=[self.session.pk]),
        )
        self.assertContains(response, "Confirm session")

    def test_trainer_cannot_complete_their_session_through_the_edit_form(self) -> None:
        trainer_user = get_user_model().objects.create_user(
            username="taylor", role="trainer",
        )
        self.trainer.user = trainer_user
        self.trainer.save()
        self.client.force_login(trainer_user)

        response = self.client.post(
            reverse("training:session-update", args=[self.session.pk]),
            {
                "title": self.session.title,
                "description": self.session.description,
                "specialization": self.discipline.pk,
                "starts_at": self.session.starts_at.strftime("%Y-%m-%dT%H:%M"),
                "duration_minutes": self.session.duration_minutes,
                "capacity": self.session.capacity,
                "location": self.session.location,
                "status": TrainingSession.Status.COMPLETED,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, TrainingSession.Status.SCHEDULED)

    def test_confirmed_booking_cannot_be_cancelled_or_refunded(self) -> None:
        self.client.post(reverse("training:book", args=[self.session.pk]))
        admin = get_user_model().objects.create_superuser(
            username="Mixon", password="test-password",
        )
        self.client.force_login(admin)
        self.confirm_session()

        self.client.force_login(self.user)
        response = self.client.post(reverse("training:cancel", args=[self.session.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.session.participants.filter(pk=self.user.pk).exists())
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.sessions_remaining, 4)
        self.assertEqual(
            SalaryAccrual.objects.get(session=self.session).status,
            SalaryAccrual.Status.READY,
        )

    def test_completed_session_cannot_be_edited(self) -> None:
        self.session.status = TrainingSession.Status.COMPLETED
        self.session.save()
        admin = get_user_model().objects.create_superuser(
            username="Mixon", password="test-password",
        )
        self.client.force_login(admin)

        self.assertEqual(
            self.client.get(reverse("training:session-update", args=[self.session.pk])).status_code,
            404,
        )

    def test_session_delete_uses_a_confirmation_screen(self) -> None:
        admin = get_user_model().objects.create_superuser(
            username="Mixon", password="test-password",
        )
        self.client.force_login(admin)
        response = self.client.get(reverse("training:session-delete", args=[self.session.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Confirmation required")
        self.assertContains(response, "Yes, delete")
        self.assertNotContains(response, "Update Morning flow")

    def test_session_without_salary_record_is_deleted_after_confirmation(self) -> None:
        admin = get_user_model().objects.create_superuser(
            username="Mixon", password="test-password",
        )
        self.client.force_login(admin)
        response = self.client.post(reverse("training:session-delete", args=[self.session.pk]))

        self.assertRedirects(response, reverse("training:session-list"))
        self.assertFalse(TrainingSession.objects.filter(pk=self.session.pk).exists())

    def test_session_with_salary_record_cannot_be_deleted(self) -> None:
        SalaryAccrual.objects.create(
            trainer=self.trainer, session=self.session, participant_count=1,
            rate_per_participant=10, amount=10,
        )
        admin = get_user_model().objects.create_superuser(
            username="Mixon", password="test-password",
        )
        self.client.force_login(admin)
        response = self.client.post(reverse("training:session-delete", args=[self.session.pk]))

        self.assertRedirects(response, reverse("training:session-list"))
        self.assertTrue(TrainingSession.objects.filter(pk=self.session.pk).exists())

    def test_salary_status_cannot_skip_or_reverse_the_payout_lifecycle(self) -> None:
        accrual = SalaryAccrual.objects.create(
            trainer=self.trainer, session=self.session, participant_count=1,
            rate_per_participant=10, amount=10,
        )
        accrual.status = SalaryAccrual.Status.PAID
        with self.assertRaises(ValidationError):
            accrual.save()

        accrual.status = SalaryAccrual.Status.READY
        accrual.save()
        self.assertIsNotNone(accrual.confirmed_at)
        accrual.status = SalaryAccrual.Status.PAID
        accrual.save()
        self.assertIsNotNone(accrual.paid_at)
        accrual.status = SalaryAccrual.Status.READY
        with self.assertRaises(ValidationError):
            accrual.save()

    def test_trainer_earnings_show_hold_and_confirmed_totals(self) -> None:
        trainer_user = get_user_model().objects.create_user(
            username="taylor", role="trainer",
        )
        self.trainer.user = trainer_user
        self.trainer.save()
        SalaryAccrual.objects.create(
            trainer=self.trainer, session=self.session, participant_count=1,
            rate_per_participant=10, amount=10,
        )
        confirmed_session = TrainingSession.objects.create(
            title="Evening flow", trainer=self.trainer,
            specialization=self.discipline, capacity=1,
            starts_at=timezone.now() + timedelta(days=2), location="Studio",
        )
        SalaryAccrual.objects.create(
            trainer=self.trainer, session=confirmed_session, participant_count=2,
            rate_per_participant=10, amount=20,
            status=SalaryAccrual.Status.READY,
        )
        paid_session = TrainingSession.objects.create(
            title="Weekend flow", trainer=self.trainer,
            specialization=self.discipline, capacity=1,
            starts_at=timezone.now() + timedelta(days=3), location="Studio",
        )
        SalaryAccrual.objects.create(
            trainer=self.trainer, session=paid_session, participant_count=3,
            rate_per_participant=10, amount=30,
            status=SalaryAccrual.Status.PAID,
        )
        self.client.force_login(trainer_user)
        response = self.client.get(reverse("training:trainer-earnings"))
        self.assertEqual(response.context["hold_total"], 10)
        self.assertEqual(response.context["confirmed_total"], 20)
        self.assertEqual(response.context["paid_total"], 30)
        self.assertEqual(response.context["outstanding_total"], 20)
        self.assertEqual(response.context["total_earned"], 50)

    def test_admin_payout_page_groups_totals_by_trainer(self) -> None:
        SalaryAccrual.objects.create(
            trainer=self.trainer, session=self.session, participant_count=1,
            rate_per_participant=10, amount=10,
        )
        admin = get_user_model().objects.create_superuser(
            username="payout-admin", password="test-password",
        )
        self.client.force_login(admin)
        response = self.client.get(reverse("training:payout-list"))

        self.assertEqual(response.context["accrued_total"], 10)
        self.assertEqual(response.context["outstanding_total"], 0)
        self.assertEqual(response.context["trainer_totals"][0].total_earned, 0)

    def test_admin_can_pay_all_ready_sessions_to_one_trainer_in_a_batch(self) -> None:
        ready_one = SalaryAccrual.objects.create(
            trainer=self.trainer, session=self.session, participant_count=1,
            rate_per_participant=10, amount=10, status=SalaryAccrual.Status.READY,
        )
        second_session = TrainingSession.objects.create(
            title="Second class", trainer=self.trainer, specialization=self.discipline,
            starts_at=timezone.now() + timedelta(days=2), location="Studio",
        )
        ready_two = SalaryAccrual.objects.create(
            trainer=self.trainer, session=second_session, participant_count=2,
            rate_per_participant=10, amount=20, status=SalaryAccrual.Status.READY,
        )
        admin = get_user_model().objects.create_superuser(
            username="batch-payout-admin", password="test-password",
        )
        self.client.force_login(admin)

        url = reverse("training:trainer-payout-create", args=[self.trainer.pk])
        preview = self.client.get(url)
        self.assertContains(preview, "30.00 PLN")
        data = {"note": "September transfer", "method": "transfer", "selection": preview.context["form"].initial["selection"]}
        response = self.client.post(url, data)

        self.assertRedirects(response, reverse("training:payout-list"))
        payout = TrainerPayout.objects.get(trainer=self.trainer)
        self.assertEqual(payout.amount, 30)
        self.assertEqual(payout.note, "September transfer")
        ready_one.refresh_from_db()
        ready_two.refresh_from_db()
        self.assertEqual(ready_one.status, SalaryAccrual.Status.PAID)
        self.assertEqual(ready_two.status, SalaryAccrual.Status.PAID)
        self.assertEqual(ready_one.payout, payout)
        self.assertEqual(ready_two.payout, payout)
        self.assertEqual(payout.recorded_by, admin)
        self.client.post(url, data)
        self.assertEqual(TrainerPayout.objects.count(), 1)

    def test_confirmation_uses_attendance_and_freezes_rate(self) -> None:
        other = get_user_model().objects.create_user(username="absent-client")
        self.session.participants.add(self.user, other)
        SessionAttendance.objects.create(session=self.session, user=self.user, status="attended")
        SessionAttendance.objects.create(session=self.session, user=other, status="absent")
        admin = get_user_model().objects.create_superuser(username="review-admin")
        self.client.force_login(admin)
        url = reverse("training:session-complete", args=[self.session.pk])
        preview = self.client.get(url)
        self.assertEqual(preview.context["amount"], 10)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "scheduled")
        self.confirm_session()
        accrual = SalaryAccrual.objects.get(session=self.session)
        self.assertEqual(accrual.amount, 10)
        self.assertEqual(accrual.participant_count, 1)
        self.session.refresh_from_db()
        self.assertEqual(self.session.completed_by, admin)
        self.trainer.rate_per_participant = 50
        self.trainer.save()
        self.session.save()
        accrual.refresh_from_db()
        self.assertEqual(accrual.amount, 10)
        accrual.amount = 50
        with self.assertRaises(ValidationError):
            accrual.save()

    def test_confirmation_rejects_changed_attendance_preview(self) -> None:
        self.session.participants.add(self.user)
        admin = get_user_model().objects.create_superuser(username="review-admin")
        self.client.force_login(admin)
        url = reverse("training:session-complete", args=[self.session.pk])
        preview = self.client.get(url)
        SessionAttendance.objects.create(session=self.session, user=self.user, status="attended")
        self.client.post(url, {"selection": preview.context["selection"]})
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "scheduled")
        self.assertFalse(SalaryAccrual.objects.filter(session=self.session).exists())

    def test_payment_period_corrections_and_history_permissions(self) -> None:
        first = SalaryAccrual.objects.create(trainer=self.trainer, session=self.session, participant_count=2, rate_per_participant=10, amount=20, status="ready")
        later = TrainingSession.objects.create(title="Later class", trainer=self.trainer, specialization=self.discipline, starts_at=self.session.starts_at + timedelta(days=30), location="Studio")
        excluded = SalaryAccrual.objects.create(trainer=self.trainer, session=later, participant_count=3, rate_per_participant=10, amount=30, status="ready")
        admin = get_user_model().objects.create_superuser(username="finance-admin")
        self.client.force_login(admin)
        correction_url = reverse("training:salary-adjustment-create", args=[first.pk])
        self.assertEqual(self.client.get(correction_url).status_code, 200)
        self.client.post(correction_url, {"amount": "-5", "reason": "Agreed correction"})
        self.assertEqual(self.client.get(reverse("training:payout-list")).context["ready_total"], 45)
        url = reverse("training:trainer-payout-create", args=[self.trainer.pk])
        period = {"starts_on": timezone.localdate(self.session.starts_at).isoformat(), "ends_on": timezone.localdate(self.session.starts_at).isoformat()}
        preview = self.client.get(url, period)
        self.assertEqual(preview.context["payout_amount"], 15)
        self.client.post(url, {**period, "method": "cash", "selection": preview.context["form"].initial["selection"]})
        payout = TrainerPayout.objects.get()
        self.assertEqual(payout.amount, 15)
        self.assertEqual(payout.adjustments.get().amount, -5)
        excluded.refresh_from_db()
        self.assertEqual(excluded.status, "ready")
        detail = reverse("training:trainer-payout-detail", args=[payout.pk])
        self.assertContains(self.client.get(detail), "Agreed correction")
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(detail).status_code, 404)
        self.assertEqual(self.client.post(correction_url, {"amount": "5", "reason": "No access"}).status_code, 403)
        trainer_user = get_user_model().objects.create_user(username="finance-trainer", role="trainer")
        self.trainer.user = trainer_user
        self.trainer.save()
        self.client.force_login(trainer_user)
        self.assertContains(self.client.get(detail), "15.00 PLN")
        earnings = self.client.get(reverse("training:trainer-earnings"))
        self.assertEqual(earnings.context["confirmed_total"], 30)
        self.assertEqual(earnings.context["paid_total"], 15)

    def test_new_ready_record_invalidates_payment_preview(self) -> None:
        first = SalaryAccrual.objects.create(trainer=self.trainer, session=self.session, participant_count=1, rate_per_participant=10, amount=10, status="ready")
        admin = get_user_model().objects.create_superuser(username="finance-admin")
        self.client.force_login(admin)
        url = reverse("training:trainer-payout-create", args=[self.trainer.pk])
        preview = self.client.get(url)
        SalaryAdjustment.objects.create(accrual=first, amount=5, reason="Additional attendee", created_by=admin)
        response = self.client.post(url, {"method": "cash", "selection": preview.context["form"].initial["selection"]})
        self.assertContains(response, "selection changed")
        self.assertFalse(TrainerPayout.objects.exists())

    def test_demo_balance_can_purchase_membership(self) -> None:
        plan = MembershipPlan.objects.create(
            title="Yoga starter", sessions_count=4, duration_days=30, price=40,
        )
        response = self.client.post(reverse("training:add-balance"), {"amount": "50"})
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, 50)
        response = self.client.post(
            reverse("training:purchase-membership", args=[plan.pk]),
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, 10)
        purchased = Membership.objects.get(user=self.user, plan=plan)
        self.assertEqual(purchased.sessions_remaining, 4)
        self.assertEqual(BalanceTransaction.objects.filter(user=self.user).count(), 2)

    def test_only_superuser_can_view_clients(self) -> None:
        self.assertEqual(self.client.get(reverse("training:client-list")).status_code, 403)
        admin = get_user_model().objects.create_superuser(
            username="Mixon", password="test-password",
        )
        self.client.force_login(admin)
        response = self.client.get(reverse("training:client-list"))
        self.assertContains(response, "member")

    def test_admin_balance_adjustment_creates_a_transaction(self) -> None:
        admin = get_user_model().objects.create_superuser(
            username="Mixon", password="test-password",
        )
        self.client.force_login(admin)
        response = self.client.post(
            reverse("training:adjust-client-balance", args=[self.user.pk]),
            {"amount": "25.50", "description": "Welcome credit"},
        )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal("25.50"))
        transaction = BalanceTransaction.objects.get(user=self.user)
        self.assertEqual(transaction.kind, BalanceTransaction.Kind.ADMIN)
        self.assertIn("Welcome credit", transaction.description)

    def test_login_required(self) -> None:
        self.client.logout()
        self.assertEqual(self.client.get("/sessions/").status_code, 302)

    def test_signup_requires_email_and_sends_activation_link(self) -> None:
        response = self.client.post(
            reverse("training:signup"),
            {
                "username": "new_member", "email": "new@example.com",
                "first_name": "New", "last_name": "Member",
                "fitness_level": "beginner",
                "password1": "strong-test-password-123",
                "password2": "strong-test-password-123",
            },
        )

        self.assertRedirects(response, reverse("training:activation-sent"))
        new_user = get_user_model().objects.get(username="new_member")
        self.assertFalse(new_user.is_active)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Activate your TrainMate account", mail.outbox[0].subject)
        self.assertIn("activate/", mail.outbox[0].body)

    def test_activation_link_activates_account(self) -> None:
        inactive_user = get_user_model().objects.create_user(
            username="inactive", email="inactive@example.com", is_active=False,
        )
        uid = urlsafe_base64_encode(force_bytes(inactive_user.pk))
        token = default_token_generator.make_token(inactive_user)

        response = self.client.get(
            reverse("training:activate-account", args=[uid, token]),
        )

        self.assertRedirects(response, reverse("login"))
        inactive_user.refresh_from_db()
        self.assertTrue(inactive_user.is_active)

    def test_password_reset_sends_email_and_is_rate_limited(self) -> None:
        cache.clear()
        self.user.email = "member@example.com"
        self.user.set_password("strong-test-password-123")
        self.user.save(update_fields=["email", "password"])
        response = self.client.post(reverse("password_reset"), {"email": self.user.email})
        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("reset/", mail.outbox[0].body)

        for _ in range(4):
            self.client.post(reverse("password_reset"), {"email": self.user.email})
        response = self.client.post(reverse("password_reset"), {"email": self.user.email})
        self.assertRedirects(response, reverse("password_reset"))

    def test_login_is_rate_limited_after_five_failed_attempts(self) -> None:
        cache.clear()
        self.client.logout()
        for _ in range(5):
            response = self.client.post(
                reverse("login"), {"username": self.user.username, "password": "wrong"},
            )
            self.assertEqual(response.status_code, 200)
        response = self.client.post(
            reverse("login"), {"username": self.user.username, "password": "wrong"},
        )
        self.assertRedirects(response, reverse("login"))
