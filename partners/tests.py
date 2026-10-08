from django.test import TestCase
from rest_framework.test import APIClient

from catalog.models import Event
from partners.models import ApiKey, User, WebhookEndpoint


class ApiKeyTests(TestCase):
    def setUp(self):
        self.org = User.objects.create_user(
            "org", email="org@test.local", password="Pa$$w0rd!123"
        )
        self.key, self.raw = ApiKey.issue(self.org, label="site")

    def test_chave_em_claro_nao_e_guardada(self):
        self.assertNotIn(self.raw, ApiKey.objects.get(pk=self.key.pk).key_hash)
        self.assertEqual(len(ApiKey.objects.get(pk=self.key.pk).key_hash), 64)

    def test_resolve_chave_valida(self):
        self.assertEqual(ApiKey.resolve(self.raw), self.key)

    def test_chave_inventada_nao_resolve(self):
        self.assertIsNone(ApiKey.resolve("etk_live_naoexiste12345678"))

    def test_chave_revogada_deixa_de_resolver(self):
        self.key.revoke()
        self.assertIsNone(ApiKey.resolve(self.raw))

    def test_ultimo_uso_e_registado(self):
        self.assertIsNone(self.key.last_used_at)
        ApiKey.resolve(self.raw)
        self.key.refresh_from_db()
        self.assertIsNotNone(self.key.last_used_at)


class WebhookSecretTests(TestCase):
    """Fase 2.4: com webhook_secret vazio, X-ETK-Signature é calculada com
    uma chave vazia e não protege nada — por isso não pode existir
    webhook_url sem segredo."""

    def test_webhook_url_gera_segredo_automaticamente(self):
        org = User.objects.create_user(
            "org", email="org@test.local", password="Pa$$w0rd!123",
            webhook_url="https://parceiro.example/webhook",
        )
        self.assertTrue(org.webhook_secret)
        self.assertGreaterEqual(len(org.webhook_secret), 32)

    def test_sem_webhook_url_nao_gera_segredo(self):
        org = User.objects.create_user(
            "org", email="org@test.local", password="Pa$$w0rd!123"
        )
        self.assertEqual(org.webhook_secret, "")

    def test_segredo_existente_nao_e_substituido(self):
        org = User.objects.create_user(
            "org", email="org@test.local", password="Pa$$w0rd!123",
            webhook_url="https://parceiro.example/webhook", webhook_secret="ja-tinha-um",
        )
        org.company_name = "Outro nome"
        org.save()
        org.refresh_from_db()
        self.assertEqual(org.webhook_secret, "ja-tinha-um")

    def test_definir_webhook_url_mais_tarde_tambem_gera_segredo(self):
        org = User.objects.create_user(
            "org", email="org@test.local", password="Pa$$w0rd!123"
        )
        self.assertEqual(org.webhook_secret, "")
        org.webhook_url = "https://parceiro.example/webhook"
        org.save()
        self.assertTrue(org.webhook_secret)


class AutenticacaoExternaTests(TestCase):
    def setUp(self):
        self.org = User.objects.create_user(
            "org", email="org@test.local", password="Pa$$w0rd!123"
        )
        _, self.raw = ApiKey.issue(self.org)
        self.client = APIClient()

    def _get(self, token=None):
        if token:
            self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return self.client.get("/back/borrow/external/events")

    def test_sem_chave_recusa(self):
        self.assertEqual(self._get().status_code, 401)

    def test_chave_valida_aceita(self):
        self.assertEqual(self._get(self.raw).status_code, 200)

    def test_chave_invalida_recusa(self):
        r = self._get("etk_live_inventada1234567890")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.data["status"], "error")


class WebhookEndpointTests(TestCase):
    """Vários destinos por organizador: o webhook_url antigo continua a ser o primeiro."""

    def setUp(self):
        # sem password: o teste não precisa de iniciar sessão (e não deixa credenciais literais no diff)
        self.org = User.objects.create_user("org", email="org@test.local")

    def test_destino_gera_segredo_e_nao_o_substitui(self):
        ep = WebhookEndpoint.objects.create(owner=self.org, url="https://app.example/webhooks/etk/")
        self.assertGreaterEqual(len(ep.secret), 32)
        segredo = ep.secret
        ep.label = "App de membros"
        ep.save()
        ep.refresh_from_db()
        self.assertEqual(ep.secret, segredo)
        outro = WebhookEndpoint.objects.create(owner=self.org, url="https://b.example/hook")
        self.assertNotEqual(outro.secret, segredo)

    def test_destinos_a_notificar(self):
        self.assertEqual(self.org.webhook_endpoints_to_notify(), [])
        ep = WebhookEndpoint.objects.create(owner=self.org, url="https://app.example/hook")
        self.assertEqual(self.org.webhook_endpoints_to_notify(), [ep])
        self.org.webhook_url = "https://site.example/hook"
        self.org.save()
        self.assertEqual(self.org.webhook_endpoints_to_notify(), [None, ep])  # o campo antigo vem primeiro
        ep.is_active = False
        ep.save()
        self.assertEqual(self.org.webhook_endpoints_to_notify(), [None])
