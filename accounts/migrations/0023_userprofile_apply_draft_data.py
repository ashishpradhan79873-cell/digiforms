from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0022_userprofile_apply_preview_controls"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="apply_draft_data",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
