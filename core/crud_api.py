"""
crud_api.py — Generic CRUD API for the Master Admin
=====================================================
A single Class-Based View handles List, Retrieve, Update, and Delete
for any registered model, dispatched by model name in the URL.

URL pattern:
  /api/admin/<model>/                  GET  → list (search, filter, pagination)
  /api/admin/<model>/<pk>/             GET  → single record as JSON
  /api/admin/<model>/<pk>/form/        GET  → rendered ModelForm HTML (for modal)
  /api/admin/<model>/<pk>/             POST → update via ModelForm (returns errors or success)
  /api/admin/<model>/<pk>/delete/      POST → delete record
  /api/admin/disputes/<pk>/resolve/    POST → resolve dispute + wallet logic

All endpoints:
  - Require request.user.is_staff (403 otherwise)
  - Return JSON exclusively
  - Never raise HTML error pages
"""

import json
import logging
from decimal import Decimal

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views import View

logger = logging.getLogger(__name__)


# ── Model registry ────────────────────────────────────────────────────────────
# Maps URL slug → (ModelClass, serializer_fn, updater_fn)
# Serializer: model instance → dict
# Updater:    (instance, data_dict) → (instance, error_str|None)

def _get_registry():
    """Lazy import to avoid circular imports at module load time."""
    from .models import (
        Factory, MedicalStore, Product, Dispute,
        WalletTransaction, FactoryPayout, OrderPool,
        DeliveryTracking, OrderEntry,
    )
    from .forms import AdminFactoryForm, AdminStoreForm, AdminProductForm
    return {
        'factories':    (Factory,           _serialize_factory,     AdminFactoryForm),
        'stores':       (MedicalStore,      _serialize_store,       AdminStoreForm),
        'products':     (Product,           _serialize_product,     AdminProductForm),
        'disputes':     (Dispute,           _serialize_dispute,     None),   # update via /resolve/
        'transactions': (WalletTransaction, _serialize_transaction, None),   # immutable ledger
        'payouts':      (FactoryPayout,     _serialize_payout,      None),   # update via status change
        'pools':        (OrderPool,         _serialize_pool,        None),
        'deliveries':   (DeliveryTracking,  _serialize_delivery,    None),
    }


# ── Serializers ───────────────────────────────────────────────────────────────

def _serialize_factory(f):
    return {
        'id':             f.id,
        'name':           f.name,
        'city':           f.city or '',
        'address':        f.address or '',
        'license_no':     f.license_no or '',
        'contact':        f.contact or '',
        'gstin':          f.gstin or '',
        'wallet_balance': str(f.wallet_balance),
        'is_verified':    f.is_verified,
        'latitude':       str(f.latitude) if f.latitude else '',
        'longitude':      str(f.longitude) if f.longitude else '',
    }


def _serialize_store(s):
    return {
        'id':             s.id,
        'name':           s.name,
        'city':           s.city or '',
        'address':        s.address or '',
        'license_no':     s.license_no,
        'contact':        s.contact,
        'gstin':          s.gstin or '',
        'wallet_balance': str(s.wallet_balance),
        'is_verified':    s.is_verified,
        'latitude':       str(s.latitude) if s.latitude else '',
        'longitude':      str(s.longitude) if s.longitude else '',
    }


def _serialize_product(p):
    return {
        'id':           p.id,
        'name':         p.name,
        'generic_name': p.generic_name or '',
        'sku_code':     p.sku_code or '',
        'hsn_code':     p.hsn_code or '',
        'barcode':      p.barcode or '',
        'base_price':   str(p.base_price),
        'category':     p.category,
        'unit':         p.unit,
        'factory_id':   p.factory_id,
        'factory_name': p.factory.name if p.factory else '',
        'is_active':    p.is_active,
        'expiry_date':  str(p.expiry_date) if p.expiry_date else '',
    }


