from django.conf import settings
from django.utils.module_loading import import_string

from .base import PaymentProvider

_instances: dict[str, PaymentProvider] = {}


def get_provider(name: str | None = None) -> PaymentProvider:
    name = name or settings.PAYMENT_PROVIDER
    if name not in _instances:
        _instances[name] = import_string(settings.PAYMENT_PROVIDERS[name])()
    return _instances[name]


def reset():  # usado nos testes
    _instances.clear()
