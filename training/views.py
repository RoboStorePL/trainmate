from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any

from django import forms as django_forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count, F, Q, QuerySet, Sum
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views import generic
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .forms import (
    BalanceAdjustmentForm, ProfileForm, RecurringScheduleForm, SessionForm,
    SignUpForm, TrainerForm, TrainerPayoutForm, TrainerRecurringScheduleForm,
    TrainerSessionForm, SalaryAdjustmentForm,
)
from .models import (
    BalanceTransaction, Membership, MembershipPlan, MembershipUsage,
    RecurringSchedule, SalaryAccrual, SessionAttendance, Specialization, Trainer,
    TrainerPayout, TrainingSession, User, SalaryAdjustment,
    VisionAnalysis, VisionDevice,
)


def is_trainer(user: User) -> bool:
    return user.role == User.Role.TRAINER and hasattr(user, "trainer_profile")


def can_use_reception(user: User) -> bool:
    return user.is_superuser or is_trainer(user) or user.role == User.Role.KIOSK


LOGIN_RATE_LIMIT = 5
RATE_LIMIT_SECONDS = 15 * 60


def rate_limit_key(request: HttpRequest, action: str, identifier: str) -> str:
    raw_key = f"{action}:{request.META.get('REMOTE_ADDR', '')}:{identifier.lower()}"
    return f"trainmate-rate-limit:{sha256(raw_key.encode()).hexdigest()}"


class RateLimitedLoginView(auth_views.LoginView):
    template_name = "registration/login.html"

    def get_limit_key(self) -> str:
        return rate_limit_key(
            self.request, "login", self.request.POST.get("username", ""),
        )

    def dispatch(
        self, request: HttpRequest, *args: Any, **kwargs: Any,
    ) -> HttpResponse:
        if request.method == "POST" and cache.get(self.get_limit_key(), 0) >= LOGIN_RATE_LIMIT:
            messages.error(request, "Too many failed attempts. Try again in 15 minutes.")
            return redirect("login")
        return super().dispatch(request, *args, **kwargs)

    def form_invalid(self, form: Any) -> HttpResponse:
        cache.add(self.get_limit_key(), 0, RATE_LIMIT_SECONDS)
        cache.incr(self.get_limit_key())
        return super().form_invalid(form)

    def form_valid(self, form: Any) -> HttpResponse:
        cache.delete(self.get_limit_key())
        return super().form_valid(form)

    def get_success_url(self) -> str:
        if self.request.user.role == User.Role.KIOSK:
            return reverse_lazy("training:reception-session-list")
        return super().get_success_url()


class RateLimitedPasswordResetView(auth_views.PasswordResetView):
    template_name = "registration/password_reset_form.html"
    email_template_name = "registration/password_reset_email.html"
    subject_template_name = "registration/password_reset_subject.txt"

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        key = rate_limit_key(request, "password-reset", request.POST.get("email", ""))
        if cache.get(key, 0) >= LOGIN_RATE_LIMIT:
            messages.error(request, "Too many reset requests. Try again in 15 minutes.")
            return redirect("password_reset")
        cache.add(key, 0, RATE_LIMIT_SECONDS)
        cache.incr(key)
        return super().post(request, *args, **kwargs)


@login_required
def home(request: HttpRequest) -> HttpResponse:
    visits = request.session.get("num_visits", 0)
    request.session["num_visits"] = visits + 1
    return render(request, "home.html", {
        "num_visits": visits,
        "sessions": TrainingSession.objects.filter(
            starts_at__gt=timezone.now(),
        ).select_related("trainer", "specialization")[:4],
        "session_count": TrainingSession.objects.count(),
        "trainer_count": Trainer.objects.count(),
        "member_count": get_user_model().objects.count(),
    })


class SearchList(LoginRequiredMixin, generic.ListView):
    template_name = "list.html"
    paginate_by = 5
    search_field = "name"

    def get_queryset(self) -> QuerySet[Any]:
        queryset = super().get_queryset()
        query = self.request.GET.get("q", "").strip()
        return queryset.filter(**{f"{self.search_field}__icontains": query})

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        params = self.request.GET.copy()
        params.pop("page", None)
        context.update(
            query_string=params.urlencode(),
            query=self.request.GET.get("q", ""),
            kind=self.kind, title=self.title,
            can_manage_catalog=self.request.user.is_superuser,
            can_create_session=(
                self.request.user.is_superuser
                or is_trainer(self.request.user)
            ),
        )
        return context


class SessionList(SearchList):
    model = TrainingSession
    template_name = "session_list.html"
    queryset = TrainingSession.objects.select_related("trainer", "specialization").prefetch_related("participants")
    # A week rarely contains this many slots, while pagination remains available for search results.
    paginate_by = 100
    search_field = "title"
    kind = "session"
    title = "Training sessions"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        # Keeps each active schedule filled without a background worker.
        for schedule in RecurringSchedule.objects.filter(is_active=True):
            schedule.sync_sessions()
        return super().dispatch(request, *args, **kwargs)

    def get_week_start(self) -> date:
        raw_week = self.request.GET.get("week", "")
        try:
            chosen_day = date.fromisoformat(raw_week) if raw_week else timezone.localdate()
        except ValueError:
            chosen_day = timezone.localdate()
        return chosen_day - timedelta(days=chosen_day.weekday())

    def get_queryset(self) -> QuerySet[TrainingSession]:
        week_start = self.get_week_start()
        week_end = week_start + timedelta(days=7)
        queryset = self.queryset.filter(starts_at__date__gte=week_start, starts_at__date__lt=week_end)
        query = self.request.GET.get("q", "").strip()
        if query:
            queryset = queryset.filter(title__icontains=query)
        return queryset

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        week_start = self.get_week_start()
        sessions = list(context["object_list"])
        days = []
        for offset in range(7):
            day = week_start + timedelta(days=offset)
            days.append({
                "date": day,
                "sessions": [session for session in sessions if timezone.localtime(session.starts_at).date() == day],
            })
        context.update({
            "week_start": week_start,
            "week_end": week_start + timedelta(days=6),
            "previous_week": week_start - timedelta(days=7),
            "next_week": week_start + timedelta(days=7),
            "days": days,
            "today": timezone.localdate(),
            "can_manage_schedules": context["can_create_session"],
        })
        return context


