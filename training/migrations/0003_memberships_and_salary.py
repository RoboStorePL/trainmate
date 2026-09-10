from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("training", "0002_user_role_trainer_user"),
    ]

    operations = [
        migrations.AddField(
            model_name="trainer",
            name="rate_per_participant",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("10.00"),
                help_text="PLN paid for each participant in a completed session.",
                max_digits=8,
            ),
        ),
        migrations.AddField(
            model_name="trainingsession",
            name="completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="trainingsession",
            name="status",
            field=models.CharField(
                choices=[("scheduled", "Scheduled"), ("completed", "Completed")],
                default="scheduled",
                max_length=10,
            ),
        ),
        migrations.CreateModel(
            name="Membership",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(default="Standard membership", max_length=100)),
                ("sessions_total", models.PositiveIntegerField()),
                ("sessions_remaining", models.PositiveIntegerField()),
                ("price", models.DecimalField(decimal_places=2, max_digits=8)),
                ("starts_on", models.DateField(default=django.utils.timezone.localdate)),
                ("expires_on", models.DateField()),
                ("is_active", models.BooleanField(default=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="memberships", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-expires_on", "-pk"]},
        ),
        migrations.CreateModel(
            name="SalaryAccrual",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("participant_count", models.PositiveIntegerField()),
                ("rate_per_participant", models.DecimalField(decimal_places=2, max_digits=8)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10)),
                ("status", models.CharField(choices=[("accrued", "Accrued"), ("ready", "Ready for payout"), ("paid", "Paid")], default="accrued", max_length=10)),
                ("admin_note", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                ("session", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="salary_accrual", to="training.trainingsession")),
                ("trainer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="accruals", to="training.trainer")),
            ],
            options={"ordering": ["-created_at", "-pk"]},
        ),
        migrations.CreateModel(
            name="MembershipUsage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("membership", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="training.membership")),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="training.trainingsession")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddConstraint(
            model_name="membershipusage",
            constraint=models.UniqueConstraint(fields=("session", "user"), name="unique_session_membership_usage"),
        ),
    ]
