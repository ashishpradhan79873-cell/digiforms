from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0025_userprofile_current_year_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="previous_course_name",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="previous_subjects",
            field=models.TextField(blank=True),
        ),
    ]
