"""Prova que a reserva nao vende a mais sob concorrencia.

EXIGE PostgreSQL. No SQLite o select_for_update() nao tranca e este teste
passa por acidente, sem provar nada.

    docker run -d -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=etk -p 5432:5432 postgres:16
    export DATABASE_URL=postgresql://postgres:dev@localhost:5432/etk
    python manage.py migrate
    python manage.py shell < concurrency_test.py
"""
import threading
from datetime import timedelta
from decimal import Decimal

from django.db import connection, connections
from django.utils import timezone

from partners.models import User
from catalog.models import Event, Price
from ticketing.models import Ticket
from ticketing.services import create_ticket, TicketError

VAGAS = 3
COMPRADORES = 10

SQLITE = connection.vendor == "sqlite"
if SQLITE:
    print("\n!!! A correr em SQLite. O select_for_update() nao tranca nada e o")
    print("!!! ficheiro so aceita um escritor: espere 'database is locked'.")
    print("!!! Isto NAO prova ausencia de corrida — e a demonstracao de que o")
    print("!!! SQLite nao serve para este trabalho. Use PostgreSQL.\n")

org = User.objects.create_user(f"org{timezone.now().timestamp()}",
                              email=f"o{timezone.now().timestamp()}@x.com",
                              password="Pa$$w0rd!123")
ev = Event.objects.create(organizer=org, name="Teste de corrida",
                          date=timezone.now() + timedelta(days=10),
                          status=Event.Status.PUBLISHED)
pr = Price.objects.create(event=ev, name="Geral", amount=Decimal("100.00"),
                          quantity_total=VAGAS)

sucessos, recusas, erros = [], [], []
barreira = threading.Barrier(COMPRADORES)

def comprar(n):
    barreira.wait()                     # todas arrancam no mesmo instante
    try:
        t = create_ticket(price_id=pr.id, event_id=ev.id,
                          phone=f"2588400000{n:02d}", issued_to=org)
        sucessos.append(t.id)
    except TicketError as e:
        recusas.append(str(e))
    except Exception as e:
        erros.append(repr(e))
    finally:
        connections.close_all()

threads = [threading.Thread(target=comprar, args=(i,)) for i in range(COMPRADORES)]
for t in threads: t.start()
for t in threads: t.join()

pr.refresh_from_db()
emitidos = Ticket.objects.filter(price=pr).count()

print(f"vagas            : {VAGAS}")
print(f"compradores      : {COMPRADORES}")
print(f"sucessos         : {len(sucessos)}")
print(f"recusas          : {len(recusas)}")
print(f"erros inesperados: {len(erros)}", erros[:2])
print(f"bilhetes emitidos: {emitidos}")
print(f"quantity_reserved: {pr.quantity_reserved} / {pr.quantity_total}")

assert emitidos <= VAGAS, f"VENDEU A MAIS: {emitidos} bilhetes para {VAGAS} vagas"

if SQLITE:
    print("\nComo previsto, o SQLite nao aguenta: as threads que nao conseguiram")
    print("o ficheiro rebentaram com 'database is locked' em vez de esperarem.")
    print("Repita com PostgreSQL — deve dar", VAGAS, "sucessos e",
          COMPRADORES - VAGAS, "recusas limpas.")
else:
    assert emitidos == VAGAS, f"esperava {VAGAS} bilhetes, saiu {emitidos}"
    assert pr.quantity_reserved == VAGAS
    assert len(erros) == 0, f"erros inesperados: {erros[:3]}"
    assert len(recusas) == COMPRADORES - VAGAS
    print("\nOK — exatamente", VAGAS, "bilhetes, nem mais nem menos.")
