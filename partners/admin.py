from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import ApiKey, User


@admin.register(User)
class PartnerUserAdmin(UserAdmin):
    list_display = ("username", "email", "company_name", "is_staff")
    fieldsets = UserAdmin.fieldsets + (
        ("Parceiro", {"fields": ("company_name", "webhook_url", "webhook_secret")}),
    )


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("__str__", "owner", "label", "environment", "created_at",
                    "last_used_at", "revoked_at")
    list_filter = ("environment", "revoked_at")
    readonly_fields = ("key_hash", "prefix", "last_four", "created_at", "last_used_at")
