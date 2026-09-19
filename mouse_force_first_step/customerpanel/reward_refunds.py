"""Staff-approved full refunds. No code recycling or external provider calls."""
from dataclasses import dataclass
from uuid import UUID

from django import forms
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .models import CustomerPoints, Reward, Redemption, RewardFulfillment, RedemptionEvent
from .reward_inventory import lock_staff_users

REFUND_REASONS = (
    ('unusable', 'Invalid or unusable benefit'),
    ('cannot_fulfill', 'Unable to fulfill'),
    ('cancellation', 'Approved cancellation before dispatch/completion'),
    ('returned', 'Verified returned physical item'),
)
STOCK_CHOICES = (
    ('none', 'Do not restore stock/capacity'),
    ('cancelled', 'Release a finite reservation before dispatch/completion'),
    ('returned', 'Restore a verified returned, sellable physical item'),
)
REFUND_SALT = 'customerpanel.staff-refund.v1'


class RefundForm(forms.Form):
    confirmation_token = forms.CharField(widget=forms.HiddenInput)
    reason = forms.ChoiceField(choices=REFUND_REASONS)
    stock_action = forms.ChoiceField(choices=STOCK_CHOICES, initial='none')
    internal_note = forms.CharField(max_length=2000, widget=forms.Textarea(attrs={'rows': 3}),
        label='Internal review note', help_text='Record the reason and any return/reservation verification. Do not enter private codes or claim links.')
    approved = forms.BooleanField(label='I have reviewed this case and approve a full refund of the original Points cost.')
    stock_verified = forms.BooleanField(required=False,
        label='If restoring stock/capacity: I verified that this specific unit is sellable or its reservation has genuinely been released.')


def refund_confirmation(actor, record, fulfillment=None):
    """Staff-only intent; no private inventory, delivery data or editable amount."""
    return signing.dumps({'actor': str(actor.pk), 'redemption': str(record.pk),
        'updated': record.updated_at.isoformat(),
        'fulfillment_updated': fulfillment.updated_at.isoformat() if fulfillment else None}, salt=REFUND_SALT)


def _verify_confirmation(actor, record, fulfillment, token):
    try:
        value = signing.loads(token, salt=REFUND_SALT, max_age=900)
        expected = {'actor': str(actor.pk), 'redemption': str(record.pk),
            'updated': record.updated_at.isoformat(),
            'fulfillment_updated': fulfillment.updated_at.isoformat() if fulfillment else None}
        if value != expected:
            raise ValueError
    except (signing.BadSignature, TypeError, ValueError):
        raise ValidationError('The redemption changed or this confirmation expired. Reload and review it again.') from None


@dataclass(frozen=True)
class RefundResult:
    redemption_id: UUID
    replayed: bool
    refunded_points: int
    current_balance: int
    stock_restored: bool


def _validate_stock(record, reward, fulfillment, action, verified):
    if action == 'none':
        return False
    if not verified:
        raise ValidationError('Verify the released reservation or sellable return before restoring stock.')
    if (record.stock_reserved_quantity != 1 or record.stock_restored_at is not None
            or record.snapshot_fulfillment_type not in ('physical', 'manual') or fulfillment is None):
        raise ValidationError('This redemption has no finite stock reservation available to restore.')
    if (reward.stock_remaining is None or reward.fulfillment_type != record.snapshot_fulfillment_type
            or reward.revision != record.snapshot_reward_revision):
        raise ValidationError('Inventory configuration changed. Manual inventory review is required; no refund or restoration was performed.')
    if action == 'cancelled':
        if (fulfillment.status not in ('pending', 'processing', 'cancelled')
                or fulfillment.dispatched_at or fulfillment.completed_at):
            raise ValidationError('Dispatched or completed rewards require a verified sellable physical return, not cancellation restocking.')
    elif action == 'returned':
        if (record.snapshot_fulfillment_type != 'physical'
                or not (fulfillment.dispatched_at or fulfillment.completed_at)):
            raise ValidationError('Only a verified returned physical item can use this stock action.')
    else:
        raise ValidationError('Choose a valid stock action.')
    if reward.stock_remaining >= 2 ** 31 - 1:
        raise ValidationError('Inventory requires manual review before restoring this unit.')
    return True


