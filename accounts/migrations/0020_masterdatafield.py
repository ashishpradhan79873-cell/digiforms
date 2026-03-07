from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0019_applicationhistory"),
    ]

    operations = [
        migrations.CreateModel(
            name="MasterDataField",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("step", models.CharField(choices=[("personal", "Personal"), ("address", "Address"), ("academic", "Academic"), ("college", "College"), ("bank", "Bank"), ("documents", "Documents")], default="personal", max_length=20)),
                ("label", models.CharField(max_length=140)),
                ("field_kind", models.CharField(choices=[("text", "Text Field"), ("document", "Document Upload")], default="text", max_length=20)),
                ("display_order", models.PositiveIntegerField(default=0)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["step", "display_order", "label", "id"],
            },
        ),
    ]

