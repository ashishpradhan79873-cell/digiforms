from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0024_userprofile_subject_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="current_semester",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="current_year",
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
