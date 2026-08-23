import os
from datetime import timedelta
from pathlib import Path
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = False
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
# PostgreSQL em produção: o select_for_update() usado na reserva de vagas
# não tem efeito real no SQLite.
# if os.getenv("DATABASE_URL"):
#     import dj_database_url
#     DATABASES = {"default": dj_database_url.config(conn_max_age=600, ssl_require=not DEBUG)}
# else:
#     DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3",
#                              "NAME": BASE_DIR / "db.sqlite3"}}
#

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

POSTGRES_LOCALLY = True

if not DEBUG or POSTGRES_LOCALLY:
    DATABASES['default'] = dj_database_url.parse(
        'postgresql://postgres:brRIZgVGtoILwrvBKWpTcjGENRDLeUDn@postgres.railway.internal:5432/railway'
    )


AUTH_USER_MODEL = "partners.User"
LANGUAGE_CODE = "pt"
TIME_ZONE = "UTC"
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
    "EXCEPTION_HANDLER": "config.envelope.envelope_exception_handler",
}
SIMPLE_JWT = {"ACCESS_TOKEN_LIFETIME": timedelta(minutes=60)}

PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "debitopay")
PAYMENT_PROVIDERS = {
    "debitopay": "payments.providers.debitopay.DebitoPayProvider",
    "fake": "payments.providers.fake.FakeProvider",
}
DEBITOPAY = {
    "BASE_URL": os.getenv("DEBITOPAY_BASE_URL", "https://api.debitopay.com"),
    "SECRET_KEY": os.getenv("DEBITOPAY_SECRET_KEY", ""),
    "WEBHOOK_SECRET": os.getenv("DEBITOPAY_WEBHOOK_SECRET", ""),
    "SIGNATURE_HEADER": os.getenv("DEBITOPAY_SIGNATURE_HEADER", "X-Debito-Signature"),
    "TIMEOUT": 30,
}
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8901")

DEFAULT_CURRENCY = "MZN"
TICKET_RESERVATION_MINUTES = 15
