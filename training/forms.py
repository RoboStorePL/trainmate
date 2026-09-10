from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm

from .models import TrainingSession


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
