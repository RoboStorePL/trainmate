from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import generic
from django.views.decorators.http import require_POST

from ..models import SessionAttendance, TrainingSession
from .common import can_use_reception


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



