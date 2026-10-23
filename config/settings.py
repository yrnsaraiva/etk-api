import os
from datetime import timedelta
from pathlib import Path
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]  # sem valor por omissão: falha no arranque se faltar
# Chave dedicada à assinatura dos QR de entrada, separada da SECRET_KEY: a
# SECRET_KEY pode ser rodada sem invalidar os bilhetes já emitidos.
QR_SIGNING_KEY = os.environ["QR_SIGNING_KEY"]
DEBUG = os.getenv("DEBUG", "0") == "1"
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "*").split(",")
CSRF_TRUSTED_ORIGINS = [o for o in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if o]

if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "rest_framework", "django_filters",
    "partners", "catalog", "ticketing", "payments",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",   # serve o /admin em produção
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [], "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

DATABASE_URL = os.getenv("DATABASE_URL")

if not DEBUG and not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não configurada em produção")

if DATABASE_URL:
    # Usa Postgres sempre que DATABASE_URL existir, mesmo com DEBUG=1 — é o
    # que permite correr os testes (incluindo test_concurrency, que só prova
    # alguma coisa em Postgres) sem também ligar o redirecionamento HTTPS
    # forçado, que quebra o test client (só fala HTTP simples).
    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=600,
            ssl_require=False,
        )
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_USER_MODEL = "partners.User"
LANGUAGE_CODE = "pt"
TIME_ZONE = "Africa/Maputo"
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_FILTER_BACKENDS": ("django_filters.rest_framework.DjangoFilterBackend",),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "EXCEPTION_HANDLER": "config.envelope.envelope_exception_handler",
}
SIMPLE_JWT = {"ACCESS_TOKEN_LIFETIME": timedelta(minutes=60)}

# iMali.Way (Paytek) — o gateway de pagamento. Ver payments/imaliway.py.
# As credenciais vêm sempre do ambiente; nunca ficam no repositório.
IMALIWAY_DEFAULT_URL = "https://paytek-africa.net:11900/api/partners/imaliway/v2"
IMALIWAY = {
    "BASE_URL": os.getenv("IMALIWAY_BASE_URL", IMALIWAY_DEFAULT_URL),
    "API_KEY": os.getenv("IMALIWAY_API_KEY", ""),
    "CLIENT_ID": os.getenv("IMALIWAY_CLIENT_ID", ""),                # cabeçalho X-Client-ID
    "PUBLIC_KEY": os.getenv("IMALIWAY_PUBLIC_KEY", ""),              # PEM (aceita "\n" literais)
    "STORE_ACCOUNT_NUMBER": os.getenv("IMALIWAY_STORE_ACCOUNT_NUMBER", ""),   # conta STORE (9 dígitos)
    "WEBHOOK_SECRET": os.getenv("IMALIWAY_WEBHOOK_SECRET", ""),
    "DEFAULT_METHOD": os.getenv("IMALIWAY_DEFAULT_METHOD", "mpesa"),
    # Tempo que o cliente tem para aprovar o push. 0 = não enviar, e vale o padrão
    # da iMali.Way (2 minutos), como no exemplo de teste da Paytek. Se > 0, nunca
    # ultrapassa a reserva do bilhete.
    "PUSH_EXPIRATION_MINUTES": int(os.getenv("IMALIWAY_PUSH_EXPIRATION_MINUTES", "0")),
    "CONNECT_TIMEOUT": 10,      # segundos para abrir a ligação
    "TIMEOUT": 30,              # segundos à espera da resposta
}
# Chaves de teste (`etk_test_…`) cobram na sandbox da iMali.Way, nunca na conta live.
# A documentação não lista um endereço separado: só as credenciais mudam.
IMALIWAY_SANDBOX = {
    "BASE_URL": os.getenv("IMALIWAY_SANDBOX_BASE_URL", IMALIWAY["BASE_URL"]),
    "API_KEY": os.getenv("IMALIWAY_SANDBOX_API_KEY", ""),
    "CLIENT_ID": os.getenv("IMALIWAY_SANDBOX_CLIENT_ID", ""),
    "PUBLIC_KEY": os.getenv("IMALIWAY_SANDBOX_PUBLIC_KEY", ""),
    "STORE_ACCOUNT_NUMBER": os.getenv("IMALIWAY_SANDBOX_STORE_ACCOUNT_NUMBER", ""),
    "WEBHOOK_SECRET": os.getenv("IMALIWAY_SANDBOX_WEBHOOK_SECRET", ""),
    "DEFAULT_METHOD": os.getenv("IMALIWAY_SANDBOX_DEFAULT_METHOD", IMALIWAY["DEFAULT_METHOD"]),
    "PUSH_EXPIRATION_MINUTES": IMALIWAY["PUSH_EXPIRATION_MINUTES"],
    "CONNECT_TIMEOUT": IMALIWAY["CONNECT_TIMEOUT"],
    "TIMEOUT": IMALIWAY["TIMEOUT"],
}
# Já não viaja no pedido ao gateway (o URL do webhook é registado na Paytek):
# serve de referência para o URL público da API.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://etk-api.up.railway.app")

DEFAULT_CURRENCY = "MZN"
TICKET_RESERVATION_MINUTES = 15

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
        # o teu logger de app, se usares __name__ nos módulos (services.py, etc.)
        "": {
            "handlers": ["console"],
            "level": "INFO",
        },
    },
}