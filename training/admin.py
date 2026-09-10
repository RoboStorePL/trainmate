from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import (
    Membership, MembershipUsage, SalaryAccrual, Specialization, Trainer,
    TrainingSession, User,
)


@admin.register(User)
class TrainMateUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("TrainMate access", {"fields": ("role",)}),
    )
@admin.register(Trainer)
class TrainerAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "experience_years", "rate_per_participant")
    list_select_related = ("user",)


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "title", "sessions_remaining", "sessions_total", "expires_on", "is_active")
    list_filter = ("is_active",)
    search_fields = ("user__username", "title")


@admin.register(SalaryAccrual)
class SalaryAccrualAdmin(admin.ModelAdmin):
    list_display = ("trainer", "session", "amount", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("trainer__name", "session__title")
    fields = ("trainer", "session", "participant_count", "rate_per_participant", "amount", "status", "admin_note", "paid_at")


admin.site.register([MembershipUsage, Specialization, TrainingSession])