class TrainerList(SearchList):
    model = Trainer
    kind = "trainer"
    title = "Our trainers"


class SpecializationList(SearchList):
    model = Specialization
    kind = "specialization"
    title = "Explore disciplines"


class SessionDetail(LoginRequiredMixin, generic.DetailView):
    model = TrainingSession
    template_name = "session_detail.html"
    queryset = TrainingSession.objects.select_related(
        "trainer", "specialization",
    ).prefetch_related("participants")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["can_manage_session"] = (
            self.request.user.is_superuser
            or self.object.trainer.user_id == self.request.user.pk
        )
        if context["can_manage_session"]:
            attendance = {
                record.user_id: record
                for record in self.object.attendance_records.select_related("user")
            }
            context["attendance_rows"] = [
                {"user": user, "attendance": attendance.get(user.pk)}
                for user in self.object.participants.order_by("username")
            ]
        return context


class ReceptionAccessMixin(LoginRequiredMixin, UserPassesTestMixin):
    """The tablet account, a trainer, or an administrator may use reception check-in."""

    def test_func(self) -> bool:
        return can_use_reception(self.request.user)


class ReceptionSessionList(ReceptionAccessMixin, generic.ListView):
    model = TrainingSession
    template_name = "reception_session_list.html"
    context_object_name = "sessions"

    def get_queryset(self) -> QuerySet[TrainingSession]:
        return TrainingSession.objects.filter(
            starts_at__date=timezone.localdate(), status=TrainingSession.Status.SCHEDULED,
        ).select_related("trainer", "specialization").order_by("starts_at")


class ReceptionCheckIn(ReceptionAccessMixin, generic.DetailView):
    model = TrainingSession
    template_name = "reception_check_in.html"

    def get_queryset(self) -> QuerySet[TrainingSession]:
        return TrainingSession.objects.filter(
            starts_at__date=timezone.localdate(), status=TrainingSession.Status.SCHEDULED,
        ).select_related("trainer", "specialization").prefetch_related("participants")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        records = {
            record.user_id: record
            for record in self.object.attendance_records.select_related("user")
        }
        context["check_in_rows"] = [
            {"user": user, "attendance": records.get(user.pk)}
            for user in self.object.participants.order_by("first_name", "last_name", "username")
        ]
        return context


@login_required
@require_POST
@transaction.atomic
def kiosk_check_in(request: HttpRequest, session_pk: int, user_pk: int) -> HttpResponse:
    if not can_use_reception(request.user):
        raise PermissionDenied
    session = get_object_or_404(
        TrainingSession.objects.select_for_update(), pk=session_pk, starts_at__date=timezone.localdate(),
        status=TrainingSession.Status.SCHEDULED,
    )
    user = get_object_or_404(session.participants, pk=user_pk)
    attendance, _ = SessionAttendance.objects.get_or_create(session=session, user=user)
    if attendance.status != SessionAttendance.Status.ATTENDED:
        attendance.mark_attended()
        messages.success(request, f"{user.get_full_name() or user.username} checked in.")
    else:
        messages.info(request, f"{user.get_full_name() or user.username} is already checked in.")
    return redirect("training:reception-check-in", pk=session.pk)


@login_required
@require_POST
@transaction.atomic
def kiosk_undo_check_in(request: HttpRequest, session_pk: int, user_pk: int) -> HttpResponse:
    if not can_use_reception(request.user):
        raise PermissionDenied
    session = get_object_or_404(
        TrainingSession.objects.select_for_update(), pk=session_pk, starts_at__date=timezone.localdate(),
        status=TrainingSession.Status.SCHEDULED,
    )
    user = get_object_or_404(session.participants, pk=user_pk)
    attendance = SessionAttendance.objects.filter(session=session, user=user).first()
    if attendance is not None and attendance.status == SessionAttendance.Status.ATTENDED:
        attendance.status = SessionAttendance.Status.BOOKED
        attendance.checked_in_at = None
        attendance.save(update_fields=["status", "checked_in_at", "updated_at"])
        messages.info(request, f"Check-in for {user.get_full_name() or user.username} was undone.")
    return redirect("training:reception-check-in", pk=session.pk)


@login_required
@require_POST
@transaction.atomic
def update_attendance(request: HttpRequest, session_pk: int, user_pk: int) -> HttpResponse:
    session = get_object_or_404(TrainingSession.objects.select_for_update(), pk=session_pk)
    if not (request.user.is_superuser or session.trainer.user_id == request.user.pk):
        raise PermissionDenied
    if session.status == TrainingSession.Status.COMPLETED:
        messages.error(request, "Attendance is fixed after confirmation. Ask an administrator to record an earnings correction.")
        return redirect(session)
    user = get_object_or_404(session.participants, pk=user_pk)
    status = request.POST.get("status")
    valid_statuses = {choice for choice, _ in SessionAttendance.Status.choices}
    if status not in valid_statuses - {SessionAttendance.Status.CANCELLED}:
        messages.error(request, "Choose a valid attendance status.")
        return redirect(session)
    attendance, _ = SessionAttendance.objects.get_or_create(session=session, user=user)
    attendance.status = status
    attendance.checked_in_at = timezone.now() if status == SessionAttendance.Status.ATTENDED else None
    attendance.save(update_fields=["status", "checked_in_at", "updated_at"])
    messages.success(request, "Attendance was updated.")
    return redirect(session)


