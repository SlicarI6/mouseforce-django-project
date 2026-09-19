"""Owned requests and controlled staff review. Neither operation spends Points."""
from uuid import UUID, uuid4

from django import forms
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import MaxLengthValidator
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .models import Reward, RewardRequest
from .points import _lock_customer
from .reward_confirmation import _active_user, _session_binding, reject
from .reward_inventory import lock_staff_users

SUBMISSION_SALT = 'customerpanel.reward-request.submit.v1'
REVIEW_SALT = 'customerpanel.reward-request.review.v1'


class RewardRequestForm(forms.ModelForm):
    submission_token = forms.CharField(widget=forms.HiddenInput)

    class Meta:
        model = RewardRequest
        fields = ('title', 'category', 'description', 'country_code', 'city', 'reference_url')
        labels = {'description': 'What are you looking for?', 'country_code': 'Preferred country code (optional)',
                  'city': 'Preferred city (optional)', 'reference_url': 'Public product or provider link (optional)'}
        help_texts = {'description': 'Describe a voucher, discount, product, experience, service or other reward. You can include a preferred provider and an approximate Points budget. Please do not include passwords, private codes or payment details.',
                      'country_code': 'Two-letter country code, for example GB.'}
        widgets = {'description': forms.Textarea(attrs={'rows': 5})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['description'].max_length = 5000
        self.fields['description'].validators.append(MaxLengthValidator(5000))
        for name, field in self.fields.items():
            field.widget.attrs.update({'data-fulfillment-input': '', 'autocomplete': 'off'})
        self.fields['description'].widget.attrs['maxlength'] = 5000

    def clean_country_code(self):
        return self.cleaned_data['country_code'].upper()


def submission_token(user, session_key):
    current = _active_user(user)
    return signing.dumps({'user': str(current.pk), 'session': _session_binding(current, session_key),
                          'id': str(uuid4())}, salt=SUBMISSION_SALT)


@sensitive_variables()
@transaction.atomic
def submit_reward_request(user, data, *, session_key):
    current = _lock_customer(user)
    form = RewardRequestForm(data)
    if not form.is_valid():
        return None, form
    try:
        token = signing.loads(form.cleaned_data['submission_token'], salt=SUBMISSION_SALT, max_age=86400)
        if (token['user'] != str(current.pk) or token['session'] != _session_binding(current, session_key)):
            raise ValueError
        request_id = UUID(token['id'])
    except (signing.BadSignature, ValueError, KeyError, TypeError):
        form.add_error(None, 'Please reopen the request form and try again.')
        return None, form
    # The authenticated User lock serializes retries without touching Points.
    existing = RewardRequest.objects.filter(pk=request_id, user=current).first()
    if existing:
        return existing, form
    record = form.save(commit=False)
    record.pk, record.user = request_id, current
    record.save()
    return record, form


def approved_request(user, request_id, reward_id, *, lock=False):
    rows = RewardRequest.objects.select_for_update() if lock else RewardRequest.objects
    try:
        record = rows.get(pk=request_id, user=user)
    except (RewardRequest.DoesNotExist, ValidationError):
        raise PermissionDenied from None
    if record.approved_reward_id != reward_id:
        reject('request_unavailable', 'This request does not have this approved offer.')
    if not record.redemption_id and record.status != RewardRequest.Status.APPROVED:
        reject('request_unavailable', 'This request is not approved for redemption.')
    return record


class RewardRequestReviewForm(forms.Form):
    review_token = forms.CharField(widget=forms.HiddenInput)
    status = forms.ChoiceField(choices=RewardRequest.Status.choices)
    approved_reward = forms.ModelChoiceField(queryset=Reward.objects.order_by('title'), required=False,
        help_text='Create a priced Reward in Rewards Admin first. For a private offer, select Selected customers and include only this customer.')
    allow_shared_offer = forms.BooleanField(required=False, label='I intend to offer an existing shared/public reward',
        help_text='Leave unchecked for a reward restricted to this customer alone. Approval does not change reward eligibility.')
    staff_response = forms.CharField(required=False, max_length=5000, widget=forms.Textarea,
        label='Response visible to the customer')
    internal_notes = forms.CharField(required=False, max_length=10000, widget=forms.Textarea)


def review_token(record):
    return signing.dumps({'id': str(record.pk), 'updated': record.updated_at.isoformat()}, salt=REVIEW_SALT)


@sensitive_variables()
@transaction.atomic
def review_reward_request(actor, request_id, *, status, approved_reward=None, allow_shared_offer=False,
                          staff_response='', internal_notes='', review_token=''):
    try:
        owner_id = RewardRequest.objects.values_list('user_id', flat=True).get(pk=request_id)
    except RewardRequest.DoesNotExist:
        raise PermissionDenied from None
    staff = lock_staff_users(actor, (owner_id,))
    if not staff.has_perm('customerpanel.change_rewardrequest'):
        raise PermissionDenied
    reward = None
    if status == RewardRequest.Status.APPROVED:
        if approved_reward is None:
            raise ValidationError('Choose a specific priced reward before approving.')
        reward = Reward.objects.select_for_update().get(pk=approved_reward.pk)
    record = RewardRequest.objects.select_for_update().get(pk=request_id)
    try:
        expected = signing.loads(review_token, salt=REVIEW_SALT, max_age=3600)
        if expected != {'id': str(record.pk), 'updated': record.updated_at.isoformat()}:
            raise ValueError
    except (signing.BadSignature, ValueError, TypeError):
        raise ValidationError('This request changed. Reload it before reviewing again.') from None
    if record.user_id != owner_id or record.redemption_id:
        raise ValidationError('An accepted request cannot be reviewed or relinked.')
    if status not in RewardRequest.Status.values:
        raise ValidationError('Choose a valid review status.')
    if reward:
        from .reward_confirmation import verify_reward_access
        verify_reward_access(record.user, reward, timezone.now())
        private = reward.access_scope == 'selected_customers' and set(reward.eligible_users.values_list('pk', flat=True)) == {owner_id}
        if not private and not allow_shared_offer:
            raise ValidationError('Restrict this Reward to this customer, or explicitly choose a shared/public offer.')
    record.status, record.approved_reward = status, reward
    record.staff_response, record.internal_notes = staff_response, internal_notes
    record.reviewed_by, record.reviewed_at = staff, timezone.now()
    record.save(update_fields=['status', 'approved_reward', 'staff_response', 'internal_notes', 'reviewed_by', 'reviewed_at', 'updated_at'])
    return record
