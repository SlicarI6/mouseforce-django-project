"""Shared redemption transactions and allocation for the four reward types.

Private values are never decrypted or returned here, including on an idempotent
replay. Customer views delegate to this service without duplicating its logic.
"""
from dataclasses import dataclass
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.debug import sensitive_variables

from .models import CustomerPoints, Redemption, RedemptionEvent, Reward, RewardFulfillment
from .reward_fulfillment import validate_fulfillment
from .points import _lock_customer
from .reward_confirmation import (
    eligible_private_codes, material_fingerprint, read_confirmation_token, reject, verify_availability,
    verify_limit_and_balance, verify_reward_access,
)
from .reward_inventory import import_key_material
from .reward_emails import send_reward_ready_email


class AllocationPlan:
    """Server-only extension point, prepared under User/Points/Reward locks.

    Preparation must not lock codes/fulfillment: reserve() runs after Redemption
    exists and is where those later locks belong. No external I/O belongs here.
    """
    stock_reserved_quantity = 0

    def reserve(self, redemption):
        raise NotImplementedError('Allocation is not implemented.')


class PrivateCodeAllocation(AllocationPlan):
    def __init__(self, reward, promised_expiry, key_id):
        self.reward = reward
        self.promised_expiry = promised_expiry
        self.key_id = key_id

    def reserve(self, redemption):
        # User/Points/Reward/Redemption precede this lock. Waiting is intentional:
        # the Reward lock serializes its inventory and preserves earliest expiry.
        codes = eligible_private_codes(
            self.reward, timezone.now(), promised_expiry=self.promised_expiry,
        ).filter(encryption_key_id=self.key_id)
        code = codes.select_for_update().only('id', 'expires_at').first()
        if code is None:
            reject('out_of_stock', 'No suitable private inventory is currently available.')
        assigned_at = timezone.now()  # Recheck time after waiting for the row lock.
        if code.expires_at is not None and code.expires_at <= assigned_at:
            reject('out_of_stock', 'This inventory expired. Please review the reward again.')
        code.redemption = redemption
        code.assigned_at = assigned_at
        code.save(update_fields=['redemption', 'assigned_at'])
        redemption.status = Redemption.Status.FULFILLED
        redemption.completed_at = assigned_at
        redemption.save(update_fields=['status', 'completed_at', 'updated_at'])


@sensitive_variables()
def _prepare_allocation(reward, *, at, promised_expiry, fulfillment_data=None):
    if reward.fulfillment_type in ('physical', 'manual'):
        return FulfillmentAllocation(reward, validate_fulfillment(reward, fulfillment_data))
    if fulfillment_data:
        validate_fulfillment(reward, fulfillment_data)  # Digital rewards accept no delivery fields.
    if reward.fulfillment_type not in ('voucher', 'external'):
        reject('redemption_not_enabled', 'Redemption is not available yet.')
    # Validate key provisioning without decrypting inventory before commit.
    # Unknown key IDs fail closed; key rotation needs an explicit keyring later.
    try:
        _, _, key_id = import_key_material()
    except ValidationError:
        reject('inventory_unavailable', 'This reward benefit is temporarily unavailable.')
    return PrivateCodeAllocation(reward, promised_expiry, key_id)


class FulfillmentAllocation(AllocationPlan):
    @sensitive_variables()
    def __init__(self, reward, details):
        self.reward = reward
        self.details = details
        self.stock_reserved_quantity = int(reward.stock_remaining is not None)

    @sensitive_variables()
    def reserve(self, redemption):
        if self.stock_reserved_quantity:
            # Reward is already locked. Guard the write too; never go negative.
            from django.db.models import F
            changed = Reward.objects.filter(pk=self.reward.pk, stock_remaining__gt=0).update(stock_remaining=F('stock_remaining') - 1)
            if changed != 1:
                reject('out_of_stock', 'This reward is currently out of stock.')
        RewardFulfillment.objects.create(redemption=redemption, **self.details)


@dataclass(frozen=True)
class RedemptionResult:
    redemption_id: UUID
    replayed: bool
    points_spent: int
    balance_after: int
    current_balance: int


