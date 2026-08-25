from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from catalog.models import Event, Price
from partners.models import User


class EventModelTests(TestCase):
    def setUp(self):
        self.org = User.objects.create_user(
            "org", email="org@test.local", password="Pa$$w0rd!123"
        )

    def test_id_gerado_tem_o_prefixo_certo(self):
        ev = Event.objects.create(
            organizer=self.org, name="Teste", date=timezone.now() + timedelta(days=1)
        )
        self.assertTrue(ev.id.startswith("EVNT"))

    def test_ids_gerados_sao_unicos(self):
        ids = {
            Event.objects.create(
                organizer=self.org, name=f"E{i}",
                date=timezone.now() + timedelta(days=1)
            ).id
            for i in range(5)
        }
        self.assertEqual(len(ids), 5)


class PriceStockTests(TestCase):
    def setUp(self):
        self.org = User.objects.create_user(
            "org", email="org@test.local", password="Pa$$w0rd!123"
        )
        self.event = Event.objects.create(
            organizer=self.org, name="Festival",
            date=timezone.now() + timedelta(days=30), status=Event.Status.PUBLISHED,
        )

    def test_available_desconta_o_reservado(self):
        p = Price.objects.create(event=self.event, quantity_total=10, quantity_reserved=3)
        self.assertEqual(p.available, 7)

    def test_available_nunca_fica_negativo(self):
        """Mesmo que quantity_reserved ultrapasse por algum motivo, o available para em 0."""
        p = Price.objects.create(event=self.event, quantity_total=5)
        p.quantity_reserved = 8
        self.assertEqual(p.available, 0)

    def test_constraint_impede_reservar_alem_do_total(self):
        """A rede de segurança ao nível da base de dados: nunca deve disparar em uso
        normal (a etapa 5 do domínio impede primeiro), mas prova que existe."""
        p = Price.objects.create(event=self.event, quantity_total=2)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Price.objects.filter(pk=p.pk).update(quantity_reserved=3)
                Price.objects.get(pk=p.pk).full_clean()
                # forçar a validação a nível de BD: reler e salvar dispara o CHECK
                obj = Price.objects.get(pk=p.pk)
                obj.quantity_reserved = 3
                obj.save()

    def test_nao_esta_a_venda_se_evento_nao_publicado(self):
        self.event.status = Event.Status.DRAFT
        self.event.save()
        p = Price.objects.create(event=self.event, quantity_total=5)
        self.assertFalse(p.is_on_sale())

    def test_nao_esta_a_venda_fora_da_janela(self):
        now = timezone.now()
        p = Price.objects.create(
            event=self.event, quantity_total=5,
            sales_start=now + timedelta(days=1),   # ainda não abriu
        )
        self.assertFalse(p.is_on_sale(now=now))

        p2 = Price.objects.create(
            event=self.event, quantity_total=5,
            sales_end=now - timedelta(days=1),      # já fechou
        )
        self.assertFalse(p2.is_on_sale(now=now))


class GestaoOrganizadorTests(TestCase):
    """A área do organizador: JWT, e cada um só vê o que é seu."""

    def setUp(self):
        self.org = User.objects.create_user(
            "org", email="org@test.local", password="Pa$$w0rd!123"
        )
        self.outro = User.objects.create_user(
            "outro", email="outro@test.local", password="Pa$$w0rd!123"
        )
        self.client = APIClient()
        token = RefreshToken.for_user(self.org).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_criar_evento(self):
        r = self.client.post("/api/events/", {
            "name": "Novo evento", "date": (timezone.now() + timedelta(days=10)).isoformat(),
            "province": "Maputo",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(Event.objects.filter(pk=r.data["id"], organizer=self.org).exists())

    def test_nao_ve_eventos_de_outro_organizador(self):
        Event.objects.create(
            organizer=self.outro, name="Alheio", date=timezone.now() + timedelta(days=5)
        )
        r = self.client.get("/api/events/")
        self.assertEqual(r.data["count"], 0)

    def test_sem_token_e_recusado(self):
        c = APIClient()
        r = c.get("/api/events/")
        self.assertEqual(r.status_code, 401)

    def test_nao_pode_criar_lote_em_evento_alheio(self):
        alheio = Event.objects.create(
            organizer=self.outro, name="Alheio", date=timezone.now() + timedelta(days=5)
        )
        r = self.client.post("/api/prices/", {
            "event": alheio.id, "name": "Geral", "amount": "100", "quantity_total": 5,
        }, format="json")
        self.assertEqual(r.status_code, 400)

    def test_emitir_chave_devolve_valor_em_claro_uma_vez(self):
        r = self.client.post("/api/api-keys/", {"label": "site"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertIn("key", r.data)
        self.assertTrue(r.data["key"].startswith("etk_"))

        r2 = self.client.get("/api/api-keys/")
        self.assertNotIn("key", r2.data["results"][0])
