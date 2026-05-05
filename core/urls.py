"""
urls.py — BulkMed URL Routing
================================
Maps URL patterns to view functions.

How Django routing works:
  1. A request comes in for e.g. /pools/
  2. Django checks bulkmed/urls.py first (the root URLconf)
  3. Root URLconf includes this file via path('', include('core.urls'))
  4. Django matches /pools/ against the patterns below
  5. Calls the matching view function with the request

URL parameter types:
  <int:id>  — matches integers only (e.g. /delivery/1/confirm/)
  <uuid:id> — matches UUID strings (e.g. /pools/3bf62fcb-3264-.../
"""

from django.urls import path
from . import views

urlpatterns = [
    # ── Public / Auth ──────────────────────────────────────────────
    path('', views.login_redirect, name='home_redirect'),       # / → login for guests, dashboard for logged-in
    path('home/', views.home, name='home'),                     # landing page moved here
    path('go/', views.smart_redirect, name='smart_redirect'),   # post-login role router
    path('register/', views.register, name='register'),         # new user registration

    # ── Store Setup ────────────────────────────────────────────────
    path('setup/', views.store_setup, name='store_setup'),      # first-time store profile

    # ── Main App ───────────────────────────────────────────────────
    path('dashboard/', views.dashboard, name='dashboard'),
    path('wallet/', views.store_wallet, name='store_wallet'),

    # ── Factory Module ─────────────────────────────────────────────
    path('factory/', views.factory_dashboard, name='factory_dashboard'),
    path('factory/orders/', views.factory_order_list, name='factory_order_list'),
    path('factory/orders/<int:order_id>/', views.factory_order_detail, name='factory_order_detail'),
    path('factory/orders/<int:order_id>/accept/', views.factory_accept_order, name='factory_accept_order'),
    path('factory/orders/<int:order_id>/dispatch/', views.factory_dispatch_order, name='factory_dispatch_order'),
    path('factory/deliveries/', views.factory_deliveries, name='factory_deliveries'),
    path('factory/products/', views.factory_products, name='factory_products'),
    path('factory/wallet/', views.factory_wallet, name='factory_wallet'),
    path('factory/analytics/', views.factory_analytics, name='factory_analytics'),
    path('low-stock/', views.low_stock_list, name='low_stock_list'),
    path('ai-predictions/', views.ai_predictions, name='ai_predictions'),
    path('search/', views.search_medicines, name='search_medicines'),
    path('search/autocomplete/', views.medicine_autocomplete, name='medicine_autocomplete'),
    path('order-history/', views.order_history, name='order_history'),
    path('pools/', views.pool_list, name='pool_list'),          # browse open pools
    path('pools/<uuid:pool_id>/', views.pool_detail, name='pool_detail'),  # pool detail + join
    path('pools/<uuid:pool_id>/check-stock/', views.pool_check_stock, name='pool_check_stock'),
    path('pools/<uuid:pool_id>/join-after-payment/', views.pool_join_after_payment, name='pool_join_after_payment'),
    path('map/', views.map_view, name='map_view'),              # live pool map

    # ── Order Management ───────────────────────────────────────────
    path('order/<int:entry_id>/cancel/', views.cancel_order, name='cancel_order'),

    # ── Delivery ───────────────────────────────────────────────────
    path('delivery/<int:delivery_id>/confirm/', views.confirm_delivery, name='confirm_delivery'),
    path('delivery/<int:delivery_id>/track/', views.track_delivery, name='track_delivery'),

    # JSON API — called by JS to get truck GPS position and auto-deliver
    path('delivery/<int:delivery_id>/location/', views.delivery_location_api, name='delivery_location_api'),
    path('delivery/<int:delivery_id>/location/update/', views.update_truck_location, name='update_truck_location'),
    path('delivery/<int:delivery_id>/auto-deliver/', views.auto_deliver, name='auto_deliver'),

    # ── Driver PWA ─────────────────────────────────────────────────
    # Open on driver's phone: /driver/<id>/?token=<driver_token>
    path('driver/<int:delivery_id>/', views.driver_app, name='driver_app'),
    path('driver/manifest.json', views.pwa_manifest, name='pwa_manifest'),
    path('driver/sw.js', views.pwa_service_worker, name='pwa_service_worker'),

    # ── Master Admin API (Generic CRUD) ───────────────────────────────────────
    path('api/admin/<str:model_name>/', views.admin_crud, name='admin_crud_list'),
    path('api/admin/<str:model_name>/<str:pk>/', views.admin_crud, name='admin_crud_detail'),
    path('api/admin/<str:model_name>/<str:pk>/form/', views.admin_crud, {'action': 'form'}, name='admin_crud_form'),
    path('api/admin/<str:model_name>/<str:pk>/delete/', views.admin_crud, {'action': 'delete'}, name='admin_crud_delete'),
    path('api/admin/disputes/<str:pk>/resolve/', views.admin_crud, {'model_name': 'disputes', 'action': 'resolve'}, name='admin_crud_resolve'),

    # ── Control Panel (Staff Only) ─────────────────────────────────
    path('control/', views.control_panel, name='control_panel'),
    path('api/admin-stats/', views.admin_stats_api, name='admin_stats_api'),
    path('audit-trail/', views.financial_audit_trail, name='financial_audit_trail'),
    path('control/order-history/', views.admin_order_history, name='admin_order_history'),
    path('control/order-history/csv/', views.admin_order_history_csv, name='admin_order_history_csv'),
    path('control/verify-store/<int:store_id>/', views.verify_store, name='verify_store'),
    path('control/create-delivery/', views.create_delivery, name='create_delivery'),

    # AJAX endpoints for inline edit drawers (return JSON)
    path('control/pool/<uuid:pool_id>/edit/', views.edit_pool, name='edit_pool'),
    path('control/store/<int:store_id>/edit/', views.edit_store, name='edit_store'),
    path('control/store/<int:store_id>/delete/', views.delete_store, name='delete_store'),
    path('control/delivery/<int:delivery_id>/cancel/', views.cancel_delivery, name='cancel_delivery_admin'),
    path('control/delivery/<int:delivery_id>/delete/', views.delete_delivery, name='delete_delivery'),
    path('control/pool/create/', views.create_pool, name='create_pool'),
    path('control/pool/<uuid:pool_id>/delete/', views.delete_pool, name='delete_pool'),
    path('control/product/create/', views.manage_product, name='create_product'),
    path('control/product/<int:product_id>/edit/', views.manage_product, name='edit_product'),
    path('control/product/<int:product_id>/delete/', views.delete_product, name='delete_product'),
    path('control/lock-pools/', views.trigger_lock_pools, name='trigger_lock_pools'),
    path('control/order/<int:entry_id>/cancel/', views.cancel_order_admin, name='cancel_order_admin'),

    # Factory admin AJAX endpoints
    path('control/factory/<int:factory_id>/edit/', views.edit_factory, name='edit_factory'),
    path('control/factory/<int:factory_id>/delete/', views.delete_factory, name='delete_factory'),
    path('control/factory/<int:factory_id>/verify/', views.verify_factory, name='verify_factory'),
    path('control/factory-order/<int:order_id>/edit/', views.edit_factory_order, name='edit_factory_order'),
    path('control/factory-order/<int:order_id>/delete/', views.delete_factory_order, name='delete_factory_order'),
    path('control/factory-payout/<int:payout_id>/delete/', views.delete_factory_payout, name='delete_factory_payout'),
    path('control/factory-product/create/', views.manage_factory_product, name='create_factory_product'),
    path('control/factory-product/<int:product_id>/edit/', views.manage_factory_product, name='edit_factory_product'),
    path('control/factory-product/<int:product_id>/delete/', views.delete_product, name='delete_factory_product'),
    path('control/factory-delivery/<int:delivery_id>/cancel/', views.cancel_factory_delivery, name='cancel_factory_delivery'),
    path('control/factory-delivery/<int:delivery_id>/delete/', views.delete_delivery, name='delete_factory_delivery'),

    # GST Invoice download
    path('factory/orders/<int:order_id>/invoice/', views.download_invoice, name='download_invoice'),

    # Pool — Pay & Join API endpoints
    path('pools/<uuid:pool_id>/check-stock/', views.pool_check_stock, name='pool_check_stock'),
    path('pools/<uuid:pool_id>/join-after-payment/', views.pool_join_after_payment, name='pool_join_after_payment'),

    # Disputes
    path('delivery/<int:delivery_id>/dispute/', views.raise_dispute, name='raise_dispute'),
    path('control/dispute/<int:dispute_id>/resolve/', views.resolve_dispute, name='resolve_dispute'),
    path('factory/disputes/', views.factory_disputes, name='factory_disputes'),

    # Razorpay — store wallet top-up
    path('wallet/create-order/', views.razorpay_create_order, name='razorpay_create_order'),
    path('wallet/verify-payment/', views.razorpay_verify_payment, name='razorpay_verify_payment'),

    # Factory withdrawal
    path('factory/withdraw/', views.factory_withdraw, name='factory_withdraw'),
    path('control/withdrawal/<int:request_id>/action/', views.admin_withdrawal_action, name='admin_withdrawal_action'),
    path('control-panel/factories/', views.factory_list_view, name='factory_list'),

    # 3PL Logistics webhook — receives real-time tracking updates from providers
    # Auth: ?token=<LOGISTICS_WEBHOOK_TOKEN>  (set in settings.py / .env)
    path('webhooks/logistics/', views.logistics_webhook, name='logistics_webhook'),
]