def _serialize_dispute(d):
    return {
        'id':           d.id,
        'store':        d.delivery.store.name,
        'product':      d.delivery.pool.product.name,
        'reason':       d.reason,
        'status':       d.status,
        'status_label': d.get_status_display(),
        'raised_at':    d.raised_at.strftime('%d %b %Y, %H:%M'),
        'resolved_at':  d.resolved_at.strftime('%d %b %Y, %H:%M') if d.resolved_at else None,
        'admin_note':   d.admin_note or '',
        'photo_url':    d.photo.url if d.photo else None,
        'delivery_id':  d.delivery_id,
    }


def _serialize_transaction(t):
    return {
        'id':               t.id,
        'transaction_type': t.transaction_type,
        'amount':           str(t.amount),
        'description':      t.description,
        'delivery_id':      t.delivery_id,
        'store':            t.delivery.store.name if t.delivery else '',
        'created_at':       t.created_at.strftime('%d %b %Y, %H:%M'),
    }


def _serialize_payout(p):
    return {
        'id':                 p.id,
        'factory':            p.factory.name,
        'product':            p.pool.product.name,
        'store':              p.delivery.store.name if p.delivery else '',
        'gross_amount':       str(p.gross_amount),
        'commission_deducted': str(p.commission_deducted),
        'net_payout':         str(p.net_payout),
        'status':             p.status,
        'created_at':         p.created_at.strftime('%d %b %Y, %H:%M'),
        'paid_at':            p.paid_at.strftime('%d %b %Y, %H:%M') if p.paid_at else None,
    }


def _serialize_pool(p):
    return {
        'id':                   str(p.id),
        'product':              p.product.name,
        'city':                 p.city,
        'status':               p.status,
        'current_member_count': p.current_member_count,
        'total_qty':            p.total_qty,
        'expires_at':           p.expires_at.strftime('%d %b %Y, %H:%M'),
    }


def _serialize_delivery(d):
    return {
        'id':           d.id,
        'store':        d.store.name,
        'product':      d.pool.product.name,
        'quantity':     d.quantity,
        'status':       d.status,
        'delivery_otp': d.delivery_otp,
        'otp_verified': d.otp_verified,
        'dispatched_at': d.dispatched_at.strftime('%d %b %Y, %H:%M') if d.dispatched_at else None,
        'delivered_at':  d.delivered_at.strftime('%d %b %Y, %H:%M') if d.delivered_at else None,
    }


# ── Updaters ──────────────────────────────────────────────────────────────────

def _update_factory(factory, data):
    factory.name           = data.get('name', factory.name).strip()
    factory.address        = data.get('address', factory.address).strip()
    factory.city           = data.get('city', factory.city).strip()
    factory.license_no     = data.get('license_no', factory.license_no) or None
    factory.contact        = data.get('contact', factory.contact).strip()
    factory.gstin          = data.get('gstin', factory.gstin or '').strip()
    factory.is_verified    = bool(data.get('is_verified', factory.is_verified))
    try:
        if data.get('wallet_balance') is not None:
            factory.wallet_balance = Decimal(str(data['wallet_balance']))
        if data.get('latitude'):
            factory.latitude = Decimal(str(data['latitude']))
        if data.get('longitude'):
            factory.longitude = Decimal(str(data['longitude']))
    except Exception:
        return factory, 'Invalid numeric value for wallet_balance, latitude, or longitude.'
    factory.save()
    return factory, None


def _update_store(store, data):
    store.name           = data.get('name', store.name).strip()
    store.address        = data.get('address', store.address).strip()
    store.city           = data.get('city', store.city).strip()
    store.contact        = data.get('contact', store.contact).strip()
    store.gstin          = data.get('gstin', store.gstin or '').strip()
    store.is_verified    = bool(data.get('is_verified', store.is_verified))
    try:
        if data.get('wallet_balance') is not None:
            store.wallet_balance = Decimal(str(data['wallet_balance']))
    except Exception:
        return store, 'Invalid wallet_balance value.'
    store.save()
    return store, None


