"""
forms.py — BulkMed Django Forms
==================================
Forms handle user input validation and HTML widget rendering.

Django forms work in two steps:
  1. GET request  → form is empty, rendered in template
  2. POST request → form is bound to request.POST data,
                    validated, and saved if valid

Each form class maps to a model (ModelForm) or standalone fields.
The 'widgets' dict customises how each field renders in HTML
(e.g., adding Bootstrap's 'form-control' CSS class).
"""

from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm  # built-in registration form
from .models import OrderEntry, Inventory, MedicalStore, OrderPool


# ── REGISTRATION ──────────────────────────────────────────────────────────────

class RegistrationForm(UserCreationForm):
    """
    Extended registration form with account type (Store/Factory) and license number.
    """
    ACCOUNT_TYPE_CHOICES = [
        ('store',   'Medical Store / Pharmacy'),
        ('factory', 'Medicine Factory / Manufacturer'),
    ]
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'your@email.com'})
    )
    account_type = forms.ChoiceField(
        choices=ACCOUNT_TYPE_CHOICES,
        widget=forms.RadioSelect(),
        initial='store',
    )
    business_name = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Apollo Pharmacy'})
    )
    license_no = forms.CharField(
        max_length=100,
        label='Drug License Number',
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. MH-MUM-123456'})
    )
    contact = forms.CharField(
        max_length=15,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': '10-digit mobile number'})
    )
    city = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Mumbai'})
    )

    def clean_license_no(self):
        """
        Validate license number uniqueness across both stores and factories.
        Reads account_type from the submitted POST data to check the right model.
        """
        from .models import MedicalStore, Factory
        license = self.cleaned_data.get('license_no', '').strip()
        account_type = self.data.get('account_type', 'store')

        if account_type == 'store':
            if MedicalStore.objects.filter(license_no=license).exists():
                raise forms.ValidationError(
                    'This license number is already registered to a store. '
                    'Please use a different license or contact support.'
                )
        else:
            if Factory.objects.filter(license_no=license).exists():
                raise forms.ValidationError(
                    'This license number is already registered to a factory. '
                    'Please use a different license or contact support.'
                )
        return license

    class Meta:
        model = User
        fields = ['username', 'email', 'password1', 'password2']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if hasattr(field.widget, 'attrs'):
                field.widget.attrs.setdefault('class', 'form-control')
            field.help_text = ''


# ── ORDER ENTRY ───────────────────────────────────────────────────────────────

class OrderEntryForm(forms.ModelForm):
    """
    Form for joining an order pool.
    Minimum quantity is 4 strips (15 stores × 4 = 60 strips, guaranteeing the 50-strip pool minimum).
    """
    class Meta:
        model = OrderEntry
        fields = ['quantity', 'mode']
        widgets = {
            'quantity': forms.NumberInput(attrs={'class': 'form-control', 'min': 4}),
            'mode': forms.RadioSelect(attrs={'class': 'form-check-input'}),
        }

    def clean_quantity(self):
        qty = self.cleaned_data.get('quantity')
        if qty is not None and qty < 4:
            raise forms.ValidationError('Minimum order quantity is 4 strips per store.')
        return qty


# ── INVENTORY UPDATE ──────────────────────────────────────────────────────────

class InventoryUpdateForm(forms.ModelForm):
    """
    Form for updating a store's stock levels.
    Used by store owners to manually update current_stock and threshold.
    """
    class Meta:
        model = Inventory
        fields = ['current_stock', 'threshold']
        widgets = {
            'current_stock': forms.NumberInput(attrs={'class': 'form-control'}),
            'threshold': forms.NumberInput(attrs={'class': 'form-control'}),
        }


# ── STORE SETUP (FIRST TIME) ──────────────────────────────────────────────────

class StoreSetupForm(forms.ModelForm):
    """
    First-time store profile creation form.
    Used at /setup/ after a new user registers.
    Does NOT include is_verified or wallet_balance — those are admin-only.
    """
    class Meta:
        model = MedicalStore
        fields = ['name', 'address', 'license_no', 'contact', 'city', 'latitude', 'longitude', 'gstin']
        widgets = {
            'name':       forms.TextInput(attrs={'class': 'form-control'}),
            'address':    forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'license_no': forms.TextInput(attrs={'class': 'form-control'}),
            'contact':    forms.TextInput(attrs={'class': 'form-control'}),
            'city':       forms.TextInput(attrs={'class': 'form-control'}),
            'latitude':   forms.NumberInput(attrs={'class': 'form-control'}),
            'longitude':  forms.NumberInput(attrs={'class': 'form-control'}),
            'gstin':      forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 27AAPFU0939F1ZV (optional)', 'maxlength': '15'}),
        }


# ── STORE EDIT (STAFF / CONTROL PANEL) ───────────────────────────────────────

