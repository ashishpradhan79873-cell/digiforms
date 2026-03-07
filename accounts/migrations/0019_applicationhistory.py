from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0018_paymentsetting"),
    ]

    operations = [
        migrations.CreateModel(
            name="ApplicationHistory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(choices=[("status_update", "Status Update"), ("cancel", "Cancelled"), ("remove", "Removed")], default="status_update", max_length=20)),
                ("profile_name", models.CharField(blank=True, max_length=150)),
                ("applicant_username", models.CharField(blank=True, max_length=150)),
                ("vacancy_title", models.CharField(blank=True, max_length=220)),
                ("actor_username", models.CharField(blank=True, max_length=150)),
                ("note", models.CharField(blank=True, max_length=300)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("application", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="history_entries", to="accounts.application")),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
    ]