class EditorMixin(LoginRequiredMixin):
    template_name = "form.html"


class AdminRequiredMixin(EditorMixin, UserPassesTestMixin):
    def test_func(self) -> bool:
        return self.request.user.is_superuser


class ClientList(AdminRequiredMixin, generic.ListView):
    model = User
    template_name = "client_list.html"
    context_object_name = "clients"
    paginate_by = 10

    def get_queryset(self) -> QuerySet[User]:
        return User.objects.filter(role=User.Role.CLIENT).order_by("username", "pk")


@login_required
def adjust_client_balance(request: HttpRequest, pk: int) -> HttpResponse:
    if not request.user.is_superuser:
        raise PermissionDenied
    client = get_object_or_404(User, pk=pk, role=User.Role.CLIENT)
    form = BalanceAdjustmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        amount = form.cleaned_data["amount"]
        with transaction.atomic():
            client = User.objects.select_for_update().get(pk=client.pk)
            if client.balance + amount < 0:
                form.add_error("amount", "This would make the client balance negative.")
            else:
                client.balance += amount
                client.save(update_fields=["balance"])
                BalanceTransaction.objects.create(
                    user=client,
                    amount=amount,
                    kind=BalanceTransaction.Kind.ADMIN,
                    description=f"Admin adjustment: {form.cleaned_data['description']}",
                )
                messages.success(request, "Client balance was adjusted.")
                return redirect("training:client-list")
    return render(
        request,
        "form.html",
        {"form": form, "title": f"Adjust balance: {client.username}"},
    )

    def get_queryset(self) -> QuerySet[User]:
        query = self.request.GET.get("q", "").strip()
        return User.objects.filter(
            role=User.Role.CLIENT, is_superuser=False, username__icontains=query,
        ).prefetch_related("training_sessions").order_by("username")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["query"] = self.request.GET.get("q", "")
        return context


class MembershipDashboard(LoginRequiredMixin, generic.ListView):
    model = Membership
    template_name = "membership_dashboard.html"
    context_object_name = "memberships"

    def get_queryset(self) -> QuerySet[Membership]:
        return Membership.objects.filter(user=self.request.user)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["plans"] = MembershipPlan.objects.filter(is_active=True)
        context["transactions"] = BalanceTransaction.objects.filter(
            user=self.request.user,
        )[:5]
        return context


