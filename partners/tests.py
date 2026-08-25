from django.test import TestCase
from rest_framework.test import APIClient

from catalog.models import Event
from partners.models import ApiKey, User


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
