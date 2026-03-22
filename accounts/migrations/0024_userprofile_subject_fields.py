from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0023_userprofile_apply_draft_data"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="current_course_name",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="current_subjects",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="subject_extra_rows",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="tenth_subjects",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="twelfth_subjects",
            field=models.TextField(blank=True),
        ),
    ]
