from django.urls import path
from . import views

app_name = "training"
urlpatterns = [
    path("", views.home, name="home"),
    path("signup/", views.SignUp.as_view(), name="signup"),
    path("profile/", views.Profile.as_view(), name="profile"),
    path("sessions/", views.SessionList.as_view(), name="session-list"),
    path("sessions/create/", views.SessionCreate.as_view(), name="session-create"),
    path("sessions/<int:pk>/update/", views.SessionUpdate.as_view(), name="session-update"),
    path("sessions/<int:pk>/delete/", views.SessionDelete.as_view(), name="session-delete"),
    path("trainers/", views.TrainerList.as_view(), name="trainer-list"),
    path("trainers/create/", views.TrainerCreate.as_view(), name="trainer-create"),
    path("trainers/<int:pk>/update/", views.TrainerUpdate.as_view(), name="trainer-update"),
    path("trainers/<int:pk>/delete/", views.TrainerDelete.as_view(), name="trainer-delete"),
    path("specializations/", views.SpecializationList.as_view(), name="specialization-list"),
    path("clients/", views.ClientList.as_view(), name="client-list"),
    path("specializations/create/", views.SpecializationCreate.as_view(), name="specialization-create"),
    path("specializations/<int:pk>/update/", views.SpecializationUpdate.as_view(), name="specialization-update"),
    path("specializations/<int:pk>/delete/", views.SpecializationDelete.as_view(), name="specialization-delete"),
    path("sessions/<int:pk>/", views.SessionDetail.as_view(), name="session-detail"),
    path("sessions/<int:pk>/book/", views.book, name="book"),
    path("sessions/<int:pk>/cancel/", views.cancel, name="cancel"),
]
