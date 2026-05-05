"""
routing.py — WebSocket URL patterns for Django Channels
=========================================================
Mounted in bulkmed/asgi.py under the 'websocket' protocol router.

URL: ws://<host>/ws/notifications/
"""

from django.urls import path
from .consumers import NotificationConsumer

websocket_urlpatterns = [
    path('ws/notifications/', NotificationConsumer.as_asgi()),
]
