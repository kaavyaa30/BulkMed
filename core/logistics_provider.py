"""
core/logistics_provider.py — 3PL Logistics Provider Adapter
=============================================================
Provider-agnostic interface for Third-Party Logistics (3PL) APIs.

Supported providers (configured via settings.LOGISTICS_PROVIDER):
  'mock'       — Sandbox mode. Returns realistic fake data. No API calls.
                 Use this in development and testing.
  'delhivery'  — Delhivery API v1 (requires DELHIVERY_API_TOKEN in settings)
  'shadowfax'  — Shadowfax API (requires SHADOWFAX_CLIENT_ID + SECRET)

All providers expose the same interface:
  create_shipment(payload) → ShipmentResult
  track_shipment(waybill_id) → TrackingResult

Usage in views.py:
  from .logistics_provider import get_provider
  provider = get_provider()
  result = provider.create_shipment({...})
  if result.success:
      delivery.waybill_id = result.waybill_id
      ...

Adding a new provider:
  1. Subclass LogisticsProvider
  2. Implement create_shipment() and track_shipment()
  3. Register in PROVIDER_REGISTRY at the bottom of this file
  4. Set LOGISTICS_PROVIDER = 'your_key' in settings.py
"""

import logging
import random
import string
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class ShipmentResult:
    """
    Returned by create_shipment().

    success          : True if the shipment was created successfully
    waybill_id       : Provider's tracking/waybill number (e.g. "DEL1234567890")
    shipping_label_url: URL to download the PDF shipping label
    shipping_rate    : Cost charged by the provider in ₹ (Decimal)
    estimated_days   : Estimated delivery days (int)
    provider_name    : Human-readable provider name for audit trail
    error            : Error message if success=False
    raw_response     : Full provider API response dict (for debugging)
    """
    success:            bool
    waybill_id:         str             = ''
    shipping_label_url: str             = ''
    shipping_rate:      Decimal         = Decimal('0.00')
    estimated_days:     int             = 3
    provider_name:      str             = ''
    error:              str             = ''
    raw_response:       dict            = field(default_factory=dict)


@dataclass
class TrackingResult:
    """
    Returned by track_shipment().

    success      : True if tracking info was retrieved
    waybill_id   : The waybill number queried
    status       : Normalised status string:
                   'picked_up' | 'in_transit' | 'out_for_delivery' | 'delivered' | 'failed'
    status_detail: Human-readable status description from provider
    location     : Last known location string
    timestamp    : ISO timestamp of last update
    error        : Error message if success=False
    """
    success:       bool
    waybill_id:    str  = ''
    status:        str  = ''
    status_detail: str  = ''
    location:      str  = ''
    timestamp:     str  = ''
    error:         str  = ''


# ── Base class ────────────────────────────────────────────────────────────────

class LogisticsProvider:
    """
    Abstract base class. All providers must implement these two methods.

    Payload schema for create_shipment():
    {
        'origin': {
            'name':    str,   # factory name
            'address': str,   # full street address
            'city':    str,
            'pincode': str,
            'phone':   str,
        },
        'destination': {
            'name':    str,   # store name
            'address': str,
            'city':    str,
            'pincode': str,
            'phone':   str,
        },
        'package': {
            'description': str,   # medicine name
            'weight_kg':   float, # qty × per_unit_weight_kg
            'length_cm':   float,
            'width_cm':    float,
            'height_cm':   float,
            'value':       float, # declared value in ₹
        },
        'reference_id': str,  # our internal delivery ID
        'cod_amount':   float, # 0 for prepaid
    }
    """

    name = 'base'

    def create_shipment(self, payload: dict) -> ShipmentResult:
        raise NotImplementedError

    def track_shipment(self, waybill_id: str) -> TrackingResult:
        raise NotImplementedError


# ── Mock provider (sandbox / development) ────────────────────────────────────

class MockProvider(LogisticsProvider):
    """
    Sandbox provider — returns realistic fake data without any API calls.
    Safe to use in development, CI, and demo environments.

    Simulates:
    - Waybill ID generation
    - Shipping rate calculation (₹40 base + ₹8/kg)
    - Shipping label URL (points to a placeholder PDF)
    - Tracking status cycling
    """

    name = 'BulkMed Mock Logistics'

    def create_shipment(self, payload: dict) -> ShipmentResult:
        # Generate a realistic-looking waybill ID: BLK + 10 random digits
        suffix     = ''.join(random.choices(string.digits, k=10))
        waybill_id = f'BLK{suffix}'

        # Random shipping cost between ₹40 and ₹80 (inclusive, rounded to 2 dp)
        rate = Decimal(str(round(random.uniform(40, 80), 2)))

        logger.info(
            '[MockProvider] Shipment created: waybill=%s, rate=₹%s, ref=%s',
            waybill_id, rate, payload.get('reference_id'),
        )

        return ShipmentResult(
            success            = True,
            waybill_id         = waybill_id,
            shipping_label_url = f'https://mock.bulkmed.in/labels/{waybill_id}.pdf',
            shipping_rate      = rate,
            estimated_days     = 2,
            provider_name      = self.name,
            raw_response       = {'mock': True, 'waybill': waybill_id},
        )

    def track_shipment(self, waybill_id: str) -> TrackingResult:
        return TrackingResult(
            success       = True,
            waybill_id    = waybill_id,
            status        = 'in_transit',
            status_detail = 'Shipment in transit to destination city',
            location      = 'Mumbai Hub',
            timestamp     = '2026-05-01T10:00:00+05:30',
        )


