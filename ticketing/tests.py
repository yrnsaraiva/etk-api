from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from catalog.models import Event, Price
from partners.models import ApiKey, User
from ticketing.models import Ticket
from ticketing.services import (
    TicketError, check_in, confirm_payment, create_ticket,
    expire_stale_tickets, parse_qr,
)


class Base(TestCase):
    def setUp(self):
        self.org = User.objects.create_user(
            "org", email="org@test.local", password="Pa$$w0rd!123"
        )
        self.event = Event.objects.create(
            organizer=self.org, name="Festival", category="social_run",
            date=timezone.now() + timedelta(days=30), province="Maputo",
            location_details="Noctis", status=Event.Status.PUBLISHED,
        )
        self.price = Price.objects.create(
            event=self.event, name="Geral", amount=Decimal("300.00"), quantity_total=2
        )
        _, self.raw = ApiKey.issue(self.org)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")

    def emitir(self, phone="258841111111"):
        return create_ticket(price_id=self.price.id, event_id=self.event.id,
                             phone=phone, issued_to=self.org)


class ReservaTests(Base):
    def test_emitir_reserva_uma_vaga(self):
        self.emitir()
        self.price.refresh_from_db()
        self.assertEqual(self.price.quantity_reserved, 1)
        self.assertEqual(self.price.available, 1)

    def test_nao_emite_alem_do_stock(self):
        self.emitir("258841111111")
        self.emitir("258842222222")
        with self.assertRaises(TicketError):
            self.emitir("258843333333")
        self.assertEqual(Ticket.objects.count(), 2)

    def test_price_de_outro_evento_e_recusado(self):
        outro = Event.objects.create(
            organizer=self.org, name="Outro",
            date=timezone.now() + timedelta(days=5), status=Event.Status.PUBLISHED,
        )
        with self.assertRaises(TicketError):
            create_ticket(price_id=self.price.id, event_id=outro.id,
                          phone="258841111111", issued_to=self.org)

    def test_chave_de_outro_organizador_nao_compra_neste_evento(self):
        """Isolamento entre parceiros: a chave do organizador B não pode criar
        bilhete contra o lote do organizador A, mesmo com o eventId certo."""
        outro_org = User.objects.create_user(
            "outro_org", email="outro_org@test.local", password="Pa$$w0rd!123"
        )
        with self.assertRaises(TicketError):
            create_ticket(price_id=self.price.id, event_id=self.event.id,
                          phone="258841111111", issued_to=outro_org)
        # nenhuma vaga foi tocada
        self.price.refresh_from_db()
        self.assertEqual(self.price.quantity_reserved, 0)

    def test_evento_em_rascunho_nao_vende(self):
        self.event.status = Event.Status.DRAFT
        self.event.save()
        with self.assertRaises(TicketError):
            self.emitir()

    def test_preco_e_congelado_no_bilhete(self):
        """Alterar o preço do lote não muda bilhetes já emitidos."""
        t = self.emitir()
        self.assertEqual(t.amount, Decimal("300.00"))
        self.price.amount = Decimal("999.00")
        self.price.save()
        t.refresh_from_db()
        self.assertEqual(t.amount, Decimal("300.00"))
        self.assertEqual(t.to_api()["amount"], 300.0)

    def test_expiracao_devolve_a_vaga(self):
        t = self.emitir()
        Ticket.objects.filter(pk=t.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        self.assertEqual(expire_stale_tickets(), 1)
        self.price.refresh_from_db()
        self.assertEqual(self.price.available, 2)

    def test_bilhete_pago_nao_expira(self):
        t = self.emitir()
        confirm_payment(t, provider="fake", provider_reference="x")
        Ticket.objects.filter(pk=t.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        self.assertEqual(expire_stale_tickets(), 0)


class PagamentoTests(Base):
    def test_confirmar_marca_pago(self):
        t = confirm_payment(self.emitir(), provider="fake", provider_reference="ref1")
        self.assertEqual(t.payment, Ticket.Payment.PAID)
        self.assertIsNotNone(t.paid_at)

    def test_confirmar_duas_vezes_e_idempotente(self):
        t = self.emitir()
        confirm_payment(t, provider="fake", provider_reference="ref1")
        confirm_payment(t, provider="fake", provider_reference="ref1")
        t.refresh_from_db()
        self.assertEqual(t.attempts.count(), 1)


class CheckInTests(Base):
    def setUp(self):
        super().setUp()
        self.ticket = confirm_payment(self.emitir(), provider="fake",
                                      provider_reference="ref1")

    def test_qr_valido_autoriza(self):
        result, _, t = check_in(qr_value=self.ticket.qr_value, staff_user=self.org)
        self.assertEqual(result, "ok")
        self.assertTrue(t.entered)

    def test_segunda_entrada_e_recusada(self):
        check_in(qr_value=self.ticket.qr_value, staff_user=self.org)
        result, _, _ = check_in(qr_value=self.ticket.qr_value, staff_user=self.org)
        self.assertEqual(result, "already_entered")

    def test_assinatura_adulterada_e_recusada(self):
        falso = f"{self.ticket.id}|0000000000000000"
        self.assertIsNone(parse_qr(falso))
        result, _, _ = check_in(qr_value=falso, staff_user=self.org)
        self.assertEqual(result, "invalid_qr")

    def test_bilhete_por_pagar_nao_entra(self):
        pendente = self.emitir("258842222222")
        result, _, _ = check_in(qr_value=pendente.qr_value, staff_user=self.org)
        self.assertEqual(result, "not_paid")

    def test_organizador_alheio_nao_valida(self):
        outro = User.objects.create_user("outro", email="o2@test.local", password="x1234567")
        result, _, _ = check_in(qr_value=self.ticket.qr_value, staff_user=outro)
        self.assertEqual(result, "not_found")


class ContratoExternoTests(Base):
    """A forma exata que o cliente do parceiro espera."""

    def test_lista_de_eventos_tem_envelope(self):
        r = self.client.get("/back/borrow/external/events")
        self.assertEqual(r.data["status"], "success")
        self.assertIn("data", r.data)
        self.assertIsInstance(r.data["data"], list)

    def test_evento_tem_campos_camelcase(self):
        r = self.client.get(f"/back/borrow/external/events/{self.event.id}")
        d = r.data["data"]
        for campo in ("id", "name", "date", "imageUrl", "location",
                      "prices", "totalTicketsPurchased"):
            self.assertIn(campo, d, f"falta {campo}")
        self.assertIn("province", d["location"])
        self.assertIn("amount", d["prices"][0])

    def test_evento_em_rascunho_nao_aparece(self):
        self.event.status = Event.Status.DRAFT
        self.event.save()
        r = self.client.get("/back/borrow/external/events")
        self.assertEqual(len(r.data["data"]), 0)

    def test_telefone_invalido_e_recusado(self):
        r = self.client.post("/back/borrow/external/tickets",
                             {"priceId": self.price.id, "eventId": self.event.id,
                              "phone": "841234567"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["status"], "error")

    def test_bilhete_de_outro_parceiro_nao_e_visivel(self):
        t = self.emitir()
        outro = User.objects.create_user("p2", email="p2@test.local", password="x1234567")
        _, raw2 = ApiKey.issue(outro)
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {raw2}")
        r = c.get(f"/back/borrow/external/tickets/{t.id}")
        self.assertEqual(r.status_code, 404)

    def test_lista_nao_mostra_eventos_de_outro_organizador(self):
        """O bug de isolamento: sem o filtro por organizador, qualquer chave
        via a agenda inteira da plataforma, não só os seus próprios eventos."""
        outro_org = User.objects.create_user(
            "outro_org", email="outro_org@test.local", password="Pa$$w0rd!123"
        )
        Event.objects.create(
            organizer=outro_org, name="Evento alheio",
            date=timezone.now() + timedelta(days=10), status=Event.Status.PUBLISHED,
        )
        r = self.client.get("/back/borrow/external/events")
        nomes = [e["name"] for e in r.data["data"]]
        self.assertIn("Festival", nomes)
        self.assertNotIn("Evento alheio", nomes)

    def test_detalhe_de_evento_alheio_da_404(self):
        outro_org = User.objects.create_user(
            "outro_org", email="outro_org@test.local", password="Pa$$w0rd!123"
        )
        alheio = Event.objects.create(
            organizer=outro_org, name="Evento alheio",
            date=timezone.now() + timedelta(days=10), status=Event.Status.PUBLISHED,
        )
        r = self.client.get(f"/back/borrow/external/events/{alheio.id}")
        self.assertEqual(r.status_code, 404)
