# 💊 BulkMed — B2B Pharmaceutical Group-Buying Platform
A full-stack Django platform that lets independent pharmacies pool their orders together to buy medicines directly from factories at bulk prices — cutting out middlemen and reducing costs.

---

## What It Does
Small pharmacies can't afford factory-direct pricing alone. BulkMed lets multiple stores join a **Group-Buying Pool** for the same medicine. As more stores join, the discount increases for everyone. When the pool closes, a consolidated order goes to the factory and each store gets their share delivered.

**Discount tiers:** 1–4 stores = 1% off · 5–9 stores = 5% off · 10–15 stores = 10% off

---

## Key Features

### For Medical Stores
- Browse open pools by medicine and city
- Join a pool with a **10% escrow advance** via Razorpay (pay the rest on delivery)
- Live GPS tracking of the delivery truck
- Enter a **6-digit OTP** to confirm delivery and release payment
- AI-powered low-stock alerts with seasonal demand predictions
- Full wallet and transaction history

### For Factories
- See consolidated orders from multiple stores in one place
- Accept → Process → Dispatch workflow
- Choose **Local Delivery** (driver GPS app) or **3PL Partner** (Delhivery/Shadowfax)
- Auto-generated **GST invoice PDF** on every dispatch
- Earnings wallet with 85% payout per confirmed delivery
- Withdrawal requests to cash out earnings

### For Superadmin
- Full control panel: manage stores, products, pools, deliveries, orders
- Financial audit trail showing every rupee movement with calculation proof
- Dispute resolution system (block/release factory payouts)
- Real-time platform wallet with commission tracking
- Factory directory with username mapping

---

## How the Money Works

```
Order Value (₹1,000)
├── Platform Commission (15%) = ₹150  → BulkMed keeps this
└── Factory Payout (85%)      = ₹850  → Factory receives this
    └── If 3PL used: shipping cost deducted from the ₹850
```

Stores pay via Razorpay. The 2% gateway fee + 18% GST on that fee is shown transparently before payment. Only the base amount is credited to the store's wallet.

---

## Automation (Celery Background Tasks)

| Task | Schedule | What it does |
|---|---|---|
| Lock expired pools | Every hour | Locks pools, assigns nearest factory, creates deliveries |
| Ensure pool coverage | Every hour | Makes sure every medicine always has an open pool |
| AI demand predictions | Daily midnight | Alerts stores about seasonal stock-out risks |
| Low-stock notifications | Every 4 hours | Pushes alerts when inventory hits threshold |
| Auto-release payouts | Every 6 hours | Pays factory after 48h dispute-free window |
| Disable expiring products | Daily 1 AM | Auto-hides products near expiry date |

---

## Driver PWA

Drivers open a token-authenticated URL on their phone (no login needed):
```
/driver/<delivery_id>/?token=<driver_token>
```
- Live GPS map with OpenStreetMap tiles
- Pings server every 10 seconds with location
- Shows OTP to hand to the store owner
- Works offline via Service Worker

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Django 5.2, Python 3.11 |
| Real-time | Django Channels 4.1, WebSockets, Daphne |
| Background tasks | Celery 5.3, Redis, django-celery-beat |
| Payments | Razorpay 1.4 |
| PDF invoices | ReportLab 4.0 |
| Maps | Leaflet.js + OpenStreetMap |
| Database | SQLite (dev) / PostgreSQL (prod) |
| Frontend | Bootstrap 5.3, Bootstrap Icons |

---

## Setup & Run

```bash
# 1. Clone and create virtual environment
python -m venv env
env\Scripts\activate        # Windows
source env/bin/activate     # Mac/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
# Edit .env with your keys (Razorpay, DB, etc.)

# 4. Run migrations
python manage.py migrate

# 5. Create superuser
python manage.py createsuperuser

# 6. Seed initial pools (run once)
python manage.py ensure_pool_coverage --commit

# 7. Start the server
python manage.py runserver
```

**To run background tasks (optional for full functionality):**
```bash
celery -A bulkmed worker --loglevel=info
celery -A bulkmed beat --loglevel=info
```

---

## Key URLs

| URL | Who sees it |
|---|---|
| `/` | Public landing page |
| `/dashboard/` | Store owner dashboard |
| `/pools/` | Browse open pools |
| `/wallet/` | Store wallet & top-up |
| `/order-history/` | Store order history + OTP confirm |
| `/factory/` | Factory dashboard |
| `/factory/orders/` | Factory order management |
| `/factory/wallet/` | Factory earnings wallet |
| `/control/` | Superadmin control panel |
| `/audit-trail/` | Financial ledger (superadmin only) |
| `/admin/` | Django admin panel |
| `/driver/<id>/?token=<token>` | Driver GPS app (no login) |
| `/webhooks/logistics/` | 3PL delivery status webhook |

---

## Management Commands

```bash
# Create open pools for all medicines with no active pool
python manage.py ensure_pool_coverage --commit

# Lock expired pools manually (normally done by Celery)
python manage.py lock_pools

# Fix any Product → Factory FK mismatches
python manage.py fix_factory_fk

# Run AI demand predictions manually
python manage.py run_predictions
```

---

## Environment Variables (`.env`)

```
SECRET_KEY=your-secret-key
DEBUG=True
RAZORPAY_KEY_ID=rzp_test_...
RAZORPAY_KEY_SECRET=...
LOGISTICS_PROVIDER=mock          # mock | delhivery | shadowfax
DELHIVERY_API_TOKEN=             # required if using delhivery
LOGISTICS_WEBHOOK_TOKEN=         # secret token for 3PL webhook
DATABASE_URL=                    # leave blank to use SQLite
REDIS_URL=redis://localhost:6379/0
```

## 📸 

### 1. Authentication & Onboarding

**Home Page**
![Home_Page](./images/Home_Page.png)

**Register**
![Register](./images/Register.png)

**Login**
![Login](./images/Login.png)

### 2. Store Module (Pharmacy Side)

**Store Dashboard**
![Store Dashboard](./images/Store_Dashboard.png)

**Pool List**
![Pool List](./images/Pool_List.png)

**Pool Detail**
![Pool Detail](./images/Pool_Detail.png)

**AI Predictions**
![AI Predictions](./images/AI_Predictions.png)

**Store Wallet**
![Store Wallet](./images/Store_Wallet.png)

### 3. Factory Module (Manufacturing Side)

**Factory Dashboard**
![Factory Dashboard](./images/Factory_Dashboard.png)

**Product Management**
![Product Management](./images/Factory_Product_Management.png)

**Order Process**
![Order Process](./images/Factory_Order_Process.png)

**Order Dispatched**
![Order Dispatched](./images/Factory_Order_Dispatched.png)

**Factory Wallet**
![Factory Wallet](./images/Factory_Wallet.png)

### 4. Logistics & Delivery Tracking

**Live Tracking Store View**
![Live Tracking Store View](./images/Order_Tracking_Live.png)

**Driver PWA Live Tracking**
![Driver PWA Live Tracking](./images/Driver_PWA_Live_Tracking.png)

### 5. SuperAdmin Control & Fintech Ledger

**Admin Dashboard**
![Admin Dashboard](./images/Admin_Dashboard.png)

**Admin Control - Stores**
![Admin Control - Stores](./images/Admin_Control_Stores.png)

**Admin Control - Factories**
![Admin Control - Factories](./images/Admin_Control_Factories.png)

**Financial Audit Trail**
![Financial Audit Trail](./images/Audit_Trail.png)

**GST Automated Invoice**
![GST Automated Invoice](./images/GST_Automated_Invoice.png)