class StoreEditForm(forms.ModelForm):
    """
    Full store edit form used in the Control Panel's slide-out drawer.
    Includes all StoreSetupForm fields PLUS is_verified, wallet_balance, and gstin.
    """
    class Meta:
        model = MedicalStore
        fields = [
            'name', 'address', 'license_no', 'contact',
            'city', 'latitude', 'longitude', 'is_verified', 'wallet_balance', 'gstin'
        ]
        widgets = {
            'name':           forms.TextInput(attrs={'class': 'form-control'}),
            'address':        forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'license_no':     forms.TextInput(attrs={'class': 'form-control'}),
            'contact':        forms.TextInput(attrs={'class': 'form-control'}),
            'city':           forms.TextInput(attrs={'class': 'form-control'}),
            'latitude':       forms.NumberInput(attrs={'class': 'form-control', 'step': '0.000001'}),
            'longitude':      forms.NumberInput(attrs={'class': 'form-control', 'step': '0.000001'}),
            'wallet_balance': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'gstin':          forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 27AAPFU0939F1ZV', 'maxlength': '15'}),
        }


# ── POOL EDIT (STAFF / CONTROL PANEL) ────────────────────────────────────────

class PoolEditForm(forms.ModelForm):
    """
    Pool edit form used in the Control Panel's slide-out drawer.
    Allows staff to change city, status, and expiry time without
    going to the Django admin panel.

    The __init__ override pre-formats expires_at for the HTML
    datetime-local input (which requires 'YYYY-MM-DDTHH:MM' format).
    """
    class Meta:
        model = OrderPool
        fields = ['city', 'status', 'expires_at']
        widgets = {
            'city':       forms.TextInput(attrs={'class': 'form-control'}),
            'status':     forms.Select(attrs={'class': 'form-control'}),
            # datetime-local input type shows a date+time picker in the browser
            'expires_at': forms.DateTimeInput(
                attrs={'class': 'form-control', 'type': 'datetime-local'},
                format='%Y-%m-%dT%H:%M'
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Pre-format the existing expires_at value for the datetime-local input
        if self.instance and self.instance.expires_at:
            self.initial['expires_at'] = self.instance.expires_at.strftime('%Y-%m-%dT%H:%M')

# ── PRODUCT (STAFF / CONTROL PANEL) ──────────────────────────────────────────

from .models import Product

class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = ['name', 'generic_name', 'sku_code', 'hsn_code', 'barcode', 'category', 'factory_name', 'base_price', 'unit']
        widgets = {
            'name':         forms.TextInput(attrs={'class': 'form-control'}),
            'generic_name': forms.TextInput(attrs={'class': 'form-control'}),
            'sku_code':     forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. DOLO-650-MG'}),
            'hsn_code':     forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 30049099'}),
            'barcode':      forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Optional barcode'}),
            'category':     forms.Select(attrs={'class': 'form-control'}),
            'factory_name': forms.TextInput(attrs={'class': 'form-control'}),
            'base_price':   forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'unit':         forms.TextInput(attrs={'class': 'form-control'}),
        }


# ── FACTORY PRODUCT (factory self-service catalog) ────────────────────────────

class FactoryProductForm(forms.ModelForm):
    """
    Used by factory users to create/edit their own products.
    Excludes factory and factory_name — those are force-set in the view
    so a factory can never assign a product to a different factory.
    """
    class Meta:
        model = Product
        fields = ['name', 'generic_name', 'sku_code', 'hsn_code', 'barcode', 'category', 'base_price', 'unit']
        widgets = {
            'name':         forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Dolo 650'}),
            'generic_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Paracetamol'}),
            'sku_code':     forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. DOLO-650-MG'}),
            'hsn_code':     forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 30049099'}),
            'barcode':      forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Optional barcode'}),
            'category':     forms.Select(attrs={'class': 'form-control'}),
            'base_price':   forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'}),
            'unit':         forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'strip / bottle / vial'}),
        }


# ── POOL CREATE (STAFF / CONTROL PANEL) ──────────────────────────────────────

class PoolCreateForm(forms.ModelForm):
    class Meta:
        model = OrderPool
        fields = ['product', 'city', 'expires_at']
        widgets = {
            'product':    forms.Select(attrs={'class': 'form-control'}),
            'city':       forms.TextInput(attrs={'class': 'form-control'}),
            'expires_at': forms.DateTimeInput(
                attrs={'class': 'form-control', 'type': 'datetime-local'},
                format='%Y-%m-%dT%H:%M'
            ),
        }


# ── DISPUTE FORM ──────────────────────────────────────────────────────────────

from .models import Dispute

