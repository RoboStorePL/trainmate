from typing import Any

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import QuerySet
from django.db.models.deletion import ProtectedError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import generic

from ..models import Trainer, TrainingSession, User

def is_trainer(user: User) -> bool:
    return user.is_trainer


def can_use_reception(user: User) -> bool:
    return user.is_superuser or is_trainer(user) or user.role == User.Role.KIOSK


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


class EditorMixin(LoginRequiredMixin):
    template_name = "form.html"


class AdminRequiredMixin(EditorMixin, UserPassesTestMixin):
    def test_func(self) -> bool:
        return self.request.user.is_superuser


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