def _update_product(product, data):
    product.name         = data.get('name', product.name).strip()
    product.generic_name = data.get('generic_name', product.generic_name or '').strip()
    product.sku_code     = data.get('sku_code', product.sku_code or '').strip()
    product.hsn_code     = data.get('hsn_code', product.hsn_code or '').strip()
    product.category     = data.get('category', product.category)
    product.unit         = data.get('unit', product.unit).strip()
    product.is_active    = bool(data.get('is_active', product.is_active))
    try:
        if data.get('base_price') is not None:
            product.base_price = Decimal(str(data['base_price']))
    except Exception:
        return product, 'Invalid base_price value.'
    if data.get('expiry_date'):
        from datetime import date
        try:
            product.expiry_date = date.fromisoformat(data['expiry_date'])
        except ValueError:
            return product, 'Invalid expiry_date format. Use YYYY-MM-DD.'
    product.save()
    return product, None


def _update_payout(payout, data):
    """Only status can be updated on a payout."""
    new_status = data.get('status', payout.status)
    if new_status not in ('pending', 'paid'):
        return payout, "Status must be 'pending' or 'paid'."
    if new_status == 'paid' and payout.status == 'pending':
        # Release payout to factory wallet
        from .models import PlatformWallet
        with transaction.atomic():
            payout.factory.wallet_balance += payout.net_payout
            payout.factory.save(update_fields=['wallet_balance'])
            payout.status  = 'paid'
            payout.paid_at = timezone.now()
            payout.save(update_fields=['status', 'paid_at'])
            PlatformWallet.get().transactions.create(
                amount           = payout.net_payout,
                transaction_type = 'debit',
                description      = (
                    f'Payout released via Master Admin — '
                    f'{payout.pool.product.name} → {payout.factory.name} '
                    f'(FactoryPayout #{payout.id})'
                ),
            )
    else:
        payout.status = new_status
        payout.save(update_fields=['status'])
    return payout, None


# ── Dispute resolution helper ─────────────────────────────────────────────────

def _resolve_dispute(dispute, action, note=''):
    """
    Executes the full financial resolution for a dispute.

    action='payout' → admin sides with factory:
        - Marks delivery as delivered
        - Calls _release_commission() → FactoryPayout(pending) created
        - WalletTransaction credits recorded

    action='refund' → admin sides with store:
        - Credits entry.total_amount() back to store.wallet_balance
        - Creates WalletTransaction debit on PlatformWallet
    """
    from .models import OrderEntry, PlatformWallet

    if dispute.status != 'open':
        return False, f'Dispute is already {dispute.get_status_display()}.'

    if action not in ('payout', 'refund'):
        return False, "action must be 'payout' or 'refund'."

    dispute.admin_note  = note
    dispute.resolved_at = timezone.now()
    delivery = dispute.delivery

    with transaction.atomic():
        if action == 'payout':
            dispute.status = 'resolved_payout'
            dispute.save()
            if not delivery.otp_verified:
                delivery.otp_verified = True
                delivery.status       = 'delivered'
                delivery.delivered_at = timezone.now()
                delivery.save(update_fields=['otp_verified', 'status', 'delivered_at'])
            # Import here to avoid circular import
            from .views import _release_commission
            _release_commission(delivery)

        elif action == 'refund':
            dispute.status = 'resolved_refund'
            dispute.save()
            entry = OrderEntry.objects.filter(
                pool=delivery.pool,
                store=delivery.store,
                status='active',
            ).first()
            if entry:
                refund_amount = entry.total_amount()
                store = delivery.store
                store.wallet_balance += refund_amount
                store.save(update_fields=['wallet_balance'])
                PlatformWallet.get().transactions.create(
                    amount           = refund_amount,
                    transaction_type = 'debit',
                    description      = (
                        f'Dispute refund via Master Admin — '
                        f'{delivery.pool.product.name} | {store.name} | '
                        f'Dispute #{dispute.id}'
                    ),
                )

    return True, None


# ── Generic CRUD View ─────────────────────────────────────────────────────────

