from typing import Any

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.db.models import Q

from .models import Trainer, TrainingSession, User


class SignUpForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "first_name", "last_name", "fitness_level")


class ProfileForm(forms.ModelForm):
    class Meta:
        model = get_user_model()
        fields = ("first_name", "last_name", "fitness_level")


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
        exclude = ("participants", "trainer")


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
