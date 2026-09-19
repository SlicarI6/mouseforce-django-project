"""Read-only confirmation quotes. No allocation or secret payloads."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import re
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import F, Q
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.views.decorators.debug import sensitive_variables

from .models import CustomerPoints, Redemption, Reward, RewardCode

CONFIRMATION_SECONDS = 15 * 60
TOKEN_SALT = 'customerpanel.reward-confirmation.v1'
_UNSET = object()
# Stock counts, revision and cosmetic images are intentionally not offer terms.
MATERIAL_FIELDS = (
    'id', 'title', 'short_description', 'full_description', 'category', 'points_required',
    'fulfillment_type', 'partner_name', 'information_url', 'country_code', 'region',
    'city', 'location_details', 'valid_from', 'valid_until', 'benefit_valid_until',
    'terms', 'fulfillment_instructions', 'max_redemptions_per_customer',
    'is_limited_time', 'is_exclusive',
    'requires_shipping_address', 'requires_contact_details', 'requires_region', 'requires_phone',
)


class ConfirmationError(ValidationError):
    pass


def reject(code, message):
    raise ConfirmationError(message, code=code)


def _active_user(user):
    if not user.is_authenticated:
        raise PermissionDenied
    try:
        current = get_user_model().objects.get(pk=user.pk)
    except get_user_model().DoesNotExist:
        raise PermissionDenied from None
    if not current.is_active or current.role != 'customer':
        raise PermissionDenied
    return current


@sensitive_variables()
def _session_binding(user, session_key):
    # The caller supplies request.session.session_key, never a POST field.
    if not isinstance(session_key, str) or not session_key:
        raise PermissionDenied('An authenticated session is required.')
    value = f'{user.pk}:{session_key}:{user.get_session_auth_hash()}'
    return salted_hmac(TOKEN_SALT + '.session', value).hexdigest()


def _wire(value):
    return value.isoformat() if isinstance(value, datetime) else str(value) if isinstance(value, UUID) else value


def material_fingerprint(reward, promised_expiry):
    data = {name: _wire(getattr(reward, name)) for name in MATERIAL_FIELDS}
    data['promised_expiry'] = _wire(promised_expiry)
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def verify_reward_access(user, reward, at):
    if not reward.is_active or (reward.valid_from and reward.valid_from > at) or (reward.valid_until and reward.valid_until <= at):
        reject('reward_unavailable', 'This reward is not currently available.')
    if reward.benefit_valid_until and reward.benefit_valid_until <= at:
        reject('reward_unavailable', 'This reward is not currently available.')
    if reward.access_scope == 'selected_customers' and not reward.eligible_users.filter(pk=user.pk).exists():
        reject('reward_unavailable', 'This reward is not currently available.')
    if reward.access_scope not in ('all_customers', 'selected_customers'):
        reject('reward_unavailable', 'This reward is not currently available.')


def verify_limit_and_balance(user, reward, balance):
    limit = reward.max_redemptions_per_customer
    # Refunded records still count; rolled-back attempts leave no record.
    if limit is not None and Redemption.objects.filter(user=user, reward=reward).count() >= limit:
        reject('limit_reached', 'Your redemption limit for this reward has been reached.')
    if balance < reward.points_required:
        reject('insufficient_points', 'You do not have enough Points for this reward.')


def eligible_private_codes(reward, at, *, promised_expiry=_UNSET):
    """Shared inventory predicate; callers select metadata or lock a candidate."""
    kind = {'voucher': 'code', 'external': 'claim_link'}.get(reward.fulfillment_type)
    if not kind:
        reject('reward_unavailable', 'This reward is not currently available.')
    codes = RewardCode.objects.filter(reward=reward, payload_kind=kind, is_active=True, redemption__isnull=True).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=at))
    expiry = reward.benefit_valid_until if promised_expiry is _UNSET else promised_expiry
    if expiry is not None:
        codes = codes.filter(Q(expires_at__isnull=True) | Q(expires_at__gte=expiry))
    elif promised_expiry is not _UNSET:
        # An undated quote cannot silently become a dated/shorter entitlement.
        codes = codes.filter(expires_at__isnull=True)
    return codes.order_by(F('expires_at').asc(nulls_last=True), 'created_at', 'pk')


def verify_availability(reward, at, *, promised_expiry=_UNSET):
    """Read only expiry/status metadata; never retrieve encrypted code fields."""
    if reward.fulfillment_type in ('physical', 'manual'):
        if reward.stock_remaining is not None and reward.stock_remaining <= 0:
            reject('out_of_stock', 'This reward is currently out of stock.')
        return reward.benefit_valid_until
    codes = eligible_private_codes(reward, at, promised_expiry=promised_expiry)
    expiry = reward.benefit_valid_until if promised_expiry is _UNSET else promised_expiry
    candidate = codes.values('expires_at').first()
    if candidate is None:
        reject('out_of_stock', 'No inventory is available with the confirmed validity.')
    return expiry if expiry is not None else candidate['expires_at']


@dataclass(frozen=True)
class RewardConfirmation:
    reward_id: UUID
    idempotency_key: UUID
    points_required: int
    balance: int
    balance_after: int
    promised_expiry: datetime | None
    expires_at: datetime
    token: str = field(repr=False)
    # The page displays exactly the public terms used to sign this quote, rather
    # than rereading a potentially edited reward after the signature is created.
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ConfirmationClaims:
    reward_id: UUID
    idempotency_key: UUID
    balance: int
    fingerprint: str
    promised_expiry: datetime | None
    request_id: UUID | None = None
    request_version: str | None = None


@sensitive_variables()
def create_reward_confirmation(user, reward_id, *, session_key, request_id=None):
    current = _active_user(user)
    binding = _session_binding(current, session_key)
    try:
        reward = Reward.objects.get(pk=reward_id)
    except (Reward.DoesNotExist, ValidationError):
        reject('reward_unavailable', 'This reward is not currently available.')
    at = timezone.now()
    request_record = None
    if request_id is not None:
        from .reward_requests import approved_request
        request_record = approved_request(current, request_id, reward.pk)
        if request_record.redemption_id:
            reject('request_unavailable', 'This request has already been accepted.')
    verify_reward_access(current, reward, at)
    balance = CustomerPoints.objects.filter(user=current).values_list('total_points', flat=True).first() or 0
    verify_limit_and_balance(current, reward, balance)
    expiry = verify_availability(reward, at)
    intent = uuid4()
    payload = {
        'v': 1, 'customer': str(current.pk), 'session': binding, 'reward': str(reward.pk),
        'intent': str(intent), 'balance': balance, 'offer': material_fingerprint(reward, expiry),
        'expiry': _wire(expiry),
    }
    if request_record:
        payload.update(request=str(request_record.pk), request_version=request_record.updated_at.isoformat())
    return RewardConfirmation(reward.pk, intent, reward.points_required, balance,
        balance - reward.points_required, expiry, timezone.now() + timedelta(seconds=CONFIRMATION_SECONDS),
        signing.dumps(payload, salt=TOKEN_SALT),
        {name: getattr(reward, name) for name in MATERIAL_FIELDS})


@sensitive_variables()
def read_confirmation_token(user, token, *, session_key, allow_expired=False):
    """Use a freshly verified/locked user. Expiry bypass is only for replay lookup."""
    if not user.is_authenticated or not user.is_active or user.role != 'customer':
        raise PermissionDenied
    binding = _session_binding(user, session_key)
    if not isinstance(token, str) or len(token) > 4096:
        reject('invalid_confirmation', 'The confirmation is invalid. Please review the reward again.')
    try:
        data = signing.loads(token, salt=TOKEN_SALT, max_age=None if allow_expired else CONFIRMATION_SECONDS)
    except signing.SignatureExpired:
        reject('expired_confirmation', 'This confirmation expired. Please review the reward again.')
    except (signing.BadSignature, ValueError, TypeError):
        reject('invalid_confirmation', 'The confirmation is invalid. Please review the reward again.')
    try:
        base_fields = {'v', 'customer', 'session', 'reward', 'intent', 'balance', 'offer', 'expiry'}
        if not isinstance(data, dict) or set(data) not in (base_fields, base_fields | {'request', 'request_version'}):
            raise ValueError
        if type(data['v']) is not int or data['v'] != 1 or type(data['balance']) is not int or not 0 <= data['balance'] <= 2 ** 63 - 1:
            raise ValueError
        if not all(isinstance(data[name], str) for name in ('customer', 'session', 'reward', 'intent', 'offer')):
            raise ValueError
        if not re.fullmatch(r'[0-9a-f]{64}', data['offer']):
            raise ValueError
        expiry = datetime.fromisoformat(data['expiry']) if data['expiry'] is not None else None
        if expiry is not None and timezone.is_naive(expiry):
            raise ValueError
        reward_id, intent = UUID(data['reward']), UUID(data['intent'])
        request_id = UUID(data['request']) if 'request' in data else None
        version = data.get('request_version')
        if request_id and (not isinstance(version, str) or timezone.is_naive(datetime.fromisoformat(version))):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        reject('invalid_confirmation', 'The confirmation is invalid. Please review the reward again.')
    if data['customer'] != str(user.pk) or not constant_time_compare(data['session'], binding):
        reject('wrong_confirmation_owner', 'This confirmation belongs to a different customer or session.')
    return ConfirmationClaims(reward_id, intent, data['balance'], data['offer'], expiry, request_id, version)