class AdminCRUDView(LoginRequiredMixin, View):
    """
    Generic CRUD API for the Master Admin.

    Routing (all require is_staff):
      GET  /api/admin/<model>/              → paginated list
      GET  /api/admin/<model>/<pk>/         → single record
      POST /api/admin/<model>/<pk>/         → update record
      POST /api/admin/<model>/<pk>/delete/  → delete record
      POST /api/admin/disputes/<pk>/resolve/ → resolve + wallet logic
    """

    # ── Auth guard ────────────────────────────────────────────────────────────
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'error': 'Authentication required.'}, status=401)
        if not request.user.is_staff:
            return JsonResponse({'error': 'Staff access required.'}, status=403)
        return super().dispatch(request, *args, **kwargs)

    # ── GET: list, single record, or form HTML ───────────────────────────────
    def get(self, request, model_name, pk=None, action=None):
        registry = _get_registry()
        if model_name not in registry:
            return JsonResponse({'error': f'Unknown model: {model_name}'}, status=404)

        ModelClass, serializer, FormClass = registry[model_name]

        # ── Render form HTML for the modal ────────────────────────────────────
        if action == 'form':
            if pk is None:
                return JsonResponse({'error': 'pk required for form.'}, status=400)
            if FormClass is None:
                return JsonResponse({'error': f'{model_name} has no editable form.'}, status=405)
            pk = self._coerce_pk(ModelClass, pk)
            try:
                obj = ModelClass.objects.get(pk=pk)
            except (ModelClass.DoesNotExist, ValueError):
                return JsonResponse({'error': 'Record not found.'}, status=404)
            form = FormClass(instance=obj)
            html = self._render_form_html(form, model_name, pk)
            return JsonResponse({'ok': True, 'html': html, 'title': f'Edit {model_name[:-1].title()} #{pk}'})

        # ── Single record as JSON ─────────────────────────────────────────────
        if pk is not None:
            pk = self._coerce_pk(ModelClass, pk)
            try:
                obj = ModelClass.objects.get(pk=pk)
            except (ModelClass.DoesNotExist, ValueError):
                return JsonResponse({'error': 'Record not found.'}, status=404)
            return JsonResponse({'ok': True, 'data': serializer(obj)})

        # List with search, status filter, pagination
        from django.db.models import Q
        qs = ModelClass.objects.all()

        # Apply select_related for common FK patterns
        qs = self._apply_select_related(model_name, qs)

        # Search
        q = request.GET.get('q', '').strip()
        if q:
            qs = self._apply_search(model_name, qs, q)

        # Status filter
        status = request.GET.get('status', '')
        if status:
            qs = self._apply_status_filter(model_name, qs, status)

        # Ordering
        qs = self._apply_ordering(model_name, qs)

        total = qs.count()
        page     = max(1, int(request.GET.get('page', 1)))
        per_page = min(100, int(request.GET.get('per_page', 20)))
        start    = (page - 1) * per_page
        qs       = qs[start : start + per_page]

        return JsonResponse({
            'ok':         True,
            'model':      model_name,
            'total':      total,
            'page':       page,
            'per_page':   per_page,
            'total_pages': max(1, (total + per_page - 1) // per_page),
            'data':       [serializer(obj) for obj in qs],
        })

    # ── POST: update, delete, or special action ───────────────────────────────
    def post(self, request, model_name, pk=None, action=None):
        registry = _get_registry()
        if model_name not in registry:
            return JsonResponse({'error': f'Unknown model: {model_name}'}, status=404)

        ModelClass, serializer, FormClass = registry[model_name]

        # ── Special: dispute resolution ───────────────────────────────────────
        if model_name == 'disputes' and action == 'resolve':
            if pk is None:
                return JsonResponse({'error': 'pk required for resolve.'}, status=400)
            pk = self._coerce_pk(ModelClass, pk)
            try:
                dispute = ModelClass.objects.select_related(
                    'delivery__store', 'delivery__pool__product'
                ).get(pk=pk)
            except ModelClass.DoesNotExist:
                return JsonResponse({'error': 'Dispute not found.'}, status=404)
            try:
                data           = json.loads(request.body)
                resolve_action = data.get('action', '')
                note           = data.get('note', '').strip()
            except (json.JSONDecodeError, KeyError):
                return JsonResponse({'error': 'Invalid JSON body.'}, status=400)
            ok, err = _resolve_dispute(dispute, resolve_action, note)
            if not ok:
                return JsonResponse({'ok': False, 'error': err}, status=400)
            dispute.refresh_from_db()
            return JsonResponse({
                'ok':      True,
                'message': f'Dispute #{pk} resolved — {resolve_action}.',
                'data':    serializer(dispute),
            })

        # ── Delete ────────────────────────────────────────────────────────────
        if action == 'delete':
            if pk is None:
                return JsonResponse({'error': 'pk required for delete.'}, status=400)
            pk = self._coerce_pk(ModelClass, pk)
            try:
                obj = ModelClass.objects.get(pk=pk)
            except (ModelClass.DoesNotExist, ValueError):
                return JsonResponse({'error': 'Record not found.'}, status=404)
            label = str(obj)
            try:
                obj.delete()
            except Exception as e:
                return JsonResponse({'ok': False, 'error': f'Delete failed: {e}'}, status=400)
            logger.info(f'Master Admin: {request.user} deleted {model_name} #{pk} ({label})')
            return JsonResponse({'ok': True, 'message': f'Deleted: {label}'})

        # ── Update via ModelForm (with real validation) ───────────────────────
        if pk is None:
            return JsonResponse({'error': 'pk required for update.'}, status=400)
        if FormClass is None:
            # Payouts: handle status-only update
            if model_name == 'payouts':
                pk = self._coerce_pk(ModelClass, pk)
                try:
                    obj = ModelClass.objects.get(pk=pk)
                except (ModelClass.DoesNotExist, ValueError):
                    return JsonResponse({'error': 'Record not found.'}, status=404)
                try:
                    data = json.loads(request.body)
                except json.JSONDecodeError:
                    return JsonResponse({'error': 'Invalid JSON body.'}, status=400)
                obj, err = _update_payout(obj, data)
                if err:
                    return JsonResponse({'ok': False, 'error': err}, status=400)
                return JsonResponse({'ok': True, 'message': 'Payout updated.', 'data': serializer(obj)})
            return JsonResponse({'error': f'{model_name} is read-only via this API.'}, status=405)

        pk = self._coerce_pk(ModelClass, pk)
        try:
            obj = ModelClass.objects.get(pk=pk)
        except (ModelClass.DoesNotExist, ValueError):
            return JsonResponse({'error': 'Record not found.'}, status=404)

        # Bind the ModelForm to POST data
        form = FormClass(request.POST, instance=obj)

        if form.is_valid():
            saved = form.save()
            logger.info(f'Master Admin: {request.user} updated {model_name} #{pk}')
            return JsonResponse({
                'ok':      True,
                'message': f'{model_name[:-1].title()} updated successfully.',
                'data':    serializer(saved),
            })
        else:
            # Return field-level errors so the frontend can highlight them
            errors = {field: list(errs) for field, errs in form.errors.items()}
            # Re-render the form with errors for the modal
            html = self._render_form_html(form, model_name, pk)
            return JsonResponse({
                'ok':     False,
                'errors': errors,
                'html':   html,   # re-rendered form with inline error messages
            }, status=400)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _apply_select_related(self, model_name, qs):
        sr = {
            'factories':    [],
            'stores':       [],
            'products':     ['factory'],
            'disputes':     ['delivery__store', 'delivery__pool__product'],
            'transactions': ['delivery__store', 'delivery__pool__product'],
            'payouts':      ['factory', 'pool__product', 'delivery__store'],
            'pools':        ['product'],
            'deliveries':   ['store', 'pool__product'],
        }
        fields = sr.get(model_name, [])
        return qs.select_related(*fields) if fields else qs

    def _apply_search(self, model_name, qs, q):
        from django.db.models import Q
        filters = {
            'factories':    Q(name__icontains=q) | Q(city__icontains=q) | Q(license_no__icontains=q),
            'stores':       Q(name__icontains=q) | Q(city__icontains=q) | Q(license_no__icontains=q),
            'products':     Q(name__icontains=q) | Q(generic_name__icontains=q) | Q(sku_code__icontains=q),
            'disputes':     Q(delivery__store__name__icontains=q) | Q(delivery__pool__product__name__icontains=q) | Q(reason__icontains=q),
            'transactions': Q(description__icontains=q),
            'payouts':      Q(factory__name__icontains=q) | Q(pool__product__name__icontains=q),
            'pools':        Q(product__name__icontains=q) | Q(city__icontains=q),
            'deliveries':   Q(store__name__icontains=q) | Q(pool__product__name__icontains=q),
        }
        f = filters.get(model_name)
        return qs.filter(f) if f else qs

    def _apply_status_filter(self, model_name, qs, status):
        field_map = {
            'factories':    ('is_verified', {'verified': True, 'unverified': False}),
            'stores':       ('is_verified', {'verified': True, 'unverified': False}),
            'products':     ('is_active',   {'active': True, 'inactive': False}),
            'disputes':     ('status',      None),
            'transactions': ('transaction_type', None),
            'payouts':      ('status',      None),
            'pools':        ('status',      None),
            'deliveries':   ('status',      None),
        }
        if model_name not in field_map:
            return qs
        field, mapping = field_map[model_name]
        value = mapping.get(status, status) if mapping else status
        return qs.filter(**{field: value})

    def _apply_ordering(self, model_name, qs):
        ordering = {
            'factories':    ['-created_at'],
            'stores':       ['city', 'name'],
            'products':     ['name'],
            'disputes':     ['-raised_at'],
            'transactions': ['-created_at'],
            'payouts':      ['-created_at'],
            'pools':        ['-created_at'],
            'deliveries':   ['-dispatched_at'],
        }
        return qs.order_by(*ordering.get(model_name, ['-id']))

    @staticmethod
    def _coerce_pk(ModelClass, pk):
        """
        Convert pk string to the correct type for the model's primary key.
        Most models use integer PKs; OrderPool uses UUID.
        """
        import uuid
        pk_field = ModelClass._meta.pk
        if pk_field and pk_field.get_internal_type() == 'UUIDField':
            try:
                return uuid.UUID(str(pk))
            except ValueError:
                return pk
        try:
            return int(pk)
        except (ValueError, TypeError):
            return pk

    @staticmethod
    def _render_form_html(form, model_name, pk):
        """
        Renders a ModelForm as dark-themed HTML for the admin modal.
        Each field gets a label, input, and inline error message.
        """
        parts = []
        for name, field in form.fields.items():
            bf        = form[name]
            errors    = bf.errors
            has_error = bool(errors)
            label     = field.label or name.replace('_', ' ').title()

            # Checkbox fields get a special layout
            from django import forms as dj_forms
            if isinstance(field.widget, dj_forms.CheckboxInput):
                parts.append(f'''
                <div class="modal-field-group" style="display:flex;align-items:center;gap:10px;margin-bottom:16px;">
                  {bf.as_widget()}
                  <label for="{bf.id_for_label}" style="color:#cbd5e1;font-size:.875rem;font-weight:600;margin:0;">{label}</label>
                  {"".join(f'<div class="modal-field-error">{e}</div>' for e in errors)}
                </div>''')
            else:
                border = '#dc2626' if has_error else '#334155'
                parts.append(f'''
                <div class="modal-field-group" style="margin-bottom:16px;">
                  <label for="{bf.id_for_label}" style="display:block;font-size:.72rem;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px;">{label}</label>
                  <div style="border:1px solid {border};border-radius:8px;overflow:hidden;">
                    {bf.as_widget()}
                  </div>
                  {"".join(f'<div class="modal-field-error" style="color:#f87171;font-size:.75rem;margin-top:4px;">{e}</div>' for e in errors)}
                </div>''')

        return '\n'.join(parts)
