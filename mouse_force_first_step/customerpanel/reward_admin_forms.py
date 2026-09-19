from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .models import Reward, RewardCode
from .reward_inventory import MAX_IMPORT_CODES, import_key_material, validate_private_values


def edit_state(instance):
    if isinstance(instance, Reward):
        return {'id': str(instance.pk), 'revision': instance.revision, 'stock': instance.stock_remaining,
                'updated': instance.updated_at.isoformat() if instance.updated_at else None}
    return {'id': str(instance.pk), 'active': instance.is_active,
            'expires': instance.expires_at.isoformat() if instance.expires_at else None,
            'assigned': str(instance.redemption_id) if instance.redemption_id else None}


class ProtectedEditForm(forms.ModelForm):
    edit_token = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.original_state = edit_state(self.instance)
        if not self.instance._state.adding:
            self.initial['edit_token'] = signing.dumps(self.original_state, salt='rewards-admin-edit')

    def clean(self):
        data = super().clean()
        if not self.instance._state.adding:
            try:
                state = signing.loads(data.get('edit_token', ''), salt='rewards-admin-edit', max_age=86400)
                if state != self.original_state:
                    raise signing.BadSignature
            except signing.BadSignature:
                raise ValidationError('This record changed or the edit form expired. Reload the page before saving; nothing was changed.') from None
        return data


class RewardAdminForm(ProtectedEditForm):
    class Meta:
        model = Reward
        exclude = ['revision', 'created_at', 'updated_at']
        labels = {'information_url': 'Public information URL'}
        help_texts = {
            'information_url': 'Public information only. Import private claim links through Reward codes.',
            'fulfillment_instructions': 'Customer-facing instructions. Keep voucher codes and private claim links in Reward codes.',
        }

    def clean(self):
        data = super().clean()
        if data.get('image_url') and not data.get('image_alt'):
            self.add_error('image_alt', 'Describe the image for customers using screen readers.')
        if data.get('access_scope') == 'selected_customers' and not data.get('eligible_users'):
            self.add_error('eligible_users', 'Select at least one active customer for a restricted reward.')
        if data.get('is_limited_time') and not data.get('valid_until'):
            self.add_error('valid_until', 'Set an end date for a limited-time reward.')
        if data.get('is_active') and data.get('valid_until') and data['valid_until'] <= timezone.now():
            self.add_error('is_active', 'An expired reward cannot be activated. Update its dates or leave it inactive.')
        if not self.instance._state.adding and data.get('fulfillment_type') != self.instance.fulfillment_type:
            if self.instance.private_codes.exists() or self.instance.redemptions.exists():
                self.add_error('fulfillment_type', 'Create a new reward to change fulfillment type after inventory or redemptions exist.')
        if data.get('is_active') and (self.instance._state.adding or not self.instance.is_active) and data.get('fulfillment_type') in ('voucher', 'external'):
            available = self.instance.private_codes.filter(is_active=True, redemption__isnull=True).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())).exists() if not self.instance._state.adding else False
            if not available:
                self.add_error('is_active', 'Save the reward as inactive, import available private inventory, then activate it.')
        if data.get('fulfillment_type') == 'manual' and data.get('requires_phone'):
            data['requires_contact_details'] = True
        shipping = data.get('fulfillment_type') == 'physical' or data.get('requires_shipping_address')
        if data.get('requires_region') and not shipping:
            self.add_error('requires_region', 'A region can only be required when a shipping address is required.')
        if data.get('fulfillment_type') in ('voucher', 'external'):
            for name in ('requires_shipping_address', 'requires_contact_details', 'requires_region', 'requires_phone'):
                if data.get(name):
                    self.add_error(name, 'Delivery/contact requirements are only for physical or manual rewards.')
        return data


class RewardCodeAdminForm(ProtectedEditForm):
    class Meta:
        model = RewardCode
        fields = ['is_active', 'expires_at', 'edit_token']

    def clean(self):
        data = super().clean()
        if data.get('expires_at') and data['expires_at'] <= timezone.now() and data.get('is_active'):
            self.add_error('expires_at', 'Use a future expiry date or disable this inventory item.')
        return data


class PrivateValuesWidget(forms.Textarea):
    def format_value(self, value):
        # Never echo secrets back into an HTML response, including validation errors.
        return None


class PrivateInventoryImportForm(forms.Form):
    reward = forms.ModelChoiceField(queryset=Reward.objects.filter(fulfillment_type__in=['voucher', 'external']).order_by('title'))
    private_values = forms.CharField(
        label='Codes or private claim links', strip=False, max_length=MAX_IMPORT_CODES * 2050,
        widget=PrivateValuesWidget(attrs={'rows': 10, 'cols': 70, 'autocomplete': 'off', 'spellcheck': 'false'}),
        help_text=f'One value per line, up to {MAX_IMPORT_CODES}. Values are encrypted and cannot be viewed here after import.',
    )
    expires_at = forms.DateTimeField(required=False, help_text='Optional inventory expiry; use the project UTC timezone.')
    is_active = forms.BooleanField(required=False, initial=True, label='Enable imported inventory')

    @sensitive_variables()
    def clean(self):
        data = super().clean()
        reward, raw = data.get('reward'), data.get('private_values')
        if reward and raw:
            kind = 'code' if reward.fulfillment_type == 'voucher' else 'claim_link'
            data['private_values'] = validate_private_values([line for line in raw.splitlines() if line.strip()], kind)
        if data.get('expires_at') and data['expires_at'] <= timezone.now():
            self.add_error('expires_at', 'Use a future expiry date.')
        import_key_material()
        return data
