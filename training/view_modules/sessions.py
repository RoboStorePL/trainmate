from datetime import date, timedelta
from typing import Any

from django import forms as django_forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F, QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import generic
from django.views.decorators.http import require_POST

from ..forms import (
    RecurringScheduleForm,
    SessionForm,
    TrainerRecurringScheduleForm,
    TrainerSessionForm,
)
from ..models import (
    Membership,
    MembershipUsage,
    RecurringSchedule,
    SalaryAccrual,
    SessionAttendance,
    TrainingSession,
)
from .common import EditorMixin, SafeDelete, SearchList, is_trainer

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

