from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0028_vacancy_hidden_from_users"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="graduation_subjects",
            field=models.TextField(blank=True),
        ),
    ]
