"""API de gestão — o organizador cria eventos e lotes (JWT, não chave de API)."""

from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from partners.models import ApiKey

from .models import Event, Price


class PriceSerializer(serializers.ModelSerializer):
    available = serializers.IntegerField(read_only=True)

    class Meta:
        model = Price
        fields = ("id", "event", "name", "amount", "currency", "status", "quantity_total",
                  "quantity_reserved", "available", "max_per_request", "sales_start", "sales_end")
        read_only_fields = ("id", "quantity_reserved", "available")

    def validate_event(self, event):
        if event.organizer_id != self.context["request"].user.pk:
            raise serializers.ValidationError("Não é o organizador deste evento.")
        return event

    def validate(self, attrs):
        if self.instance and "quantity_total" in attrs:
            if attrs["quantity_total"] < self.instance.quantity_reserved:
                raise serializers.ValidationError(
                    f"Já foram emitidos {self.instance.quantity_reserved} bilhetes."
                )
        return attrs


class InviteCreateSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1, max_value=500, default=1)
    holderName = serializers.CharField(required=False, allow_blank=True, max_length=200)
    holderEmail = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=20)
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)


class EventSerializer(serializers.ModelSerializer):
    prices = PriceSerializer(many=True, read_only=True)
    total_tickets_purchased = serializers.IntegerField(read_only=True)

    class Meta:
        model = Event
        fields = ("id", "name", "description", "category", "date", "image_url", "province",
                  "location_details", "status", "prices", "total_tickets_purchased", "created_at")
        read_only_fields = ("id", "created_at")


class EventViewSet(viewsets.ModelViewSet):
    serializer_class = EventSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["status", "category", "province"]

    def get_queryset(self):
        return Event.objects.filter(organizer=self.request.user).prefetch_related("prices")

    def perform_create(self, serializer):
        serializer.save(organizer=self.request.user)

    @action(detail=True, methods=["get"])
    def tickets(self, request, pk=None):
        """Lista de participantes do evento — para o dashboard do organizador."""
        from ticketing.models import Ticket
        qs = Ticket.objects.filter(price__event=self.get_object()).select_related("price")
        return Response({
            "count": qs.count(),
            "paid": qs.filter(payment=Ticket.Payment.PAID).count(),
            "invited": qs.filter(payment=Ticket.Payment.INVITED).count(),
            "entered": qs.filter(entered=True).count(),
            "results": [t.to_api() for t in qs[:200]],
        })


class PriceViewSet(viewsets.ModelViewSet):
    serializer_class = PriceSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["event", "status"]

    def get_queryset(self):
        return Price.objects.filter(event__organizer=self.request.user).select_related("event")

    @action(detail=True, methods=["post"])
    def invites(self, request, pk=None):
        """POST /api/prices/{id}/invites/ — emite bilhetes gratuitos deste lote.

        Não passa pelo gateway de pagamento; ocupa vaga tal como um bilhete
        pago, para a capacidade do lote continuar a ser respeitada.
        """
        from ticketing.services import TicketError, issue_invites

        price = self.get_object()  # já filtrado por organizador em get_queryset
        s = InviteCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        try:
            tickets = issue_invites(
                price_id=price.id, event_id=price.event_id, organizer=request.user,
                quantity=data["quantity"], holder_name=data.get("holderName", ""),
                holder_email=data.get("holderEmail", ""), phone=data.get("phone", ""),
                note=data.get("note", ""),
            )
        except TicketError as exc:
            return Response({"detail": str(exc)}, status=400)

        return Response([t.to_api() for t in tickets], status=201)


class ApiKeySerializer(serializers.ModelSerializer):
    class Meta:
        model = ApiKey
        fields = ("id", "label", "environment", "prefix", "last_four",
                  "created_at", "last_used_at", "revoked_at")
        read_only_fields = fields


class ApiKeyViewSet(viewsets.ModelViewSet):
    """O valor em claro da chave aparece uma única vez: na resposta ao POST."""

    serializer_class = ApiKeySerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "delete"]

    def get_queryset(self):
        return ApiKey.objects.filter(owner=self.request.user)

    def create(self, request, *args, **kwargs):
        key, raw = ApiKey.issue(
            owner=request.user,
            label=request.data.get("label", ""),
            environment=request.data.get("environment", ApiKey.Environment.LIVE),
        )
        data = ApiKeySerializer(key).data
        data["key"] = raw
        data["warning"] = "Guarde esta chave agora. Não voltará a ser mostrada."
        return Response(data, status=201)

    def perform_destroy(self, instance):
        instance.revoke()
