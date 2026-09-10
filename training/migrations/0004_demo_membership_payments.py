from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("training", "0003_memberships_and_salary"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="balance",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10),
        ),
        migrations.CreateModel(
            name="MembershipPlan",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=100, unique=True)),
                ("description", models.TextField(blank=True)),
                ("sessions_count", models.PositiveIntegerField()),
                ("duration_days", models.PositiveIntegerField(default=30)),
                ("price", models.DecimalField(decimal_places=2, max_digits=8)),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={"ordering": ["price", "pk"]},
        ),
        migrations.AddField(
            model_name="membership",
            name="plan",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="purchases", to="training.membershipplan"),
        ),
        migrations.CreateModel(
            name="BalanceTransaction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10)),
                ("kind", models.CharField(choices=[("top_up", "Demo top-up"), ("membership", "Membership purchase"), ("admin", "Admin adjustment")], max_length=12)),
                ("description", models.CharField(max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="balance_transactions", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at", "-pk"]},
        ),
    ]