class DisputeForm(forms.ModelForm):
    """
    Used on the confirm_delivery page to raise a dispute.
    Only reason and photo are store-submitted; all other fields are set in the view.
    """
    class Meta:
        model = Dispute
        fields = ['reason', 'photo']
        widgets = {
            'reason': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 3,
                'placeholder': 'e.g. 3 strips were damaged, packaging was torn, wrong medicine received…',
            }),
            'photo': forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': 'image/*'}),
        }
        labels = {
            'reason': 'Describe the issue',
            'photo':  'Upload photo of damaged goods (optional)',
        }


# ── ADMIN MODAL FORMS ─────────────────────────────────────────────────────────
# Dark-themed ModelForms used by the Master Admin modal.
# All widgets use the 'admin-input' CSS class defined in master_admin.html.

from .models import Factory

ADMIN_INPUT  = {'class': 'admin-input'}
ADMIN_SELECT = {'class': 'admin-input admin-select'}
ADMIN_CHECK  = {'class': 'admin-checkbox'}
ADMIN_AREA   = {'class': 'admin-input', 'rows': 2}


class AdminFactoryForm(forms.ModelForm):
    """Full factory edit form for the Master Admin modal."""
    class Meta:
        model = Factory
        fields = [
            'name', 'address', 'city', 'license_no', 'contact',
            'gstin', 'wallet_balance', 'is_verified', 'latitude', 'longitude',
        ]
        widgets = {
            'name':           forms.TextInput(attrs=ADMIN_INPUT),
            'address':        forms.Textarea(attrs=ADMIN_AREA),
            'city':           forms.TextInput(attrs=ADMIN_INPUT),
            'license_no':     forms.TextInput(attrs=ADMIN_INPUT),
            'contact':        forms.TextInput(attrs=ADMIN_INPUT),
            'gstin':          forms.TextInput(attrs={**ADMIN_INPUT, 'maxlength': '15', 'placeholder': 'e.g. 27AAPFU0939F1ZV'}),
            'wallet_balance': forms.NumberInput(attrs={**ADMIN_INPUT, 'step': '0.01'}),
            'is_verified':    forms.CheckboxInput(attrs=ADMIN_CHECK),
            'latitude':       forms.NumberInput(attrs={**ADMIN_INPUT, 'step': '0.000001'}),
            'longitude':      forms.NumberInput(attrs={**ADMIN_INPUT, 'step': '0.000001'}),
        }
        labels = {
            'license_no':     'Drug License No.',
            'wallet_balance': 'Wallet Balance (₹)',
            'is_verified':    'Verified Partner',
        }


class AdminStoreForm(forms.ModelForm):
    """Full store edit form for the Master Admin modal."""
    class Meta:
        model = MedicalStore
        fields = [
            'name', 'address', 'city', 'license_no', 'contact',
            'gstin', 'wallet_balance', 'is_verified', 'latitude', 'longitude',
        ]
        widgets = {
            'name':           forms.TextInput(attrs=ADMIN_INPUT),
            'address':        forms.Textarea(attrs=ADMIN_AREA),
            'city':           forms.TextInput(attrs=ADMIN_INPUT),
            'license_no':     forms.TextInput(attrs=ADMIN_INPUT),
            'contact':        forms.TextInput(attrs=ADMIN_INPUT),
            'gstin':          forms.TextInput(attrs={**ADMIN_INPUT, 'maxlength': '15', 'placeholder': 'e.g. 27AAPFU0939F1ZV'}),
            'wallet_balance': forms.NumberInput(attrs={**ADMIN_INPUT, 'step': '0.01'}),
            'is_verified':    forms.CheckboxInput(attrs=ADMIN_CHECK),
            'latitude':       forms.NumberInput(attrs={**ADMIN_INPUT, 'step': '0.000001'}),
            'longitude':      forms.NumberInput(attrs={**ADMIN_INPUT, 'step': '0.000001'}),
        }
        labels = {
            'license_no':     'Drug License No.',
            'wallet_balance': 'Wallet Balance (₹)',
            'is_verified':    'Verified Partner',
        }


class AdminProductForm(forms.ModelForm):
    """Full product edit form for the Master Admin modal."""
    class Meta:
        model = Product
        fields = [
            'name', 'generic_name', 'sku_code', 'hsn_code', 'barcode',
            'category', 'base_price', 'unit', 'factory', 'is_active', 'expiry_date',
        ]
        widgets = {
            'name':         forms.TextInput(attrs=ADMIN_INPUT),
            'generic_name': forms.TextInput(attrs=ADMIN_INPUT),
            'sku_code':     forms.TextInput(attrs={**ADMIN_INPUT, 'placeholder': 'e.g. DOLO-650-MG'}),
            'hsn_code':     forms.TextInput(attrs={**ADMIN_INPUT, 'placeholder': 'e.g. 30049099'}),
            'barcode':      forms.TextInput(attrs={**ADMIN_INPUT, 'placeholder': 'Optional'}),
            'category':     forms.Select(attrs=ADMIN_SELECT),
            'base_price':   forms.NumberInput(attrs={**ADMIN_INPUT, 'step': '0.01'}),
            'unit':         forms.TextInput(attrs={**ADMIN_INPUT, 'placeholder': 'strip / bottle / vial'}),
            'factory':      forms.Select(attrs=ADMIN_SELECT),
            'is_active':    forms.CheckboxInput(attrs=ADMIN_CHECK),
            'expiry_date':  forms.DateInput(attrs={**ADMIN_INPUT, 'type': 'date'}),
        }
        labels = {
            'base_price':  'Base Price (₹)',
            'is_active':   'Active (visible in pools)',
            'expiry_date': 'Batch Expiry Date',
        }
