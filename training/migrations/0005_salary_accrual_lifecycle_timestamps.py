# Generated manually for the salary payout lifecycle update.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("training", "0004_demo_membership_payments"),
    ]

    operations = [
        migrations.AddField(
            model_name="salaryaccrual",
            name="confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="salaryaccrual",
            name="updated_at",
            field=models.DateTimeField(auto_now=True),
        ),
    ]
