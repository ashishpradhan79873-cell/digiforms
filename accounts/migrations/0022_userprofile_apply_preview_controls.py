from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0021_userprofile_security_timers"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="apply_profile_view_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="apply_profile_view_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="apply_profile_unmask_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="apply_profile_unmask_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="apply_profile_unmask_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]
