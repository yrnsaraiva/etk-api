"""API externa — o dialeto que o cliente do parceiro já fala."""

import logging

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView

from catalog.models import Event
from config.envelope import fail, ok
from partners.authentication import ApiKeyAuthentication

from .models import Ticket
from payments.providers.base import PaymentError
from payments.services import start_payment

from .services import check_in, confirm_payment, create_ticket
from .webhooks import notify_partner

logger = logging.getLogger(__name__)

EXTERNAL_AUTH = [ApiKeyAuthentication]


class ExternalEventListView(APIView):
    """GET /back/borrow/external/events

    Só devolve os eventos do organizador dono da chave — nunca os de outro
    parceiro. Sem este filtro, qualquer chave via a agenda inteira da plataforma.
    """

    authentication_classes = EXTERNAL_AUTH
    permission_classes = [IsAuthenticated]

    def get(self, request):
        events = (
            Event.objects.filter(status=Event.Status.PUBLISHED, organizer=request.user)
            .prefetch_related("prices")
            .order_by("date")
        )
        if request.query_params.get("upcoming") == "true":
            events = events.filter(date__gte=timezone.now())
        return ok([e.to_api() for e in events], "Events retrieved successfully")


class ExternalEventDetailView(APIView):
    """GET /back/borrow/external/events/{eventId}

    Mesmo isolamento: um evento de outro organizador dá 404, não 403 — não
    confirma sequer que o ID existe, para não vazar essa informação.
    """

    authentication_classes = EXTERNAL_AUTH
    permission_classes = [IsAuthenticated]

    def get(self, request, event_id):
        try:
            event = Event.objects.prefetch_related("prices").get(
                pk=event_id, status=Event.Status.PUBLISHED, organizer=request.user
            )
        except Event.DoesNotExist:
            return fail("Event not found", status.HTTP_404_NOT_FOUND)
        return ok(event.to_api(), "Event retrieved successfully")


class TicketCreateSerializer(serializers.Serializer):
    priceId = serializers.CharField()
    eventId = serializers.CharField()
    phone = serializers.RegexField(r"^258\d{9}$", error_messages={
        "invalid": "Número inválido. Use o formato 258XXXXXXXXX."
    })
    email = serializers.EmailField(required=False, allow_blank=True)
    fullName = serializers.CharField(required=False, allow_blank=True, max_length=200)
    paymentMethod = serializers.CharField(required=False, allow_blank=True, max_length=40)


class ExternalTicketCreateView(APIView):
    """POST /back/borrow/external/tickets"""

    authentication_classes = EXTERNAL_AUTH
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = TicketCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        ticket = create_ticket(
            price_id=data["priceId"],
            event_id=data["eventId"],
            phone=data["phone"],
            issued_to=request.user,
            full_name=data.get("fullName", ""),
            email=data.get("email", ""),
            payment_method=data.get("paymentMethod", ""),
        )
        # Inicia a cobrança no gateway. O bilhete nasce `pending`; o webhook
        # (ou a reconciliação) confirma.
        try:
            charge = start_payment(
                ticket,
                callback_url=f"{settings.PUBLIC_BASE_URL}/back/payments/webhooks/debitopay",
            )
        except PaymentError as exc:
            # A vaga fica reservada até expirar: o cliente pode tentar de novo
            # sem perder o lugar, e o expirador limpa se desistir.
            logger.error("falha ao iniciar cobrança de %s: %s", ticket.id, exc)
            return fail(
                "Não foi possível iniciar o pagamento. Tente novamente.",
                status.HTTP_502_BAD_GATEWAY,
                data={"ticketId": ticket.id},
            )

        ticket.refresh_from_db()
        payload = ticket.to_api()
        payload["paymentInstructions"] = charge.instructions
        return ok(payload, "Ticket created successfully", status.HTTP_201_CREATED)


class ExternalTicketDetailView(APIView):
    """GET /back/borrow/external/tickets/{ticketId} — para o parceiro sondar o pagamento."""

    authentication_classes = EXTERNAL_AUTH
    permission_classes = [IsAuthenticated]

    def get(self, request, ticket_id):
        try:
            ticket = Ticket.objects.select_related("price__event").get(
                pk=ticket_id, issued_to=request.user
            )
        except Ticket.DoesNotExist:
            return fail("Ticket not found", status.HTTP_404_NOT_FOUND)
        return ok(ticket.to_api(), "Ticket retrieved successfully")


class ExternalCheckInView(APIView):
    """POST /back/borrow/external/tickets/check-in — body: {"qrValue": "TCKT…|sig"}"""

    authentication_classes = EXTERNAL_AUTH
    permission_classes = [IsAuthenticated]

    def post(self, request):
        qr_value = (request.data.get("qrValue") or request.data.get("qr_value") or "").strip()
        if not qr_value:
            return fail("qrValue é obrigatório.")
        result, message, ticket = check_in(qr_value=qr_value, staff_user=request.user)
        payload = {"result": result, "ticket": ticket.to_api() if ticket else None}
        if result == "ok":
            return ok(payload, message)
        return ok(payload, message)  # 200: o porteiro precisa sempre de ler a razão


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def payment_callback(request):
    """POST /back/payments/callback — chamado pelo gateway de pagamento."""
    reference = request.data.get("ticketId")
    try:
        ticket = Ticket.objects.select_related("price__event", "issued_to").get(pk=reference)
    except Ticket.DoesNotExist:
        return fail("Ticket not found", status.HTTP_404_NOT_FOUND)

    if request.data.get("status") != "succeeded":
        return ok(None, "Ignored")

    ticket = confirm_payment(
        ticket,
        provider=request.data.get("provider", "unknown"),
        provider_reference=request.data.get("providerReference", ""),
        payload=dict(request.data),
    )
    notify_partner(ticket)
    return ok(ticket.to_api(), "Payment confirmed")
