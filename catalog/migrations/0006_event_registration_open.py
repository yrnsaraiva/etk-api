from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0005_event_offers_shirts"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="registration_open",
            field=models.BooleanField(
                default=True,
                help_text="Inscrições/vendas abertas. Desmarque para encerrar (ex.: ao abrir a "
                          "confirmação de presença); quem já tem bilhete não é afectado.",
            ),
        ),
    ]
