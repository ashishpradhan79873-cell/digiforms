from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0027_vacancy_visible_to_users"),
    ]

    operations = [
        migrations.AddField(
            model_name="vacancy",
            name="hidden_from_users",
            field=models.BooleanField(default=False),
        ),
    ]
