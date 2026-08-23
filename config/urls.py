from django.contrib import admin
from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from catalog.views import ApiKeyViewSet, EventViewSet, PriceViewSet
from payments.views import debitopay_webhook
from ticketing.views import (
    ExternalCheckInView,
    ExternalEventDetailView,
    ExternalEventListView,
    ExternalTicketCreateView,
    ExternalTicketDetailView,
    payment_callback,
)

router = DefaultRouter()
router.register("events", EventViewSet, basename="event")
router.register("prices", PriceViewSet, basename="price")
router.register("api-keys", ApiKeyViewSet, basename="apikey")

EXTERNAL = "back/borrow/external"

urlpatterns = [
    path("admin/", admin.site.urls),

    # --- API externa: consumida pelos sites parceiros com etk_live_... ---
    path(f"{EXTERNAL}/events", ExternalEventListView.as_view()),
    path(f"{EXTERNAL}/events/<str:event_id>", ExternalEventDetailView.as_view()),
    path(f"{EXTERNAL}/tickets", ExternalTicketCreateView.as_view()),
    path(f"{EXTERNAL}/tickets/check-in", ExternalCheckInView.as_view()),
    path(f"{EXTERNAL}/tickets/<str:ticket_id>", ExternalTicketDetailView.as_view()),
    path("back/payments/callback", payment_callback),
    path("back/payments/webhooks/debitopay", debitopay_webhook),

    # --- API de gestão: o organizador, autenticado com JWT ---
    path("api/auth/token/", TokenObtainPairView.as_view()),
    path("api/auth/token/refresh/", TokenRefreshView.as_view()),
    path("api/", include(router.urls)),
]
