from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0010_userprofile_chat_enabled_chatmessage"),
    ]

    operations = [
        migrations.AddField(
            model_name="vacancy",
            name="required_documents",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="vacancy",
            name="required_profile_fields",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