class VisionDashboard(LoginRequiredMixin, generic.ListView):
    model = VisionAnalysis
    template_name = "vision_dashboard.html"
    context_object_name = "analyses"

    def get_queryset(self) -> QuerySet[VisionAnalysis]:
        return VisionAnalysis.objects.filter(user=self.request.user).select_related(
            "device",
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        devices = VisionDevice.objects.filter(
            owner=self.request.user, is_active=True,
        )
        context["devices"] = devices
        context["primary_device"] = devices.first()
        context["progress_analyses"] = list(self.get_queryset().exclude(
            pose_score__isnull=True,
        ).order_by("-captured_at")[:20])[::-1]
        return context


def _device_from_request(request: HttpRequest) -> VisionDevice | None:
    """Authenticate a Pi with a bearer token; never store the raw token."""
    authorization = request.headers.get("Authorization", "")
    scheme, _, raw_token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not raw_token:
        return None
    token_hash = sha256(raw_token.encode()).hexdigest()
    device = VisionDevice.objects.filter(
        token_hash=token_hash, is_active=True,
    ).select_related("owner").first()
    if device is None or not device.check_token(raw_token):
        return None
    return device


@csrf_exempt
@require_POST
def ingest_vision_analysis(request: HttpRequest) -> JsonResponse:
    """Receive a compact pose result from an authorised Raspberry Pi device."""
    if len(request.body) > 65_536:
        return JsonResponse({"detail": "Payload is too large."}, status=413)
    device = _device_from_request(request)
    if device is None:
        return JsonResponse({"detail": "Invalid device token."}, status=401)
    if device.owner is None:
        return JsonResponse({"detail": "Assign a device owner first."}, status=409)
    try:
        payload = json.loads(request.body)
    except (TypeError, json.JSONDecodeError):
        return JsonResponse({"detail": "Expected a JSON body."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"detail": "Expected a JSON object."}, status=400)

    activity = payload.get("activity")
    valid_activities = {choice for choice, _ in VisionAnalysis.Activity.choices}
    if activity not in valid_activities:
        return JsonResponse({"detail": "Unsupported activity."}, status=400)
    feedback = payload.get("feedback", "")
    landmarks = payload.get("landmarks", {})
    score = payload.get("pose_score")
    if not isinstance(feedback, str) or len(feedback) > 500:
        return JsonResponse({"detail": "Invalid feedback."}, status=400)
    if not isinstance(landmarks, dict):
        return JsonResponse({"detail": "Landmarks must be an object."}, status=400)
    try:
        pose_score = None if score is None else Decimal(str(score)).quantize(
            Decimal("0.01"),
        )
    except (InvalidOperation, ValueError):
        return JsonResponse({"detail": "Invalid pose score."}, status=400)
    if pose_score is not None and not Decimal("0") <= pose_score <= Decimal("100"):
        return JsonResponse({"detail": "Pose score must be 0–100."}, status=400)

    analysis = VisionAnalysis.objects.create(
        user=device.owner, device=device, activity=activity,
        pose_score=pose_score, feedback=feedback, landmarks=landmarks,
    )
    device.last_seen_at = timezone.now()
    device.save(update_fields=["last_seen_at"])
    return JsonResponse({"id": analysis.pk, "status": "created"}, status=201)


@csrf_exempt
@require_POST
def upload_vision_snapshot(request: HttpRequest, pk: int) -> JsonResponse:
    """Attach one optional, user-approved progress frame to an analysis."""
    device = _device_from_request(request)
    if device is None:
        return JsonResponse({"detail": "Invalid device token."}, status=401)
    analysis = get_object_or_404(VisionAnalysis, pk=pk, device=device)
    image = request.body
    if not image:
        return JsonResponse({"detail": "Expected a JPEG snapshot."}, status=400)
    if len(image) > 2 * 1024 * 1024:
        return JsonResponse({"detail": "Snapshot must be 2 MB or smaller."}, status=413)
    if request.content_type.split(";", 1)[0].lower() != "image/jpeg":
        return JsonResponse({"detail": "Snapshot must be JPEG."}, status=400)
    if not _is_jpeg(image):
        return JsonResponse({"detail": "Snapshot is not a valid JPEG."}, status=400)
    if analysis.snapshot:
        return JsonResponse({"detail": "A snapshot is already attached."}, status=409)
    analysis.snapshot.save(
        f"analysis-{analysis.pk}.jpg", ContentFile(image), save=True,
    )
    return JsonResponse({"status": "stored"}, status=201)


def _live_frame_cache_key(device_id: int) -> str:
    return f"trainmate:vision:live-frame:{device_id}"


def _is_jpeg(image: bytes) -> bool:
    """Reject arbitrary uploads while keeping Pi-side validation inexpensive."""
    return len(image) >= 4 and image.startswith(b"\xff\xd8") and image.endswith(b"\xff\xd9")


@csrf_exempt
@require_POST
def ingest_vision_live_frame(request: HttpRequest) -> HttpResponse:
    """Store a short-lived JPEG frame for an authenticated owner's dashboard."""
    device = _device_from_request(request)
    if device is None:
        return JsonResponse({"detail": "Invalid device token."}, status=401)
    frame = request.body
    if not frame:
        return JsonResponse({"detail": "Expected a JPEG frame."}, status=400)
    if len(frame) > 1 * 1024 * 1024:
        return JsonResponse({"detail": "Live frame must be 1 MB or smaller."}, status=413)
    if request.content_type.split(";", 1)[0].lower() != "image/jpeg":
        return JsonResponse({"detail": "Live frame must be JPEG."}, status=400)
    if not _is_jpeg(frame):
        return JsonResponse({"detail": "Live frame is not a valid JPEG."}, status=400)
    cache.set(_live_frame_cache_key(device.pk), frame, timeout=15)
    device.last_seen_at = timezone.now()
    device.save(update_fields=["last_seen_at"])
    return HttpResponse(status=204)


@login_required
@require_GET
def vision_live_frame(request: HttpRequest, pk: int) -> HttpResponse:
    """Expose the latest Pi frame only to the device owner."""
    device = get_object_or_404(
        VisionDevice, pk=pk, owner=request.user, is_active=True,
    )
    frame = cache.get(_live_frame_cache_key(device.pk))
    if frame is None:
        return HttpResponse(status=204)
    response = HttpResponse(frame, content_type="image/jpeg")
    response["Cache-Control"] = "no-store, max-age=0"
    return response


@login_required
@require_GET
def vision_snapshot(request: HttpRequest, pk: int) -> FileResponse:
    """Serve a saved progress frame only to the analysis owner."""
    analysis = get_object_or_404(VisionAnalysis, pk=pk, user=request.user)
    if not analysis.snapshot:
        raise Http404("No snapshot is available for this analysis.")
    response = FileResponse(analysis.snapshot.open("rb"), content_type="image/jpeg")
    response["Cache-Control"] = "private, no-store, max-age=0"
    return response


class TrainerEarnings(LoginRequiredMixin, UserPassesTestMixin, generic.ListView):
    model = SalaryAccrual
    template_name = "trainer_earnings.html"
    context_object_name = "accruals"

    def test_func(self) -> bool:
        return is_trainer(self.request.user)

    def get_queryset(self) -> QuerySet[SalaryAccrual]:
        return SalaryAccrual.objects.filter(
            trainer=self.request.user.trainer_profile,
        ).select_related("session")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        accruals = self.get_queryset()
        context["hold_total"] = accruals.filter(
            status=SalaryAccrual.Status.ACCRUED,
        ).aggregate(total=Sum("amount"))["total"] or 0
        context["confirmed_total"] = accruals.filter(
            status=SalaryAccrual.Status.READY,
        ).aggregate(total=Sum("amount"))["total"] or 0
        context["paid_total"] = accruals.filter(
            status=SalaryAccrual.Status.PAID,
        ).aggregate(total=Sum("amount"))["total"] or 0
        adjustments = SalaryAdjustment.objects.filter(accrual__in=accruals).select_related("accrual__session", "created_by", "payout")
        context["adjustments"] = adjustments
        context["confirmed_total"] += adjustments.filter(payout__isnull=True).aggregate(total=Sum("amount"))["total"] or 0
        context["paid_total"] += adjustments.filter(payout__isnull=False).aggregate(total=Sum("amount"))["total"] or 0
        context["outstanding_total"] = context["confirmed_total"]
        context["total_earned"] = context["outstanding_total"] + context["paid_total"]
        context["payouts"] = TrainerPayout.objects.filter(trainer=self.request.user.trainer_profile).select_related("recorded_by", "trainer")
        return context


class PayoutList(AdminRequiredMixin, generic.ListView):
    model = SalaryAccrual
    template_name = "payout_list.html"
    context_object_name = "accruals"

    def get_queryset(self) -> QuerySet[SalaryAccrual]:
        return SalaryAccrual.objects.select_related("trainer", "session__completed_by")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        accruals = self.get_queryset()
        totals = accruals.aggregate(
            accrued_total=Sum("amount", filter=Q(status=SalaryAccrual.Status.ACCRUED)),
            ready_total=Sum("amount", filter=Q(status=SalaryAccrual.Status.READY)),
            paid_total=Sum("amount", filter=Q(status=SalaryAccrual.Status.PAID)),
        )
        context.update({key: value or 0 for key, value in totals.items()})
        adjustments = SalaryAdjustment.objects.select_related("accrual__trainer", "accrual__session", "created_by", "payout")
        context["adjustments"] = adjustments
        adjustment_totals = {
            row["accrual__trainer_id"]: row for row in adjustments.values("accrual__trainer_id").annotate(
                due=Sum("amount", filter=Q(payout__isnull=True)),
                paid=Sum("amount", filter=Q(payout__isnull=False)),
            )
        }
        context["ready_total"] += sum(row["due"] or 0 for row in adjustment_totals.values())
        context["paid_total"] += sum(row["paid"] or 0 for row in adjustment_totals.values())
        context["outstanding_total"] = context["ready_total"]

        trainer_totals = Trainer.objects.filter(accruals__isnull=False).annotate(
            session_count=Count("accruals"),
            accrued_total=Sum(
                "accruals__amount",
                filter=Q(accruals__status=SalaryAccrual.Status.ACCRUED),
            ),
            ready_total=Sum(
                "accruals__amount",
                filter=Q(accruals__status=SalaryAccrual.Status.READY),
            ),
            paid_total=Sum(
                "accruals__amount",
                filter=Q(accruals__status=SalaryAccrual.Status.PAID),
            ),
        ).order_by("name")
        for trainer in trainer_totals:
            trainer.accrued_total = trainer.accrued_total or 0
            trainer.ready_total = trainer.ready_total or 0
            trainer.paid_total = trainer.paid_total or 0
            trainer_adjustments = adjustment_totals.get(trainer.pk, {})
            trainer.ready_total += trainer_adjustments.get("due") or 0
            trainer.paid_total += trainer_adjustments.get("paid") or 0
            trainer.outstanding_total = trainer.ready_total
            trainer.total_earned = trainer.outstanding_total + trainer.paid_total
        context["trainer_totals"] = trainer_totals
        context["payouts"] = TrainerPayout.objects.select_related("trainer", "recorded_by").prefetch_related("accruals")
        return context


class TrainerPayoutCreate(AdminRequiredMixin, generic.FormView):
    template_name = "trainer_payout_confirm.html"
    form_class = TrainerPayoutForm
    success_url = reverse_lazy("training:payout-list")

    def get_trainer(self) -> Trainer:
        return get_object_or_404(Trainer, pk=self.kwargs["trainer_pk"])

    def get_ready_accruals(self) -> QuerySet[SalaryAccrual]:
        records = SalaryAccrual.objects.filter(
            trainer=self.get_trainer(), status=SalaryAccrual.Status.READY,
            payout__isnull=True, amount__gt=0,
        ).select_related("session").order_by("session__starts_at", "pk")
        values = self.request.POST if self.request.method == "POST" else self.request.GET
        try:
            start = date.fromisoformat(values["starts_on"]) if values.get("starts_on") else None
            end = date.fromisoformat(values["ends_on"]) if values.get("ends_on") else None
        except ValueError:
            return records.none()
        if start and end and start > end:
            return records.none()
        if start:
            records = records.filter(session__starts_at__date__gte=start)
        if end:
            records = records.filter(session__starts_at__date__lte=end)
        return records

    def get_adjustments(self):
        records = SalaryAdjustment.objects.filter(accrual__trainer=self.get_trainer(), payout__isnull=True)
        values = self.request.POST if self.request.method == "POST" else self.request.GET
        try:
            start = date.fromisoformat(values["starts_on"]) if values.get("starts_on") else None
            end = date.fromisoformat(values["ends_on"]) if values.get("ends_on") else None
        except ValueError:
            return records.none()
        if start and end and start > end:
            return records.none()
        if start:
            records = records.filter(accrual__session__starts_at__date__gte=start)
        if end:
            records = records.filter(accrual__session__starts_at__date__lte=end)
        return records.select_related("accrual__session").order_by("pk")

    @staticmethod
    def selection_for(records, adjustments):
        return [["earning", record.pk, str(record.amount)] for record in records] + [["correction", record.pk, str(record.amount)] for record in adjustments]

    def get_initial(self):
        return {"starts_on": self.request.GET.get("starts_on", ""), "ends_on": self.request.GET.get("ends_on", ""), "method": "transfer"}

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        ready_accruals = list(self.get_ready_accruals())
        adjustments = list(self.get_adjustments())
        context["form"].initial["selection"] = signing.dumps(
            self.selection_for(ready_accruals, adjustments), salt="payout-selection",
        )
        context.update({
            "trainer": self.get_trainer(),
            "ready_accruals": ready_accruals,
            "adjustments": adjustments,
            "payout_amount": sum((record.amount for record in ready_accruals + adjustments), Decimal("0.00")),
        })
        return context

    def form_valid(self, form: TrainerPayoutForm) -> HttpResponse:
        trainer = self.get_trainer()
        with transaction.atomic():
            Trainer.objects.select_for_update().get(pk=trainer.pk)
            ready_accruals = list(self.get_ready_accruals().select_for_update())
            adjustments = list(self.get_adjustments().select_for_update())
            try:
                selection = signing.loads(form.cleaned_data["selection"], salt="payout-selection", max_age=3600)
            except signing.BadSignature:
                selection = None
            if selection != self.selection_for(ready_accruals, adjustments):
                form.add_error(None, "The selection changed or expired. Reload this page to review the payment again.")
                return self.form_invalid(form)
            if not ready_accruals and not adjustments:
                form.add_error(None, "There are no ready salary records left to pay.")
                return self.form_invalid(form)
            amount = sum((record.amount for record in ready_accruals + adjustments), Decimal("0.00"))
            if amount <= 0:
                form.add_error(None, "The payment must be positive. Negative corrections offset future earnings.")
                return self.form_invalid(form)
            payout = TrainerPayout.objects.create(
                trainer=trainer, amount=amount, note=form.cleaned_data["note"],
                method=form.cleaned_data["method"], recorded_by=self.request.user,
            )
            for accrual in ready_accruals:
                accrual.status = SalaryAccrual.Status.PAID
                accrual.payout = payout
                accrual.save(update_fields=["status", "payout"])
            SalaryAdjustment.objects.filter(pk__in=[record.pk for record in adjustments]).update(payout=payout)
        messages.success(
            self.request,
            f"Recorded a payment of {amount} PLN to {trainer.name} for {len(ready_accruals)} sessions.",
        )
        return super().form_valid(form)


class TrainerPayoutDetail(LoginRequiredMixin, generic.DetailView):
    model = TrainerPayout
    template_name = "trainer_payout_detail.html"

    def get_queryset(self):
        records = TrainerPayout.objects.select_related("trainer", "recorded_by").prefetch_related("accruals__session__completed_by", "adjustments__accrual__session", "adjustments__created_by")
        if not self.request.user.is_superuser:
            records = records.filter(trainer__user=self.request.user)
        return records


class SalaryAdjustmentCreate(AdminRequiredMixin, generic.FormView):
    template_name = "form.html"
    form_class = SalaryAdjustmentForm
    success_url = reverse_lazy("training:payout-list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        record = get_object_or_404(SalaryAccrual, pk=self.kwargs["pk"], status__in=["ready", "paid"])
        context["title"] = f"Correction: {record.trainer} · {record.session.title}"
        return context

    def form_valid(self, form):
        with transaction.atomic():
            record = get_object_or_404(SalaryAccrual, pk=self.kwargs["pk"], status__in=["ready", "paid"])
            Trainer.objects.select_for_update().get(pk=record.trainer_id)
            SalaryAdjustment.objects.create(accrual=record, created_by=self.request.user, **form.cleaned_data)
        messages.success(self.request, "Correction recorded. It will be included in a future payment for this session period.")
        return super().form_valid(form)


@login_required
@require_POST
def add_balance(request: HttpRequest) -> HttpResponse:
    try:
        amount = Decimal(request.POST.get("amount", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        amount = Decimal("0.00")
    if not Decimal("0.00") < amount <= Decimal("10000.00"):
        messages.error(request, "Enter a top-up amount between 0.01 and 10,000.00 PLN.")
        return redirect("training:membership")
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        user.balance += amount
        user.save(update_fields=["balance"])
        BalanceTransaction.objects.create(
            user=user, amount=amount, kind=BalanceTransaction.Kind.TOP_UP,
            description="Demo balance top-up",
        )
    messages.success(request, f"{amount} PLN was added to your demo balance.")
    return redirect("training:membership")


@login_required
@require_POST
def purchase_membership(request: HttpRequest, pk: int) -> HttpResponse:
    plan = get_object_or_404(MembershipPlan, pk=pk, is_active=True)
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        if user.balance < plan.price:
            messages.error(request, "Your balance is too low for this membership.")
            return redirect("training:membership")
        user.balance -= plan.price
        user.save(update_fields=["balance"])
        Membership.objects.filter(user=user, is_active=True).update(is_active=False)
        starts_on = timezone.localdate()
        Membership.objects.create(
            user=user, plan=plan, title=plan.title,
            sessions_total=plan.sessions_count,
            sessions_remaining=plan.sessions_count,
            price=plan.price, starts_on=starts_on,
            expires_on=starts_on + timedelta(days=plan.duration_days),
        )
        BalanceTransaction.objects.create(
            user=user, amount=-plan.price,
            kind=BalanceTransaction.Kind.MEMBERSHIP,
            description=f"Purchased {plan.title}",
        )
    messages.success(request, f"{plan.title} is now active.")
    return redirect("training:membership")


class TrainerSessionMixin(EditorMixin, UserPassesTestMixin):
    """Lets a trainer manage only sessions assigned to their profile."""

    trainer = None

    def test_func(self) -> bool:
        return self.request.user.is_superuser or is_trainer(self.request.user)

    def dispatch(
        self, request: HttpRequest, *args: Any, **kwargs: Any,
    ) -> HttpResponse:
        if not request.user.is_superuser:
            if not is_trainer(request.user):
                raise PermissionDenied
            self.trainer = request.user.trainer_profile
        return super().dispatch(request, *args, **kwargs)

    def get_form_class(self) -> type[SessionForm]:
        return SessionForm if self.request.user.is_superuser else TrainerSessionForm

    def get_form(
        self, form_class: type[SessionForm] | None = None,
    ) -> SessionForm:
        form = super().get_form(form_class)
        if self.trainer:
            form.instance.trainer = self.trainer
            form.fields["specialization"].queryset = self.trainer.specializations.all()
        return form

    def get_queryset(self) -> QuerySet[TrainingSession]:
        queryset = super().get_queryset()
        if self.request.user.is_superuser:
            return queryset
        return queryset.filter(trainer=self.trainer)


class SessionCreate(TrainerSessionMixin, generic.CreateView):
    model = TrainingSession

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.update({"form_title": "Create one-time session", "back_url": reverse_lazy("training:session-list")})
        return context


class SessionUpdate(TrainerSessionMixin, generic.UpdateView):
    model = TrainingSession

    def get_queryset(self) -> QuerySet[TrainingSession]:
        return super().get_queryset().filter(status=TrainingSession.Status.SCHEDULED)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.update({"form_title": "Edit session", "back_url": reverse_lazy("training:session-list")})
        return context


class RecurringScheduleMixin(EditorMixin, UserPassesTestMixin):
    trainer = None

    def test_func(self) -> bool:
        return self.request.user.is_superuser or is_trainer(self.request.user)

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.user.is_superuser:
            if not is_trainer(request.user):
                raise PermissionDenied
            self.trainer = request.user.trainer_profile
        return super().dispatch(request, *args, **kwargs)

    def get_form_class(self) -> type[RecurringScheduleForm]:
        return RecurringScheduleForm if self.request.user.is_superuser else TrainerRecurringScheduleForm

    def get_form(self, form_class: type[RecurringScheduleForm] | None = None) -> RecurringScheduleForm:
        form = super().get_form(form_class)
        if self.trainer:
            form.instance.trainer = self.trainer
            form.fields["specialization"].queryset = self.trainer.specializations.all()
        return form

    def get_queryset(self) -> QuerySet[RecurringSchedule]:
        queryset = super().get_queryset()
        return queryset if self.request.user.is_superuser else queryset.filter(trainer=self.trainer)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        schedule = getattr(self, "object", None)
        context.update({
            "form_title": "Edit regular schedule" if schedule else "Create regular schedule",
            "back_url": reverse_lazy("training:recurring-schedule-list"),
        })
        return context


class RecurringScheduleList(RecurringScheduleMixin, generic.ListView):
    model = RecurringSchedule
    template_name = "recurring_schedule_list.html"
    context_object_name = "schedules"


class RecurringScheduleCreate(RecurringScheduleMixin, generic.CreateView):
    model = RecurringSchedule
    success_url = reverse_lazy("training:recurring-schedule-list")

    def form_valid(self, form: RecurringScheduleForm) -> HttpResponse:
        response = super().form_valid(form)
        count = self.object.sync_sessions(refresh=True)
        messages.success(self.request, f"Regular schedule saved. {count} sessions were created for the next three weeks.")
        return response


class RecurringScheduleUpdate(RecurringScheduleMixin, generic.UpdateView):
    model = RecurringSchedule
    success_url = reverse_lazy("training:recurring-schedule-list")

    def form_valid(self, form: RecurringScheduleForm) -> HttpResponse:
        response = super().form_valid(form)
        count = self.object.sync_sessions(refresh=True)
        messages.success(self.request, f"Regular schedule updated. {count} unbooked future sessions were refreshed.")
        return response


class SafeDelete(LoginRequiredMixin, generic.DeleteView):
    def get_template_names(self) -> list[str]:
        """Use confirmation UI despite the editor mixin's form template."""
        return ["confirm_delete.html"]

    def form_valid(self, form: Any) -> HttpResponse:
        object_name = str(self.get_object())
        try:
            response = super().form_valid(form)
            messages.success(self.request, f"{object_name} was deleted.")
            return response
        except ProtectedError:
            messages.error(self.request, "Remove linked sessions first.")
            return redirect(self.success_url)


class SessionDelete(TrainerSessionMixin, SafeDelete):
    model = TrainingSession
    success_url = reverse_lazy("training:session-list")

    def get_form_class(self) -> type[django_forms.Form]:
        """DeleteView needs its empty confirmation form, not SessionForm."""
        return django_forms.Form

    def form_valid(self, form: Any) -> HttpResponse:
        session = self.get_object()
        if SalaryAccrual.objects.filter(session=session).exists():
            messages.error(
                self.request,
                "This session cannot be deleted because it has a salary record.",
            )
            return redirect("training:session-list")
        return super().form_valid(form)


class RecurringScheduleDelete(RecurringScheduleMixin, SafeDelete):
    model = RecurringSchedule
    success_url = reverse_lazy("training:recurring-schedule-list")

    def get_form_class(self) -> type[django_forms.Form]:
        return django_forms.Form


class TrainerCreate(AdminRequiredMixin, generic.CreateView):
    model = Trainer
    form_class = TrainerForm
    success_url = reverse_lazy("training:trainer-list")


class TrainerUpdate(AdminRequiredMixin, generic.UpdateView):
    model = Trainer
    form_class = TrainerForm
    success_url = reverse_lazy("training:trainer-list")


class TrainerDelete(AdminRequiredMixin, SafeDelete):
    model = Trainer
    success_url = reverse_lazy("training:trainer-list")


class SpecializationCreate(AdminRequiredMixin, generic.CreateView):
    model = Specialization
    fields = ("name", "description")
    success_url = reverse_lazy("training:specialization-list")


class SpecializationUpdate(AdminRequiredMixin, generic.UpdateView):
    model = Specialization
    fields = ("name", "description")
    success_url = reverse_lazy("training:specialization-list")


class SpecializationDelete(AdminRequiredMixin, SafeDelete):
    model = Specialization
    success_url = reverse_lazy("training:specialization-list")


class SignUp(generic.CreateView):
    form_class = SignUpForm
    template_name = "form.html"

    def form_valid(self, form: SignUpForm) -> HttpResponse:
        self.object = form.save(commit=False)
        self.object.is_active = False
        self.object.save()
        uid = urlsafe_base64_encode(force_bytes(self.object.pk))
        token = default_token_generator.make_token(self.object)
        activation_url = self.request.build_absolute_uri(
            reverse_lazy("training:activate-account", args=[uid, token]),
        )
        message = render_to_string(
            "registration/activation_email.txt",
            {"user": self.object, "activation_url": activation_url},
        )
        send_mail(
            "Activate your TrainMate account",
            message,
            None,
            [self.object.email],
        )
        return redirect("training:activation-sent")


def activate_account(request: HttpRequest, uidb64: str, token: str) -> HttpResponse:
    try:
        user_id = urlsafe_base64_decode(uidb64).decode()
        user = User.objects.get(pk=user_id)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None
    if user is not None and default_token_generator.check_token(user, token):
        if not user.is_active:
            user.is_active = True
            user.save(update_fields=["is_active"])
        messages.success(request, "Your email is confirmed. You can now log in.")
        return redirect("login")
    return render(request, "registration/activation_invalid.html", status=400)


def activation_sent(request: HttpRequest) -> HttpResponse:
    return render(request, "registration/activation_sent.html")


class Profile(EditorMixin, generic.UpdateView):
    form_class = ProfileForm
    success_url = reverse_lazy("training:profile")

    def get_object(self, queryset: QuerySet[User] | None = None) -> User:
        return self.request.user


@login_required
def complete_session(request: HttpRequest, pk: int) -> HttpResponse:
    with transaction.atomic():
        session = get_object_or_404(
            TrainingSession.objects.select_for_update(), pk=pk,
        )
        is_session_trainer = session.trainer.user_id == request.user.pk
        if not request.user.is_superuser and not is_session_trainer:
            raise PermissionDenied
        if request.method not in {"GET", "POST"}:
            return HttpResponse(status=405)
        attended = session.attendance_records.filter(status=SessionAttendance.Status.ATTENDED, user__in=session.participants.all()).count()
        snapshot = [session.pk, attended, str(session.trainer.rate_per_participant)]
        if request.method == "GET":
            return render(request, "session_confirm.html", {
                "session": session, "attended": attended,
                "amount": attended * session.trainer.rate_per_participant,
                "selection": signing.dumps(snapshot, salt="session-confirm"),
            })
        if session.status == TrainingSession.Status.COMPLETED:
            messages.info(request, "This session is already completed.")
        else:
            try:
                reviewed = signing.loads(request.POST.get("selection", ""), salt="session-confirm", max_age=3600)
            except signing.BadSignature:
                reviewed = None
            if reviewed != snapshot:
                messages.error(request, "Attendance or rate changed. Review the calculation again.")
                return redirect("training:session-complete", pk=pk)
            session.status = TrainingSession.Status.COMPLETED
            session.completed_by = request.user
            session.save()
            messages.success(request, "Session confirmed and salary accrual updated.")
    return redirect("training:session-list")


@login_required
@require_POST
def book(request: HttpRequest, pk: int) -> HttpResponse:
    with transaction.atomic():
        # Acquire a SQLite write lock before checking capacity.
        TrainingSession.objects.filter(pk=pk).update(capacity=F("capacity"))
        session = get_object_or_404(TrainingSession, pk=pk)
        if session.status == TrainingSession.Status.COMPLETED:
            messages.error(request, "This session has already been completed.")
        elif session.starts_at <= timezone.now():
            messages.error(request, "This session has already started.")
        elif session.participants.filter(pk=request.user.pk).exists():
            messages.info(request, "You already booked this session.")
        elif session.participants.count() >= session.capacity:
            messages.error(request, "This session is full.")
        else:
            membership = Membership.objects.select_for_update().filter(
                user=request.user,
                is_active=True,
                starts_on__lte=timezone.localdate(),
                expires_on__gte=timezone.localdate(),
                sessions_remaining__gt=0,
            ).order_by("expires_on", "pk").first()
            if membership is None:
                messages.error(request, "An active membership with sessions is required.")
            else:
                membership.sessions_remaining = F("sessions_remaining") - 1
                membership.save(update_fields=["sessions_remaining"])
                MembershipUsage.objects.create(
                    membership=membership, session=session, user=request.user,
                )
                session.participants.add(request.user)
                SessionAttendance.objects.get_or_create(
                    session=session, user=request.user,
                    defaults={"status": SessionAttendance.Status.BOOKED},
                )
                participant_count = session.participants.count()
                accrual, created = SalaryAccrual.objects.get_or_create(
                    session=session,
                    defaults={
                        "trainer": session.trainer,
                        "participant_count": participant_count,
                        "rate_per_participant": session.trainer.rate_per_participant,
                        "amount": participant_count * session.trainer.rate_per_participant,
                        "status": SalaryAccrual.Status.ACCRUED,
                    },
                )
                if not created and accrual.status == SalaryAccrual.Status.ACCRUED:
                    accrual.participant_count = participant_count
                    accrual.rate_per_participant = session.trainer.rate_per_participant
                    accrual.amount = participant_count * session.trainer.rate_per_participant
                    accrual.save(update_fields=[
                        "participant_count", "rate_per_participant", "amount",
                    ])
                messages.success(request, "Your place is reserved.")
    return redirect(session)


@login_required
@require_POST
def cancel(request: HttpRequest, pk: int) -> HttpResponse:
    with transaction.atomic():
        session = get_object_or_404(
            TrainingSession.objects.select_for_update(), pk=pk,
        )
        usage = MembershipUsage.objects.filter(
            session=session, user=request.user,
        ).select_related("membership").first()
        accrual = SalaryAccrual.objects.filter(session=session).first()
        if usage is None:
            messages.info(request, "You do not have a booking for this session.")
        elif session.status == TrainingSession.Status.COMPLETED or (
            accrual is not None and accrual.status != SalaryAccrual.Status.ACCRUED
        ):
            messages.error(
                request,
                "This booking cannot be cancelled after the session is confirmed.",
            )
        else:
            session.participants.remove(request.user)
            SessionAttendance.objects.filter(
                session=session, user=request.user,
            ).update(status=SessionAttendance.Status.CANCELLED, checked_in_at=None)
            if accrual:
                participant_count = session.participants.count()
                if participant_count == 0:
                    accrual.delete()
                else:
                    accrual.participant_count = participant_count
                    accrual.amount = participant_count * accrual.rate_per_participant
                    accrual.save(update_fields=["participant_count", "amount"])
            Membership.objects.filter(pk=usage.membership_id).update(
                sessions_remaining=F("sessions_remaining") + 1,
            )
            usage.delete()
            messages.success(request, "Your booking was cancelled.")
    return redirect(session)
