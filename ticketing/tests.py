from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from catalog.models import Event, Price
from partners.models import ApiKey, User
from ticketing.models import PartnerDelivery, Ticket
from ticketing.services import (
    TicketError, check_in, confirm_payment, create_ticket,
    expire_stale_tickets, parse_qr, reclaim_and_confirm, refund, release,
)
from ticketing.webhooks import deliver_pending_webhooks


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

    def test_id_nu_sem_assinatura_e_recusado(self):
        """Com IDs previsíveis (TCKT + sequência), aceitar o ID nu deixaria
        entrar quem adivinhasse o ID de um bilhete pago."""
        self.assertIsNone(parse_qr(self.ticket.id))
        result, _, _ = check_in(qr_value=self.ticket.id, staff_user=self.org)
        self.assertEqual(result, "invalid_qr")

    def test_bilhete_por_pagar_nao_entra(self):
        pendente = self.emitir("258842222222")
        result, _, _ = check_in(qr_value=pendente.qr_value, staff_user=self.org)
        self.assertEqual(result, "not_paid")

    def test_convite_entra(self):
        from ticketing.services import issue_invites

        [convite] = issue_invites(
            price_id=self.price.id, event_id=self.event.id, organizer=self.org
        )
        result, _, t = check_in(qr_value=convite.qr_value, staff_user=self.org)
        self.assertEqual(result, "ok")
        self.assertTrue(t.entered)
        result, _, _ = check_in(qr_value=convite.qr_value, staff_user=self.org)
        self.assertEqual(result, "already_entered")

    def test_bilhete_cancelado_nao_entra(self):
        cancelado = confirm_payment(self.emitir("258842222233"), provider="fake",
                                    provider_reference="ref2")
        cancelado.status = Ticket.Status.CANCELLED
        cancelado.save(update_fields=["status"])
        result, _, _ = check_in(qr_value=cancelado.qr_value, staff_user=self.org)
        self.assertEqual(result, "not_paid")

    def test_organizador_alheio_nao_valida(self):
        outro = User.objects.create_user("outro", email="o2@test.local", password="x1234567")
        result, _, _ = check_in(qr_value=self.ticket.qr_value, staff_user=outro)
        self.assertEqual(result, "not_found")


class ContratoExternoTests(Base):
    """A forma exata que o cliente do parceiro espera."""

    def test_rota_de_callback_inseguro_foi_removida(self):
        """Fase 0.1: qualquer pessoa marcava um bilhete como pago enviando
        {"ticketId", "status": "succeeded"} a esta rota, sem assinatura
        nenhuma. A confirmação só pode vir do webhook assinado ou da
        reconciliação."""
        r = self.client.post("/back/payments/callback",
                             {"ticketId": self.emitir().id, "status": "succeeded"},
                             format="json")
        self.assertEqual(r.status_code, 404)

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


