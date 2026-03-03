from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0011_vacancy_required_documents_and_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="DocumentRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=140, unique=True)),
                ("min_kb", models.PositiveIntegerField(default=1)),
                ("max_kb", models.PositiveIntegerField(default=500)),
                ("kind", models.CharField(choices=[("any", "Any"), ("image", "Image"), ("pdf", "PDF")], default="any", max_length=10)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["name"]},
        ),
    ]
