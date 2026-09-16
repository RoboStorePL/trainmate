from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.http import HttpRequest

from .models import (
    BalanceTransaction, Membership, MembershipPlan, MembershipUsage,
    RecurringSchedule, SalaryAccrual, SessionAttendance, Specialization, Trainer,
    TrainerPayout, TrainingSession, User,
    VisionAnalysis, VisionDevice,
)


@admin.register(User)
class TrainMateUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("TrainMate access", {"fields": ("role", "balance")}),
    )
    readonly_fields = ("balance",)
@admin.register(Trainer)
class TrainerAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "experience_years", "rate_per_participant")
    list_select_related = ("user",)


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "title", "sessions_remaining", "sessions_total", "expires_on", "is_active")
    list_filter = ("is_active",)
    search_fields = ("user__username", "title")


@admin.register(MembershipPlan)
class MembershipPlanAdmin(admin.ModelAdmin):
    list_display = ("title", "sessions_count", "duration_days", "price", "is_active")
    list_filter = ("is_active",)
    search_fields = ("title",)


@admin.register(BalanceTransaction)
class BalanceTransactionAdmin(admin.ModelAdmin):
    list_display = ("user", "amount", "kind", "description", "created_at")
    list_filter = ("kind",)
    search_fields = ("user__username", "description")
    readonly_fields = ("user", "amount", "kind", "description", "created_at")

    def has_add_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: object | None = None,
    ) -> bool:
        return False


@admin.register(SalaryAccrual)
class SalaryAccrualAdmin(admin.ModelAdmin):
    list_display = (
        "trainer", "session", "amount", "status", "confirmed_at", "paid_at",
    )
    list_filter = ("status",)
    search_fields = ("trainer__name", "session__title")
    fields = (
        "trainer", "session", "participant_count", "rate_per_participant",
        "amount", "status", "payout", "admin_note", "confirmed_at", "paid_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: object | None = None,
    ) -> bool:
        return False


@admin.register(TrainerPayout)
class TrainerPayoutAdmin(admin.ModelAdmin):
    list_display = ("trainer", "amount", "paid_at", "note")
    search_fields = ("trainer__name", "note")
    list_select_related = ("trainer",)
    readonly_fields = ("trainer", "amount", "paid_at", "note", "method", "recorded_by")

    def has_change_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return False

    def has_add_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return False


@admin.register(RecurringSchedule)
class RecurringScheduleAdmin(admin.ModelAdmin):
    list_display = ("title", "trainer", "start_time", "starts_on", "ends_on", "is_active")
    list_filter = ("is_active", "specialization")
    search_fields = ("title", "trainer__name")


admin.site.register([MembershipUsage, Specialization, TrainingSession])


@admin.register(SessionAttendance)
class SessionAttendanceAdmin(admin.ModelAdmin):
    list_display = ("session", "user", "status", "checked_in_at", "updated_at")
    list_filter = ("status",)
    search_fields = ("session__title", "user__username")
    list_select_related = ("session", "user")


@admin.register(VisionDevice)
class VisionDeviceAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "is_active", "last_seen_at")
    list_filter = ("is_active",)
    search_fields = ("name", "owner__username")
    readonly_fields = ("token_hash", "last_seen_at", "created_at")


@admin.register(VisionAnalysis)
class VisionAnalysisAdmin(admin.ModelAdmin):
    list_display = ("user", "device", "activity", "pose_score", "has_snapshot", "captured_at")
    list_filter = ("activity", "device")
    search_fields = ("user__username", "device__name", "feedback")
    readonly_fields = ("device", "user", "captured_at", "created_at")

    @admin.display(boolean=True, description="Snapshot")
    def has_snapshot(self, obj: VisionAnalysis) -> bool:
        return bool(obj.snapshot)
