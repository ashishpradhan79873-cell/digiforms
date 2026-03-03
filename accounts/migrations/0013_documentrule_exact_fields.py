from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0012_documentrule"),
    ]

    operations = [
        migrations.AddField(
            model_name="documentrule",
            name="exact_kb",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="documentrule",
            name="exact_width",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="documentrule",
            name="exact_height",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
