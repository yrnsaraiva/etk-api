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

if not DEBUG:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não configurada em produção")

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

DEBITOPAY = {
    "BASE_URL": os.getenv(
        "DEBITOPAY_BASE_URL",
        "https://gyqoaningqhurhvdugne.supabase.co/functions/v1",
    ),
    "SECRET_KEY": os.getenv("DEBITOPAY_SECRET_KEY", ""),        # sk_live_… / sk_sandbox_…
    "WEBHOOK_SECRET": os.getenv("DEBITOPAY_WEBHOOK_SECRET", ""),
    "SIGNATURE_HEADER": "X-Webhook-Signature",                  # fixo, definido pela Debito Pay
    "MERCHANT_ID": os.getenv("DEBITOPAY_MERCHANT_ID", ""),
    # Cada método de pagamento tem a sua própria carteira (wallet_code) na
    # Debito Pay não é o mesmo código para todos os métodos.
    "WALLETS": {
        "mpesa": os.getenv("DEBITOPAY_WALLET_MPESA", ""),
        "emola": os.getenv("DEBITOPAY_WALLET_EMOLA", ""),
        "mkesh": os.getenv("DEBITOPAY_WALLET_MKESH", ""),
        "visa_mastercard": os.getenv("DEBITOPAY_WALLET_CARD", ""),
        "payfast": os.getenv("DEBITOPAY_WALLET_PAYFAST", ""),
    },
    "DEFAULT_METHOD": os.getenv("DEBITOPAY_DEFAULT_METHOD", "mpesa"),
    "TIMEOUT": 90,
}
# Usada quando o ticket foi criado com uma chave etk_test_… (Fase 4.2): sem
# isto, uma chave "test" tinha os mesmos poderes que uma "live" e cobrava de
# verdade. Por omissão herda BASE_URL/SIGNATURE_HEADER/TIMEOUT da conta live
# (a Debito Pay parece partilhar o mesmo endpoint), só as credenciais mudam.
DEBITOPAY_SANDBOX = {
    "BASE_URL": os.getenv("DEBITOPAY_SANDBOX_BASE_URL", DEBITOPAY["BASE_URL"]),
    "SECRET_KEY": os.getenv("DEBITOPAY_SANDBOX_SECRET_KEY", ""),   # sk_sandbox_…
    "WEBHOOK_SECRET": os.getenv("DEBITOPAY_SANDBOX_WEBHOOK_SECRET", ""),
    "SIGNATURE_HEADER": DEBITOPAY["SIGNATURE_HEADER"],
    "MERCHANT_ID": os.getenv("DEBITOPAY_SANDBOX_MERCHANT_ID", ""),
    "WALLETS": {
        "mpesa": os.getenv("DEBITOPAY_SANDBOX_WALLET_MPESA", ""),
        "emola": os.getenv("DEBITOPAY_SANDBOX_WALLET_EMOLA", ""),
        "mkesh": os.getenv("DEBITOPAY_SANDBOX_WALLET_MKESH", ""),
        "visa_mastercard": os.getenv("DEBITOPAY_SANDBOX_WALLET_CARD", ""),
        "payfast": os.getenv("DEBITOPAY_SANDBOX_WALLET_PAYFAST", ""),
    },
    "DEFAULT_METHOD": os.getenv("DEBITOPAY_SANDBOX_DEFAULT_METHOD", DEBITOPAY["DEFAULT_METHOD"]),
    "TIMEOUT": DEBITOPAY["TIMEOUT"],
}
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