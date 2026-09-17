from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ticketing', '0003_ticket_amount_ticket_currency'),
    ]

    operations = [
        migrations.AddField(
            model_name='ticket',
            name='note',
            field=models.CharField(blank=True, help_text='ex.: Patrocinador Coca-Cola', max_length=255),
        ),
    ]