def _snapshot(reward, promised_expiry):
    fields = ('title', 'fulfillment_type', 'partner_name', 'terms', 'fulfillment_instructions',
              'information_url', 'country_code', 'region', 'city', 'location_details',
              'valid_from', 'valid_until')
    values = {'snapshot_' + name: getattr(reward, name) for name in fields}
    values.update(snapshot_description=reward.full_description,
                  snapshot_reward_revision=reward.revision, snapshot_benefit_valid_until=promised_expiry)
    return values


@sensitive_variables()
@transaction.atomic
def redeem_reward(user, reward_id, token, *, session_key, fulfillment_data=None):
    # Same first lock as Daily/Streak; the extra Points lock never precedes User.
    current = _lock_customer(user)
    claims = read_confirmation_token(current, token, session_key=session_key, allow_expired=True)
    try:
        route_id = UUID(str(reward_id))
    except (ValueError, TypeError, AttributeError):
        reject('invalid_confirmation', 'The confirmation does not match this reward.')
    if claims.reward_id != route_id:
        reject('invalid_confirmation', 'The confirmation does not match this reward.')
    points = CustomerPoints.objects.select_for_update().filter(user=current).first()
    balance = points.total_points if points else 0
    try:
        reward = Reward.objects.select_for_update().get(pk=route_id)
    except (Reward.DoesNotExist, ValidationError):
        reject('reward_unavailable', 'This reward is not currently available.')
    request_record = None
    if claims.request_id:
        from .reward_requests import approved_request
        request_record = approved_request(current, claims.request_id, reward.pk, lock=True)
        if request_record.redemption_id:
            existing = Redemption.objects.select_for_update().get(pk=request_record.redemption_id, user=current, reward=reward)
            return RedemptionResult(existing.pk, True, existing.points_spent, existing.balance_after, balance)
    existing = Redemption.objects.select_for_update().filter(user=current, idempotency_key=claims.idempotency_key).first()
    if existing:
        if existing.reward_id != reward.pk:
            reject('invalid_confirmation', 'The confirmation does not match this reward.')
        return RedemptionResult(existing.pk, True, existing.points_spent, existing.balance_after, balance)
    # Only a committed, owned replay can bypass age/offer/balance eligibility.
    read_confirmation_token(current, token, session_key=session_key)
    if request_record and request_record.updated_at.isoformat() != claims.request_version:
        reject('request_changed', 'This request offer changed. Please review it again.')
    at = timezone.now()
    verify_reward_access(current, reward, at)
    if not constant_time_compare(material_fingerprint(reward, claims.promised_expiry), claims.fingerprint):
        reject('offer_changed', 'This reward changed. Please review the updated offer.')
    verify_limit_and_balance(current, reward, balance)
    if balance != claims.balance:
        reject('balance_changed', 'Your balance changed. Please review the updated confirmation.')
    verify_availability(reward, at, promised_expiry=claims.promised_expiry)
    allocation = _prepare_allocation(reward, at=at, promised_expiry=claims.promised_expiry, fulfillment_data=fulfillment_data)
    expected_quantity = int(reward.fulfillment_type in ('physical', 'manual') and reward.stock_remaining is not None)
    if not isinstance(allocation, AllocationPlan) or type(allocation.stock_reserved_quantity) is not int or allocation.stock_reserved_quantity != expected_quantity:
        raise ValueError('Invalid internal allocation plan.')
    after = balance - reward.points_required
    redemption = Redemption.objects.create(
        user=current, reward=reward, idempotency_key=claims.idempotency_key,
        points_spent=reward.points_required, balance_after=after,
        stock_reserved_quantity=allocation.stock_reserved_quantity, **_snapshot(reward, claims.promised_expiry),
    )
    points.total_points = after
    points.save(update_fields=['total_points'])
    allocation.reserve(redemption)
    RedemptionEvent.objects.create(redemption=redemption, actor=current, event_type='redeemed',
        points_delta=-reward.points_required, balance_after=after)
    if request_record:
        request_record.redemption = redemption
        request_record.save(update_fields=['redemption', 'updated_at'])
    if redemption.snapshot_fulfillment_type in ('voucher', 'external'):
        recipient, title, reference = current.email, redemption.snapshot_title, redemption.pk
        # Only new purchases reach here. Rollback discards this callback;
        # idempotent replays return above without scheduling another notice.
        transaction.on_commit(lambda: send_reward_ready_email(recipient, title, reference), robust=True)
    # Result is private-data-free. Any future reveal must wait for OUTERMOST commit.
    return RedemptionResult(redemption.pk, False, reward.points_required, after, after)
