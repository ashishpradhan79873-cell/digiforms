from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0020_masterdatafield"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="master_data_last_saved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="master_data_unmask_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="master_data_unmask_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="master_data_unmask_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="apply_autofill_locked_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
