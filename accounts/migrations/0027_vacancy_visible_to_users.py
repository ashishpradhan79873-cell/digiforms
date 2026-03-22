from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0026_userprofile_previous_course_name_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="vacancy",
            name="visible_to_users",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
