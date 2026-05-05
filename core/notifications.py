"""
notifications.py — Push real-time notifications via Django Channels
=====================================================================
Usage from any Django view or Celery task:

    from core.notifications import push_notification

    push_notification(
        user_id = store.user_id,
        notif_type = 'order_accepted',
        title = 'Order Accepted',
        body  = f'Your order for {product_name} is now being processed.',
        url   = '/order-history/',   # optional — makes the toast clickable
    )

Notification types (used by the frontend to pick icon + colour):
    order_accepted   — factory accepted the order
    order_dispatched — factory dispatched the order
    delivery_otp     — delivery arrived, OTP ready
    dispute_resolved — admin resolved a dispute
    payout_released  — factory payout released
    pool_locked      — pool has locked (order confirmed)
    info             — generic informational
    warning          — generic warning
    error            — generic error
"""

from datetime import datetime, timezone as dt_tz
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


def push_notification(
    user_id: int,
    notif_type: str,
    title: str,
    body: str,
    url: str = '',
) -> None:
    """
    Send a real-time notification to a specific user's WebSocket channel.

    This function is synchronous and safe to call from any Django view,
    signal, or Celery task. It uses async_to_sync to bridge into the
    async channel layer.

    Args:
        user_id:    The Django User pk of the recipient.
        notif_type: Notification category string (drives frontend icon/colour).
        title:      Short heading shown in the toast.
        body:       Longer description shown below the title.
        url:        Optional URL — clicking the toast navigates here.
    """
    channel_layer = get_channel_layer()
    group_name    = f'notifications_{user_id}'

    async_to_sync(channel_layer.group_send)(
        group_name,
        {
            # 'type' maps to the consumer method: send_notification()
            'type':       'send_notification',
            # Payload forwarded to the browser:
            'notif_type': notif_type,
            'title':      title,
            'body':       body,
            'url':        url,
            'ts':         datetime.now(dt_tz.utc).isoformat(),
        }
    )
