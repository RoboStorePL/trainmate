# Generated manually for the TrainMate role-based access update.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("training", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="role",
            field=models.CharField(
                choices=[("client", "Client"), ("trainer", "Trainer")],
                default="client",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="trainer",
            name="user",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="trainer_profile",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
