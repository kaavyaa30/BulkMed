"""
asgi.py — ASGI entry point for BulkMed
========================================
Routes:
  HTTP  → Django's standard ASGI application (views, admin, etc.)
  WS    → Django Channels router  (core/routing.py)

Run with Daphne (production):
  daphne -b 0.0.0.0 -p 8000 bulkmed.asgi:application

Run with Uvicorn (dev):
  uvicorn bulkmed.asgi:application --reload
"""

import os
import django
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bulkmed.settings')

# Django must be fully set up before importing Channels routing
# (models, apps, etc. need to be ready)
django.setup()

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from core.routing import websocket_urlpatterns

application = ProtocolTypeRouter({
    # Standard Django HTTP — views, admin, DRF, static files
    'http': get_asgi_application(),

    # WebSocket — wrapped in AuthMiddlewareStack so request.user is populated
    # from the Django session cookie, exactly like a normal HTTP request.
    'websocket': AuthMiddlewareStack(
        URLRouter(websocket_urlpatterns)
    ),
})
