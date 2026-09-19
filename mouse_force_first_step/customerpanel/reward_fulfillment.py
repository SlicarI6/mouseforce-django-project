"""Validated private fulfillment input and controlled staff processing.

No provider calls, refunds, restocking or open-ended requests belong here.
"""
from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .models import CustomerPoints, Reward, Redemption, RewardFulfillment, RedemptionEvent
from .reward_inventory import lock_staff_users

FULFILLMENT_FIELDS = frozenset(('recipient_name', 'contact_email', 'contact_phone',
    'address_line_1', 'address_line_2', 'city', 'region', 'postal_code', 'country_code', 'request_details'))


def fulfillment_requirements(reward):
    physical = reward.fulfillment_type == 'physical'
    supported = reward.fulfillment_type in ('physical', 'manual')
    shipping = supported and (physical or reward.requires_shipping_address)
    return {
        'shipping': shipping,
        'contact': supported and (physical or reward.requires_contact_details or reward.requires_phone),
        'region': shipping and reward.requires_region,
        'phone': supported and reward.requires_phone,
    }


class FulfillmentForm(forms.Form):
    recipient_name = forms.CharField(label='Full name', max_length=200)
    contact_email = forms.EmailField(label='Contact email', max_length=254)
    contact_phone = forms.RegexField(label='Telephone', regex=r'^\+?[0-9 ()\-\.]{6,32}$', max_length=32,
        error_messages={'invalid': 'Enter a valid telephone number.'})
    address_line_1 = forms.CharField(label='Address line 1', max_length=255)
    address_line_2 = forms.CharField(label='Address line 2 (optional)', max_length=255, required=False)
    city = forms.CharField(max_length=100)
    region = forms.CharField(label='Region / state', max_length=100)
    postal_code = forms.CharField(label='Postal code', max_length=32)
    country_code = forms.RegexField(label='Country (two-letter code, e.g. GB)', regex=r'^[A-Za-z]{2}$', max_length=2,
        error_messages={'invalid': 'Enter a two-letter country code, such as GB.'})
    request_details = forms.CharField(label='Details for this reward', max_length=4000, required=False,
        widget=forms.Textarea(attrs={'rows': 4}), help_text='Provide only the information requested in the reward instructions.')

    def __init__(self, reward, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reward = reward
        needed = fulfillment_requirements(reward)
        allowed = set()
        if needed['shipping']:
            allowed.update(('address_line_1', 'address_line_2', 'city', 'postal_code', 'country_code'))
        if needed['contact']:
            allowed.update(('recipient_name', 'contact_email'))
        if needed['region']:
            allowed.add('region')
        if needed['phone']:
            allowed.add('contact_phone')
        if reward.fulfillment_type == 'manual':
            allowed.add('request_details')
        for name in tuple(self.fields):
            if name not in allowed:
                del self.fields[name]
            else:
                self.fields[name].widget.attrs.update({'autocomplete': 'off', 'data-fulfillment-input': ''})
        if 'country_code' in self.fields and reward.country_code:
            self.initial['country_code'] = reward.country_code

    def clean_country_code(self):
        return self.cleaned_data['country_code'].upper()

    def clean_contact_phone(self):
        value = self.cleaned_data['contact_phone']
        if not 6 <= sum(character.isdigit() for character in value) <= 15:
            raise ValidationError('Enter a telephone number containing 6 to 15 digits.')
        return value

    def clean(self):
        data = super().clean()
        if set(self.data) - set(self.fields):
            raise ValidationError('This reward does not request those additional details.')
        for name in ('country_code', 'region', 'city'):
            restriction = getattr(self.reward, name, '')
            if name in data and restriction and data[name].casefold() != restriction.strip().casefold():
                self.add_error(name, 'This address is outside the delivery area shown for this reward.')
        return data


class FulfillmentInputError(ValidationError):
    def __init__(self, form):
        self.form = form
        super().__init__('Please check the fulfillment details below.', code='invalid_fulfillment')


@sensitive_variables()
def validate_fulfillment(reward, data):
    form = FulfillmentForm(reward, data={} if data is None else data)
    if not form.is_valid():
        raise FulfillmentInputError(form)
    return form.cleaned_data


@transaction.atomic
def process_fulfillment(actor, fulfillment_id, target_status):
    """User(s) -> Points -> Reward -> Redemption -> Fulfillment, no external I/O."""
    identity = RewardFulfillment.objects.filter(pk=fulfillment_id).values(
        'redemption_id', 'redemption__user_id', 'redemption__reward_id').first()
    if identity is None:
        raise ValidationError('This fulfillment is unavailable.')
    current = lock_staff_users(actor, [identity['redemption__user_id']])
    if not current.has_perm('customerpanel.change_rewardfulfillment'):
        raise PermissionDenied
    CustomerPoints.objects.select_for_update().filter(user_id=identity['redemption__user_id']).first()
    Reward.objects.select_for_update().get(pk=identity['redemption__reward_id'])
    redemption = Redemption.objects.select_for_update().get(pk=identity['redemption_id'])
    fulfillment = RewardFulfillment.objects.select_for_update().get(pk=fulfillment_id)
    if fulfillment.status == target_status:
        return fulfillment.pk  # Duplicate staff action creates no extra event.
    transitions = {'pending': {'processing'}, 'processing': {'dispatched', 'completed'}, 'dispatched': {'completed'}}
    if (target_status not in transitions.get(fulfillment.status, set())
            or redemption.status not in ('pending', 'processing') or redemption.refunded_at
            or (target_status == 'dispatched' and not fulfillment.address_line_1)):
        raise ValidationError('This fulfillment cannot make that status change.')
    at = timezone.now()
    fulfillment.status = target_status
    fulfillment.processed_by = current
    fields = ['status', 'processed_by', 'updated_at']
    if target_status == 'dispatched':
        fulfillment.dispatched_at = at
        fields.append('dispatched_at')
    if target_status == 'completed':
        fulfillment.completed_at = at
        fields.append('completed_at')
        redemption.status, redemption.completed_at = 'fulfilled', at
    else:
        redemption.status = 'processing'
    fulfillment.save(update_fields=fields)
    redemption.save(update_fields=['status', 'completed_at', 'updated_at'])
    RedemptionEvent.objects.create(redemption=redemption, actor=current,
        event_type='fulfilled' if target_status == 'completed' else 'processing',
        customer_message={'processing': 'Your reward is being processed.', 'dispatched': 'Your reward has been dispatched.',
                          'completed': 'Your reward fulfillment is complete.'}[target_status])
    return fulfillment.pk
