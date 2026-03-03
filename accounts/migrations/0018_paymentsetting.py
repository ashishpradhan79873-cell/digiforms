from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0017_portalnews_image_and_external_link"),
    ]

    operations = [
        migrations.CreateModel(
            name="PaymentSetting",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("upi_id", models.CharField(blank=True, max_length=120)),
                ("payee_name", models.CharField(blank=True, max_length=120)),
                ("amount", models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ("qr_image", models.ImageField(blank=True, null=True, upload_to="payment_qr/")),
                ("note", models.CharField(blank=True, max_length=200)),
                ("is_active", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Payment Setting",
                "verbose_name_plural": "Payment Setting",
            },
        ),
    ]

