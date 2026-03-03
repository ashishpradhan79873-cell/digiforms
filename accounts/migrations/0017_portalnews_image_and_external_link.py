from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0016_wallettransaction"),
    ]

    operations = [
        migrations.AddField(
            model_name="portalnews",
            name="external_link",
            field=models.URLField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="portalnews",
            name="image",
            field=models.ImageField(blank=True, null=True, upload_to="news_images/"),
        ),
    ]

