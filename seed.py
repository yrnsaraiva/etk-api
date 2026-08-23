"""Cria um organizador, um evento e uma chave de API. Imprime a chave."""
from datetime import timedelta
from django.utils import timezone
from partners.models import ApiKey, User
from catalog.models import Event, Price

org = User.objects.create_user("yara", email="org@moverunningclub.com", password="za4798wg5",
                               company_name="Move running club", is_staff=True)
ev = Event.objects.create(
    organizer=org, name="Mayora Experience",
    description="Corrida de 5 km seguida de after run.",
    category="social_run", date=timezone.now() + timedelta(days=30),
    image_url="https://example.com/lastwinter.jpeg",
    province="Beira", location_details="Mayora", status=Event.Status.PUBLISHED,
)
Price.objects.create(event=ev, name="Inscrição", amount=300, quantity_total=2)
key, raw = ApiKey.issue(org, label="site move running club")
print(f"EVENT_ID={ev.id}")
print(f"API_KEY={raw}")
