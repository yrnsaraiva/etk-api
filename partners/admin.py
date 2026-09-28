from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import ApiKey, User


@admin.register(User)
class PartnerUserAdmin(UserAdmin):
    list_display = ("username", "email", "company_name", "is_staff")
    fieldsets = UserAdmin.fieldsets + (
        ("Parceiro", {
            "fields": ("company_name", "webhook_url", "webhook_secret"),
            "description": (
                "webhook_secret é gerado automaticamente ao gravar um webhook_url "
                "vazio de segredo — não é editável aqui. Mostre-o ao parceiro uma "
                "única vez: ele valida cada aviso recalculando HMAC-SHA256 (em "
                "hexadecimal) sobre o corpo cru do pedido com este segredo, e "
                "comparando com o cabeçalho X-ETK-Signature."
            ),
        }),
    )
    readonly_fields = UserAdmin.readonly_fields + ("webhook_secret",)


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("__str__", "owner", "label", "environment", "created_at",
                    "last_used_at", "revoked_at")
    list_filter = ("environment", "revoked_at")
    readonly_fields = ("key_hash", "prefix", "last_four", "created_at", "last_used_at")
