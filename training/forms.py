from decimal import Decimal
from typing import Any

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.db.models import Q

from .models import RecurringSchedule, Trainer, TrainingSession, User


class SignUpForm(UserCreationForm):
    email = forms.EmailField(required=True)

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "email", "first_name", "last_name", "fitness_level")

    def clean_email(self) -> str:
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email


class ProfileForm(forms.ModelForm):
    class Meta:
        model = get_user_model()
        fields = ("first_name", "last_name", "fitness_level")


class BalanceAdjustmentForm(forms.Form):
    amount = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        label="Adjustment amount (PLN)",
        help_text="Use a positive amount to add funds and a negative amount to deduct them.",
    )
    description = forms.CharField(max_length=255, label="Reason")

    def clean_amount(self) -> Decimal:
        amount = self.cleaned_data["amount"]
        if amount == 0:
            raise forms.ValidationError("The adjustment cannot be zero.")
        return amount


class SessionForm(forms.ModelForm):
    class Meta:
        model = TrainingSession
        exclude = ("participants",)
        widgets = {
            "starts_at": forms.DateTimeInput(
                format="%Y-%m-%dT%H:%M",
                attrs={"type": "datetime-local"},
            ),
        }


class TrainerSessionForm(SessionForm):
    class Meta(SessionForm.Meta):
        # Completion changes financial data and is restricted to an admin.
        fields = (
            "title", "description", "specialization", "starts_at",
            "duration_minutes", "capacity", "location",
        )


class RecurringScheduleForm(forms.ModelForm):
    weekdays = forms.MultipleChoiceField(
        choices=RecurringSchedule.DAYS_OF_WEEK,
        widget=forms.CheckboxSelectMultiple,
        help_text="TrainMate creates bookable sessions only for the next three weeks.",
    )

    class Meta:
        model = RecurringSchedule
        exclude = ()
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "starts_on": forms.DateInput(attrs={"type": "date"}),
            "ends_on": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.initial["weekdays"] = [str(day) for day in self.instance.weekdays]

    def clean_weekdays(self) -> list[int]:
        return [int(day) for day in self.cleaned_data["weekdays"]]


class TrainerRecurringScheduleForm(RecurringScheduleForm):
    class Meta(RecurringScheduleForm.Meta):
        fields = (
            "title", "description", "specialization", "weekdays", "start_time",
            "duration_minutes", "capacity", "location", "starts_on", "ends_on",
            "is_active",
        )


class TrainerForm(forms.ModelForm):
    user = forms.ModelChoiceField(queryset=User.objects.none(), required=False)

    class Meta:
        model = Trainer
        fields = (
            "user", "name", "bio", "experience_years", "rate_per_participant",
            "specializations",
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        selected_user_id = self.instance.user_id
        self.fields["user"].queryset = User.objects.filter(
            role=User.Role.TRAINER,
        ).filter(
            Q(trainer_profile__isnull=True) | Q(pk=selected_user_id),
        ).order_by("username")
