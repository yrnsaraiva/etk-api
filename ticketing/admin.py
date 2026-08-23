from django.contrib import admin

from .models import CheckInLog, PaymentAttempt, Ticket


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("id", "phone", "full_name", "price", "payment", "entered", "created_at")
    list_filter = ("payment", "entered", "status", "provider")
    search_fields = ("id", "phone", "full_name", "email", "provider_charge_id")
    readonly_fields = ("id", "qr_value", "created_at", "updated_at")


@admin.register(PaymentAttempt)
class PaymentAttemptAdmin(admin.ModelAdmin):
    list_display = ("ticket", "provider", "provider_reference", "amount", "succeeded", "created_at")
    list_filter = ("provider", "succeeded")


@admin.register(CheckInLog)
class CheckInLogAdmin(admin.ModelAdmin):
    list_display = ("ticket_id_raw", "result", "scanned_by", "scanned_at")
    list_filter = ("result",)
