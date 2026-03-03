from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0013_documentrule_exact_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="PortalNews",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=220)),
                ("details", models.TextField(blank=True)),
                (
                    "news_type",
                    models.CharField(
                        choices=[("vacancy", "Vacancy"), ("result", "Result"), ("notice", "Notice")],
                        default="notice",
                        max_length=20,
                    ),
                ),
                (
                    "target_portal",
                    models.CharField(
                        choices=[("all", "All Users"), ("government", "Government Portal"), ("student", "Student Portal")],
                        default="all",
                        max_length=20,
                    ),
                ),
                ("event_date", models.DateField(blank=True, null=True)),
                ("display_order", models.PositiveIntegerField(default=0)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["display_order", "-event_date", "-updated_at", "-id"],
            },
        ),
    ]
