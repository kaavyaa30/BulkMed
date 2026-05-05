"""
consumers.py — BulkMed WebSocket Consumers
============================================
Handles real-time push notifications to authenticated users.

Channel naming convention:
  notifications_<user_id>

This means every logged-in user has their own private channel group.
Any Django view or Celery task can push a message to a specific user
by calling:

    from core.notifications import push_notification
    push_notification(user_id, type='order_accepted', title='...', body='...')

The frontend JS connects to:
    ws://<host>/ws/notifications/

and receives JSON messages of the shape:
    {
        "type":    "order_accepted",   # drives the toast icon/colour
        "title":   "Order Accepted",
        "body":    "Your order for Dolo 650 is now being processed.",
        "url":     "/order-history/",  # optional — makes toast clickable
        "ts":      "2026-04-27T10:30:00"
    }
"""

import json
from channels.generic.websocket import AsyncWebsocketConsumer


class NotificationConsumer(AsyncWebsocketConsumer):
    """
    One persistent WebSocket connection per browser tab per logged-in user.

    connect()    — joins the user's private group
    disconnect() — leaves the group
    receive()    — not used (server-push only; clients don't send messages here)
    notify()     — called by the channel layer when a message is dispatched
    """

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self):
        user = self.scope['user']

        # Reject unauthenticated connections immediately
        if not user.is_authenticated:
            await self.close(code=4001)
            return

        # Each user gets a private group: "notifications_42"
        self.group_name = f'notifications_{user.id}'

        # Join the group so broadcast messages reach this socket
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # Send a silent "connected" ack so the frontend knows the socket is live
        await self.send(text_data=json.dumps({
            'type':  'connected',
            'title': 'Connected',
            'body':  'Real-time notifications active.',
        }))

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        # Clients don't send messages — this is a server-push-only channel.
        # Silently ignore anything received.
        pass

    # ── Message handler ───────────────────────────────────────────────────
    # The method name MUST match the "type" field sent via group_send,
    # with dots replaced by underscores.
    # We use a single handler "send_notification" for all notification types.

    async def send_notification(self, event):
        """
        Forwards a notification event from the channel layer to the WebSocket.
        Called automatically when group_send(type='send_notification') is used.
        """
        # Strip the internal 'type' key; the payload already has its own 'type'
        payload = {k: v for k, v in event.items() if k != 'type'}
        await self.send(text_data=json.dumps(payload))