class ReclamacaoTardiaTests(Base):
    """Fase 2.1: um pagamento que chega depois de a reserva expirar nunca
    pode ser recusado — só fica em revisão se não houver vaga."""

    def test_com_vaga_livre_confirma_como_pago(self):
        t = self.emitir()
        release(t, Ticket.Payment.FAILED)   # simula o cron de expiração
        t.refresh_from_db()
        self.assertEqual(t.payment, Ticket.Payment.FAILED)

        t = reclaim_and_confirm(t, provider="debitopay", provider_reference="ref-tardio")
        self.assertEqual(t.payment, Ticket.Payment.PAID)
        self.assertEqual(t.status, Ticket.Status.VALID)
        self.price.refresh_from_db()
        self.assertEqual(self.price.quantity_reserved, 1)

    def test_sem_vaga_fica_em_revisao(self):
        # price (Base) só tem 2 vagas: a primeira fica paga e ocupa-a.
        t1 = self.emitir("258841111111")
        confirm_payment(t1, provider="debitopay", provider_reference="ref1")
        t2 = self.emitir("258842222222")
        release(t2, Ticket.Payment.FAILED)   # t2 expira sem pagar, liberta a 2ª vaga
        # outra compra ocupa de novo a vaga que t2 libertou, antes do
        # pagamento tardio de t2 chegar.
        t3 = self.emitir("258843333333")
        confirm_payment(t3, provider="debitopay", provider_reference="ref3")

        t2 = reclaim_and_confirm(t2, provider="debitopay", provider_reference="ref-tardio")
        self.assertEqual(t2.payment, Ticket.Payment.REVIEW)
        self.price.refresh_from_db()
        self.assertEqual(self.price.quantity_reserved, 2)   # t1 e t3, não t2

    def test_review_reconfirmado_quando_liberta_vaga(self):
        """A ação de admin 'Confirmar pagamento' chama a mesma função sobre
        um ticket já em review."""
        t1 = confirm_payment(self.emitir("258841111111"), provider="debitopay",
                             provider_reference="ref1")
        t2 = self.emitir("258842222222")
        release(t2, Ticket.Payment.FAILED)
        t3 = confirm_payment(self.emitir("258843333333"), provider="debitopay",
                             provider_reference="ref3")

        t2 = reclaim_and_confirm(t2, provider="debitopay", provider_reference="ref-tardio")
        self.assertEqual(t2.payment, Ticket.Payment.REVIEW)

        refund(t1)   # liberta uma das duas vagas ocupadas (t1 e t3)
        t2 = reclaim_and_confirm(t2, provider="admin", provider_reference="manual")
        self.assertEqual(t2.payment, Ticket.Payment.PAID)


class ReembolsoTests(Base):
    """Fase 2.2: reembolso/chargeback anulam o bilhete e devolvem a vaga."""

    def test_reembolso_de_bilhete_pago_liberta_a_vaga_e_impede_entrada(self):
        t = confirm_payment(self.emitir(), provider="debitopay", provider_reference="ref1")
        self.price.refresh_from_db()
        antes = self.price.available

        t = refund(t)
        self.assertEqual(t.payment, Ticket.Payment.REFUNDED)
        self.assertEqual(t.status, Ticket.Status.CANCELLED)
        self.price.refresh_from_db()
        self.assertEqual(self.price.available, antes + 1)

        result, _, _ = check_in(qr_value=t.qr_value, staff_user=self.org)
        self.assertEqual(result, "not_paid")

    def test_reembolso_de_bilhete_em_revisao_nao_toca_na_vaga(self):
        # Ocupa as duas vagas do Base com outros bilhetes pagos, para o
        # bilhete "tardio" ficar mesmo sem vaga quando reclamar.
        confirm_payment(self.emitir("258841111111"), provider="debitopay", provider_reference="ref1")
        t = self.emitir("258842222222")
        release(t, Ticket.Payment.FAILED)
        confirm_payment(self.emitir("258843333333"), provider="debitopay", provider_reference="ref3")

        t = reclaim_and_confirm(t, provider="debitopay", provider_reference="tardio")
        self.assertEqual(t.payment, Ticket.Payment.REVIEW)
        self.price.refresh_from_db()
        antes = self.price.quantity_reserved

        t = refund(t)
        self.assertEqual(t.payment, Ticket.Payment.REFUNDED)
        self.price.refresh_from_db()
        self.assertEqual(self.price.quantity_reserved, antes)   # nunca tinha ocupado vaga

    def test_reembolso_e_idempotente(self):
        t = confirm_payment(self.emitir(), provider="debitopay", provider_reference="ref1")
        refund(t)
        self.price.refresh_from_db()
        vagas_depois_do_primeiro = self.price.available

        t.refresh_from_db()
        refund(t)   # segunda chamada não deve devolver a vaga outra vez
        self.price.refresh_from_db()
        self.assertEqual(self.price.available, vagas_depois_do_primeiro)


def _resp(status_code=200):
    m = Mock()
    m.status_code = status_code
    m.text = "erro" if status_code >= 400 else "ok"
    return m