# ── Delhivery provider ────────────────────────────────────────────────────────

class DelhiveryProvider(LogisticsProvider):
    """
    Delhivery API v1 integration.

    Required settings:
      DELHIVERY_API_TOKEN  — Bearer token from Delhivery dashboard
      DELHIVERY_WAREHOUSE_NAME — Your registered warehouse name in Delhivery

    API docs: https://developers.delhivery.com/
    """

    name = 'Delhivery'
    BASE_URL = 'https://track.delhivery.com'

    def __init__(self, token: str, warehouse_name: str):
        self.token          = token
        self.warehouse_name = warehouse_name

    def _headers(self) -> dict:
        return {
            'Authorization': f'Token {self.token}',
            'Content-Type':  'application/json',
        }

    def create_shipment(self, payload: dict) -> ShipmentResult:
        import requests

        dest = payload['destination']
        pkg  = payload['package']

        body = {
            'format': 'json',
            'data': {
                'shipments': [{
                    'name':          dest['name'],
                    'add':           dest['address'],
                    'city':          dest['city'],
                    'pin':           dest['pincode'],
                    'phone':         dest['phone'],
                    'order':         payload['reference_id'],
                    'payment_mode':  'Prepaid',
                    'cod_amount':    payload.get('cod_amount', 0),
                    'weight':        pkg['weight_kg'] * 1000,  # grams
                    'seller_name':   payload['origin']['name'],
                    'seller_add':    payload['origin']['address'],
                    'seller_city':   payload['origin']['city'],
                    'seller_pin':    payload['origin']['pincode'],
                    'seller_cntry':  'India',
                    'seller_inv':    payload['reference_id'],
                    'quantity':      1,
                    'shipment_width':  pkg.get('width_cm', 10),
                    'shipment_height': pkg.get('height_cm', 10),
                    'shipment_length': pkg.get('length_cm', 20),
                    'comment':       pkg['description'],
                    'total_amount':  pkg['value'],
                    'waybill':       '',  # auto-assign
                    'fragile_shipment': False,
                }],
                'pickup_location': {'name': self.warehouse_name},
            },
        }

        try:
            resp = requests.post(
                f'{self.BASE_URL}/api/cmu/create.json',
                headers=self._headers(),
                json=body,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            # Delhivery returns packages array
            pkg_data = data.get('packages', [{}])[0]
            waybill  = pkg_data.get('waybill', '')
            if not waybill:
                return ShipmentResult(
                    success=False,
                    error=f'Delhivery did not return a waybill: {data}',
                    provider_name=self.name,
                    raw_response=data,
                )

            return ShipmentResult(
                success            = True,
                waybill_id         = waybill,
                shipping_label_url = f'{self.BASE_URL}/api/p/packing_slip?wbns={waybill}&pdf=true',
                shipping_rate      = Decimal(str(pkg_data.get('cod_charges', 0) or 50)),
                estimated_days     = 3,
                provider_name      = self.name,
                raw_response       = data,
            )

        except Exception as exc:
            logger.error('[DelhiveryProvider] create_shipment failed: %s', exc)
            return ShipmentResult(
                success=False,
                error=str(exc),
                provider_name=self.name,
            )

    def track_shipment(self, waybill_id: str) -> TrackingResult:
        import requests

        # Normalise Delhivery status strings to our internal vocabulary
        STATUS_MAP = {
            'Manifested':          'picked_up',
            'In Transit':          'in_transit',
            'Out For Delivery':    'out_for_delivery',
            'Delivered':           'delivered',
            'RTO Initiated':       'failed',
            'RTO Delivered':       'failed',
        }

        try:
            resp = requests.get(
                f'{self.BASE_URL}/api/v1/packages/json/',
                headers=self._headers(),
                params={'waybill': waybill_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            shipment = data.get('ShipmentData', [{}])[0].get('Shipment', {})
            raw_status = shipment.get('Status', {}).get('Status', '')
            location   = shipment.get('Status', {}).get('City', '')
            timestamp  = shipment.get('Status', {}).get('StatusDateTime', '')

            return TrackingResult(
                success       = True,
                waybill_id    = waybill_id,
                status        = STATUS_MAP.get(raw_status, 'in_transit'),
                status_detail = raw_status,
                location      = location,
                timestamp     = timestamp,
            )

        except Exception as exc:
            logger.error('[DelhiveryProvider] track_shipment failed: %s', exc)
            return TrackingResult(success=False, waybill_id=waybill_id, error=str(exc))


# ── Shadowfax provider ────────────────────────────────────────────────────────

class ShadowfaxProvider(LogisticsProvider):
    """
    Shadowfax API integration (stub — implement when credentials are available).

    Required settings:
      SHADOWFAX_CLIENT_ID
      SHADOWFAX_CLIENT_SECRET

    API docs: https://developer.shadowfax.in/
    """

    name = 'Shadowfax'

    def __init__(self, client_id: str, client_secret: str):
        self.client_id     = client_id
        self.client_secret = client_secret

    def create_shipment(self, payload: dict) -> ShipmentResult:
        # TODO: implement when Shadowfax credentials are available
        return ShipmentResult(
            success=False,
            error='Shadowfax integration not yet configured. Set SHADOWFAX_CLIENT_ID and SHADOWFAX_CLIENT_SECRET.',
            provider_name=self.name,
        )

    def track_shipment(self, waybill_id: str) -> TrackingResult:
        return TrackingResult(
            success=False,
            waybill_id=waybill_id,
            error='Shadowfax tracking not yet configured.',
        )


# ── Provider registry & factory function ─────────────────────────────────────

def get_provider() -> LogisticsProvider:
    """
    Returns the configured logistics provider instance.

    Reads from Django settings:
      LOGISTICS_PROVIDER  — 'mock' | 'delhivery' | 'shadowfax'  (default: 'mock')

    Example settings.py:
      LOGISTICS_PROVIDER     = 'delhivery'
      DELHIVERY_API_TOKEN    = env('DELHIVERY_API_TOKEN')
      DELHIVERY_WAREHOUSE    = env('DELHIVERY_WAREHOUSE', default='BulkMed-WH1')
    """
    from django.conf import settings

    provider_key = getattr(settings, 'LOGISTICS_PROVIDER', 'mock').lower()

    if provider_key == 'delhivery':
        token    = getattr(settings, 'DELHIVERY_API_TOKEN', '')
        wh_name  = getattr(settings, 'DELHIVERY_WAREHOUSE', 'BulkMed-WH1')
        if not token:
            logger.warning(
                'LOGISTICS_PROVIDER=delhivery but DELHIVERY_API_TOKEN is not set. '
                'Falling back to mock provider.'
            )
            return MockProvider()
        return DelhiveryProvider(token=token, warehouse_name=wh_name)

    if provider_key == 'shadowfax':
        cid    = getattr(settings, 'SHADOWFAX_CLIENT_ID', '')
        secret = getattr(settings, 'SHADOWFAX_CLIENT_SECRET', '')
        if not cid or not secret:
            logger.warning(
                'LOGISTICS_PROVIDER=shadowfax but credentials are not set. '
                'Falling back to mock provider.'
            )
            return MockProvider()
        return ShadowfaxProvider(client_id=cid, client_secret=secret)

    # Default: mock / sandbox
    return MockProvider()


def build_shipment_payload(delivery, factory) -> dict:
    """
    Constructs the provider-agnostic shipment payload from a DeliveryTracking
    object and its Factory.

    Weight estimation: 0.1 kg per unit (100 g/strip — conservative default).
    Override by setting Product.weight_kg if you add that field later.
    """
    product    = delivery.pool.product
    store      = delivery.store
    qty        = delivery.quantity

    # Weight: 100 g per unit, minimum 0.1 kg
    per_unit_kg = getattr(product, 'weight_kg', 0.1)
    total_kg    = max(round(qty * per_unit_kg, 3), 0.1)

    # Declared value: full order value for insurance purposes
    from core.models import OrderEntry
    entry = OrderEntry.objects.filter(
        pool=delivery.pool, store=store
    ).exclude(status__in=('cancelled_free', 'cancelled_penalty')).first()
    declared_value = float(entry.total_amount()) if entry else float(product.base_price * qty)

    return {
        'origin': {
            'name':    factory.name,
            'address': factory.address or '',
            'city':    factory.city    or '',
            'pincode': getattr(factory, 'pincode', '380001'),
            'phone':   factory.contact or '',
        },
        'destination': {
            'name':    store.name,
            'address': store.address or '',
            'city':    store.city    or '',
            'pincode': getattr(store, 'pincode', '400001'),
            'phone':   store.contact or '',
        },
        'package': {
            'description': product.name,
            'weight_kg':   total_kg,
            'length_cm':   20.0,
            'width_cm':    15.0,
            'height_cm':   10.0,
            'value':       declared_value,
        },
        'reference_id': str(delivery.id),
        'cod_amount':   0,  # always prepaid on BulkMed
    }
