from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0014_portalnews"),
    ]

    operations = [
        migrations.AddField(
            model_name="portalnews",
            name="details_pdf",
            field=models.FileField(blank=True, null=True, upload_to="news_pdfs/"),
        ),
        migrations.AddField(
            model_name="portalnews",
            name="details_color",
            field=models.CharField(default="#334155", max_length=7),
        ),
        migrations.AddField(
            model_name="portalnews",
            name="title_color",
            field=models.CharField(default="#0f172a", max_length=7),
        ),
    ]