class FilaDeAvisosTests(Base):
    """Fase 2.3: notify_partner só enfileira; deliver_pending_webhooks
    entrega de facto, com espera crescente e desistência ao fim de 1 dia."""

    def setUp(self):
        super().setUp()
        self.org.webhook_url = "https://parceiro.example/webhook"
        self.org.webhook_secret = "segredo-do-parceiro"
        self.org.save()

    def test_notify_partner_so_enfileira_nao_faz_pedido_http(self):
        t = confirm_payment(self.emitir(), provider="fake", provider_reference="ref1")
        self.assertEqual(PartnerDelivery.objects.filter(ticket=t).count(), 1)
        entrega = PartnerDelivery.objects.get(ticket=t)
        self.assertEqual(entrega.event, "ticket.paid")
        self.assertIsNone(entrega.delivered_at)

    def test_sem_webhook_configurado_nao_enfileira(self):
        self.org.webhook_url = ""
        self.org.save()
        confirm_payment(self.emitir(), provider="fake", provider_reference="ref1")
        self.assertEqual(PartnerDelivery.objects.count(), 0)

    @patch("ticketing.webhooks.requests.post")
    def test_entrega_com_sucesso_marca_delivered(self, post):
        post.return_value = _resp(200)
        t = confirm_payment(self.emitir(), provider="fake", provider_reference="ref1")

        stats = deliver_pending_webhooks()
        self.assertEqual(stats["entregues"], 1)

        entrega = PartnerDelivery.objects.get(ticket=t)
        self.assertIsNotNone(entrega.delivered_at)
        self.assertEqual(entrega.attempts, 1)
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["X-ETK-Delivery-ID"], str(entrega.pk))
        self.assertEqual(headers["X-ETK-Event"], "ticket.paid")

    @patch("ticketing.webhooks.requests.post")
    def test_falha_agenda_nova_tentativa_com_espera_crescente(self, post):
        post.return_value = _resp(500)
        t = confirm_payment(self.emitir(), provider="fake", provider_reference="ref1")

        deliver_pending_webhooks()
        entrega = PartnerDelivery.objects.get(ticket=t)
        self.assertIsNone(entrega.delivered_at)
        self.assertEqual(entrega.attempts, 1)
        primeira_espera = entrega.next_attempt_at - entrega.created_at
        self.assertTrue(timedelta(minutes=0) < primeira_espera <= timedelta(minutes=1, seconds=5))

        # ainda não é altura da 2ª tentativa: não entrega outra vez já.
        stats = deliver_pending_webhooks()
        self.assertEqual(stats["entregues"], 0)
        self.assertEqual(stats["falharam"], 0)
        entrega.refresh_from_db()
        self.assertEqual(entrega.attempts, 1)

    @patch("ticketing.webhooks.requests.post")
    def test_desiste_ao_fim_de_um_dia(self, post):
        post.return_value = _resp(500)
        t = confirm_payment(self.emitir(), provider="fake", provider_reference="ref1")
        PartnerDelivery.objects.filter(ticket=t).update(
            created_at=timezone.now() - timedelta(days=2)
        )

        stats = deliver_pending_webhooks()
        self.assertEqual(stats["desistidos"], 1)
        post.assert_not_called()   # nem tenta — já passou o prazo

        entrega = PartnerDelivery.objects.get(ticket=t)
        self.assertIsNotNone(entrega.gave_up_at)
        self.assertIsNone(entrega.delivered_at)

    @patch("ticketing.webhooks.requests.post")
    def test_reembolso_gera_uma_entrega_propria_com_id_diferente(self, post):
        post.return_value = _resp(200)
        t = confirm_payment(self.emitir(), provider="fake", provider_reference="ref1")
        refund(t)

        entregas = list(PartnerDelivery.objects.filter(ticket=t).order_by("created_at"))
        self.assertEqual([e.event for e in entregas], ["ticket.paid", "ticket.refunded"])
        self.assertNotEqual(entregas[0].pk, entregas[1].pk)
