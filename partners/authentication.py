from rest_framework import authentication, exceptions

from .models import ApiKey


class ApiKeyAuthentication(authentication.BaseAuthentication):
    """Lê `Authorization: Bearer etk_live_...` — o esquema que o cliente já usa."""

    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].decode().lower() != self.keyword.lower():
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Cabeçalho Authorization malformado.")

        raw = header[1].decode()
        if not raw.startswith("etk_"):
            return None  # deixa passar para o JWT (área do organizador)

        api_key = ApiKey.resolve(raw)
        if api_key is None:
            raise exceptions.AuthenticationFailed("Chave de API inválida ou revogada.")

        request.api_key = api_key
        return (api_key.owner, api_key)

    def authenticate_header(self, request):
        return self.keyword
