from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Specialization, Trainer, TrainingSession, User


@admin.register(User)
class TrainMateUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("TrainMate access", {"fields": ("role",)}),
    )
@admin.register(Trainer)
class TrainerAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "experience_years")
    list_select_related = ("user",)


admin.site.register([Specialization, TrainingSession])
