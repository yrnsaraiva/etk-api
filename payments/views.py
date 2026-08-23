import logging

from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny

from config.envelope import fail, ok

from .providers.base import InvalidSignature
from .services import handle_webhook

logger = logging.getLogger(__name__)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def debitopay_webhook(request):
    """POST /back/payments/webhooks/debitopay

    Responde 200 a tudo o que seja autêntico — mesmo a eventos que ignoramos.
    Um 4xx faz o gateway reenviar em ciclo sem que isso resolva nada.
    """
    try:
        processed, message = handle_webhook(request.body, request.headers)
    except InvalidSignature:
        logger.warning("webhook com assinatura inválida de %s",
                       request.META.get("REMOTE_ADDR"))
        return fail("Assinatura inválida.", status.HTTP_401_UNAUTHORIZED)
    except Exception:
        logger.exception("erro a processar webhook")
        return fail("Erro interno.", status.HTTP_500_INTERNAL_SERVER_ERROR)
    return ok({"processed": processed}, message)
