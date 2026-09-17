from django.urls import reverse_lazy
from django.views import generic

from ..forms import TrainerForm
from ..models import Specialization, Trainer
from .common import AdminRequiredMixin, SafeDelete, SearchList

class TrainerList(SearchList):
    model = Trainer
    kind = "trainer"
    title = "Our trainers"


class SpecializationList(SearchList):
    model = Specialization
    kind = "specialization"
    title = "Explore disciplines"


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




