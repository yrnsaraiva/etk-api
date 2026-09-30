from django.contrib import admin

from .models import Event, Price


class PriceInline(admin.TabularInline):
    model = Price
    extra = 1
    readonly_fields = ("id", "quantity_reserved")


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "organizer", "date", "status", "registration_mode",
                    "total_tickets_purchased")
    list_filter = ("status", "registration_mode", "category", "province")
    search_fields = ("id", "name")
    inlines = [PriceInline]