@sensitive_variables()
@transaction.atomic
def refund_redemption(actor, redemption_id, *, reason, stock_action='none', internal_note='',
                      approved=False, stock_verified=False, confirmation_token=''):
    """User(s) -> Points -> Reward -> Redemption -> Fulfillment.

    Code inventory is deliberately never changed or decrypted. Shared User and
    Reward locks serialize refunds with claims, purchases and fulfillment work.
    """
    identity = Redemption.objects.filter(pk=redemption_id).values('user_id', 'reward_id').first()
    if identity is None:
        raise ValidationError('This redemption is unavailable.')
    staff = lock_staff_users(actor, [identity['user_id']])
    if not staff.has_perm('customerpanel.change_redemption'):
        raise PermissionDenied
    points = CustomerPoints.objects.select_for_update().filter(user_id=identity['user_id']).first()
    reward = Reward.objects.select_for_update().get(pk=identity['reward_id'])
    record = Redemption.objects.select_for_update().get(pk=redemption_id)
    if points is None:
        raise ValidationError('The Points account requires manual review. Nothing was changed.')
    if record.refunded_at is not None:
        return RefundResult(record.pk, True, record.refunded_points, points.total_points, record.stock_restored_at is not None)
    fulfillment = RewardFulfillment.objects.select_for_update().filter(redemption=record).first()
    _verify_confirmation(staff, record, fulfillment, confirmation_token)
    if (approved is not True or reason not in dict(REFUND_REASONS) or stock_action not in dict(STOCK_CHOICES)
            or not isinstance(internal_note, str) or not internal_note.strip() or len(internal_note) > 2000):
        raise ValidationError('A verified reason, internal review note and explicit approval are required.')
    if record.refunded_points or not RedemptionEvent.objects.filter(redemption=record, event_type='redeemed',
            points_delta=-record.points_spent, balance_after=record.balance_after).exists():
        raise ValidationError('The original charge requires manual review. Nothing was changed.')
    if RedemptionEvent.objects.filter(redemption=record, event_type='refunded').exists():
        raise ValidationError('The refund record requires manual review. Nothing was changed.')
    physical_or_manual = record.snapshot_fulfillment_type in ('physical', 'manual')
    if physical_or_manual != (fulfillment is not None):
        raise ValidationError('The fulfillment record requires manual review. Nothing was changed.')
    if reason == 'cancellation' and (not fulfillment or fulfillment.dispatched_at or fulfillment.completed_at):
        raise ValidationError('This reward cannot be refunded as a cancellation before dispatch/completion.')
    if reason == 'cannot_fulfill' and (not fulfillment or fulfillment.completed_at):
        raise ValidationError('Choose a reviewed reason appropriate to this benefit.')
    if reason == 'returned' and (record.snapshot_fulfillment_type != 'physical' or not fulfillment
            or not (fulfillment.dispatched_at or fulfillment.completed_at)):
        raise ValidationError('This reason requires a verified returned physical item.')
    if stock_action == 'returned' and reason != 'returned':
        raise ValidationError('A sellable return requires the verified physical return reason.')
    if stock_action != 'none' and not staff.has_perm('customerpanel.change_reward'):
        raise PermissionDenied
    restore = _validate_stock(record, reward, fulfillment, stock_action, stock_verified is True)
    after = points.total_points + record.points_spent
    if after > 2 ** 63 - 1:
        raise ValidationError('The Points balance requires manual review. Nothing was changed.')
    at = timezone.now()
    points.total_points = after
    points.save(update_fields=['total_points'])
    record.refunded_points, record.refunded_at, record.status = record.points_spent, at, 'cancelled'
    fields = ['refunded_points', 'refunded_at', 'status', 'updated_at']
    if restore:
        reward.stock_remaining += 1
        reward.save(update_fields=['stock_remaining'])
        record.stock_restored_at = at
        fields.append('stock_restored_at')
    record.save(update_fields=fields)
    if fulfillment:
        fulfillment.status, fulfillment.processed_by = 'cancelled', staff
        fulfillment.customer_update = 'Your redemption has been refunded in full.'
        fulfillment.save(update_fields=['status', 'processed_by', 'customer_update', 'updated_at'])
    RedemptionEvent.objects.create(redemption=record, actor=staff, event_type='refunded',
        points_delta=record.points_spent, balance_after=after,
        customer_message='Your redemption has been refunded in full.',
        internal_note=f'{dict(REFUND_REASONS)[reason]}; stock: {dict(STOCK_CHOICES)[stock_action]}. {internal_note.strip()}')
    return RefundResult(record.pk, False, record.points_spent, after, restore)
