"""Prova que a reserva não vende mais bilhetes do que existem.

    python manage.py test_concurrency --vagas 3 --compradores 10

EXIGE PostgreSQL. No SQLite o select_for_update() não tranca e o teste não
prova nada — serve apenas para demonstrar que o SQLite não serve.
"""

import threading
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, connections
from django.utils import timezone

from catalog.models import Event, Price
from partners.models import User
from ticketing.models import Ticket
from ticketing.services import TicketError, create_ticket


class Command(BaseCommand):
    help = "Lança N compradores simultâneos sobre M vagas e verifica a contagem."

    def add_arguments(self, parser):
        parser.add_argument("--vagas", type=int, default=3)
        parser.add_argument("--compradores", type=int, default=10)

    def handle(self, *args, **o):
        vagas, compradores = o["vagas"], o["compradores"]
        if compradores <= vagas:
            raise CommandError("--compradores tem de ser maior que --vagas.")

        sqlite = connection.vendor == "sqlite"
        if sqlite:
            self.stdout.write(self.style.WARNING(
                "\nA correr em SQLite. O select_for_update() não tranca nada e o\n"
                "ficheiro só aceita um escritor de cada vez: espere erros\n"
                "'database is locked'. Isto NÃO prova ausência de corrida — é a\n"
                "demonstração de que o SQLite não serve. Use PostgreSQL.\n"
            ))

        stamp = int(timezone.now().timestamp())
        org = User.objects.create_user(
            f"conc{stamp}", email=f"conc{stamp}@test.local", password="Pa$$w0rd!123"
        )
        event = Event.objects.create(
            organizer=org, name="Teste de corrida",
            date=timezone.now() + timedelta(days=10), status=Event.Status.PUBLISHED,
        )
        price = Price.objects.create(
            event=event, name="Geral", amount=Decimal("100.00"), quantity_total=vagas
        )

        sucessos, recusas, erros = [], [], []
        barreira = threading.Barrier(compradores)

        def comprar(n):
            barreira.wait()          # todas arrancam no mesmo instante
            try:
                t = create_ticket(price_id=price.id, event_id=event.id,
                                  phone=f"2588400{n:05d}", issued_to=org)
                sucessos.append(t.id)
            except TicketError as exc:
                recusas.append(str(exc))
            except Exception as exc:
                erros.append(repr(exc))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=comprar, args=(i,)) for i in range(compradores)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        price.refresh_from_db()
        emitidos = Ticket.objects.filter(price=price).count()

        self.stdout.write(f"\n  vagas             : {vagas}")
        self.stdout.write(f"  compradores       : {compradores}")
        self.stdout.write(f"  sucessos          : {len(sucessos)}")
        self.stdout.write(f"  recusas           : {len(recusas)}")
        self.stdout.write(f"  erros inesperados : {len(erros)} {erros[:2]}")
        self.stdout.write(f"  bilhetes emitidos : {emitidos}")
        self.stdout.write(f"  quantity_reserved : {price.quantity_reserved} / {price.quantity_total}\n")

        if emitidos > vagas:
            raise CommandError(f"VENDEU A MAIS: {emitidos} bilhetes para {vagas} vagas.")

        if sqlite:
            self.stdout.write(self.style.WARNING(
                f"Como previsto, o SQLite não aguentou. Repita com PostgreSQL —\n"
                f"deve dar {vagas} sucessos e {compradores - vagas} recusas limpas.\n"
            ))
            return

        if emitidos != vagas or erros or len(recusas) != compradores - vagas:
            raise CommandError("resultado inesperado — ver números acima.")
        self.stdout.write(self.style.SUCCESS(
            f"OK — exatamente {vagas} bilhetes, nem mais nem menos.\n"
        ))
