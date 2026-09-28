from django.contrib import admin

from .models import CheckInLog, PartnerDelivery, PaymentAttempt, Ticket
from .services import reclaim_and_confirm, refund


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("id", "phone", "full_name", "price", "payment", "entered", "created_at")
    list_filter = ("payment", "entered", "status", "provider")
    search_fields = ("id", "phone", "full_name", "email", "provider_charge_id")
    readonly_fields = ("id", "qr_value", "created_at", "updated_at")
    actions = ["confirmar_pagamento", "marcar_para_reembolso"]

    @admin.action(description="Confirmar pagamento (reserva a vaga e marca pago)")
    def confirmar_pagamento(self, request, queryset):
        confirmados, sem_vaga, ignorados = 0, 0, 0
        for ticket in queryset:
            if ticket.payment != Ticket.Payment.REVIEW:
                ignorados += 1
                continue
            ticket = reclaim_and_confirm(
                ticket, provider="admin", provider_reference=f"manual:{request.user}",
            )
            if ticket.payment == Ticket.Payment.PAID:
                confirmados += 1
            else:
                sem_vaga += 1
        self.message_user(
            request,
            f"{confirmados} confirmado(s), {sem_vaga} continuam sem vaga, "
            f"{ignorados} ignorado(s) (não estavam em revisão).",
        )

    @admin.action(description="Marcar para reembolso")
    def marcar_para_reembolso(self, request, queryset):
        reembolsados, ignorados = 0, 0
        for ticket in queryset:
            if ticket.payment not in (Ticket.Payment.PAID, Ticket.Payment.REVIEW):
                ignorados += 1
                continue
            ticket = refund(ticket)
            if ticket.payment == Ticket.Payment.REFUNDED:
                reembolsados += 1
            else:
                ignorados += 1
        self.message_user(
            request,
            f"{reembolsados} marcado(s) para reembolso, {ignorados} ignorado(s).",
        )


@admin.register(PaymentAttempt)
class PaymentAttemptAdmin(admin.ModelAdmin):
    list_display = ("ticket", "provider", "provider_reference", "amount", "succeeded", "created_at")
    list_filter = ("provider", "succeeded")


@admin.register(CheckInLog)
class CheckInLogAdmin(admin.ModelAdmin):
    list_display = ("ticket_id_raw", "result", "scanned_by", "scanned_at")
    list_filter = ("result",)


@admin.register(PartnerDelivery)
class PartnerDeliveryAdmin(admin.ModelAdmin):
    list_display = ("ticket", "event", "attempts", "next_attempt_at",
                    "delivered_at", "gave_up_at")
    list_filter = ("event", "delivered_at", "gave_up_at")
    search_fields = ("ticket__id",)
    readonly_fields = ("ticket", "event", "payload", "created_at")
