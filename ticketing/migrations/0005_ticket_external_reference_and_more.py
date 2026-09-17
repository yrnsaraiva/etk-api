from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0002_initial'),
        ('ticketing', '0004_ticket_note'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='ticket',
            name='external_reference',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AlterField(
            model_name='ticket',
            name='payment',
            field=models.CharField(choices=[('pending', 'Pendente'), ('paid', 'Pago'), ('failed', 'Falhou'), ('refunded', 'Reembolsado'), ('invited', 'Convite')], default='pending', max_length=20),
        ),
        migrations.AddIndex(
            model_name='ticket',
            index=models.Index(fields=['issued_to', 'external_reference'], name='ticketing_t_issued__658312_idx'),
        ),
        migrations.AddConstraint(
            model_name='ticket',
            constraint=models.UniqueConstraint(condition=models.Q(('external_reference', ''), _negated=True), fields=('issued_to', 'external_reference'), name='uniq_ticket_partner_external_reference'),
        ),
    ]
