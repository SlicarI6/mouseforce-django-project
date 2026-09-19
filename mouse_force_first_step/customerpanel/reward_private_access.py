"""Post-commit, owner-only benefit access. No URL, UI, logging or external I/O.

The caller must enter in autocommit, after the OUTERMOST redemption transaction
has committed (or from an on_commit callback). An atomic request/view must not
call this service directly. Returning a RedemptionResult never reveals a value.
"""
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import json
from uuid import UUID

from cryptography.fernet import InvalidToken
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.debug import sensitive_variables

from .models import Redemption, RedemptionEvent, RewardCode
from .points import _lock_customer
from .reward_inventory import import_key_material, validate_private_values


class PrivateBenefitUnavailable(ValidationError):
    def __init__(self):
        super().__init__('This reward benefit is unavailable. Please contact support.', code='benefit_unavailable')


@dataclass(frozen=True)
class PrivateRewardBenefit:
    redemption_id: UUID
    payload_kind: str
    value: str = field(repr=False)
    expires_at: datetime | None = None


@sensitive_variables()
def _decode_benefit(code, redemption):
    """Authenticate ciphertext, its reward/type binding and keyed fingerprint."""
    try:
        cipher, fingerprint_key, key_id = import_key_material()
        encrypted = bytes(code.encrypted_payload)
        if code.encryption_key_id != key_id or not 0 < len(encrypted) <= 65536:
            raise ValueError
        payload = json.loads(cipher.decrypt(encrypted).decode('utf-8'))
        if not isinstance(payload, dict) or set(payload) != {'reward', 'kind', 'value'}:
            raise ValueError
        if payload['reward'] != str(redemption.reward_id) or payload['kind'] != code.payload_kind:
            raise ValueError
        value = validate_private_values([payload['value']], code.payload_kind)[0]
        if value != payload['value']:
            raise ValueError
        fingerprint = hmac.new(fingerprint_key, (code.payload_kind + '\0' + value).encode(), hashlib.sha256).hexdigest()
        if not constant_time_compare(code.payload_fingerprint, fingerprint):
            raise ValueError
    except (InvalidToken, ValidationError, ValueError, TypeError, UnicodeError, KeyError):
        raise PrivateBenefitUnavailable from None
    return PrivateRewardBenefit(redemption.pk, code.payload_kind, value, redemption.snapshot_benefit_valid_until)


@sensitive_variables()
def get_private_reward_benefit(user, redemption_id):
    # Checking both also rejects manually disabled autocommit. This must happen
    # before any reads/decryption, even when a nested atomic block has returned.
    if connection.in_atomic_block or not connection.get_autocommit():
        raise PrivateBenefitUnavailable
    try:
        redemption_id = UUID(str(redemption_id))
    except (ValueError, TypeError, AttributeError):
        raise PermissionDenied from None
    with transaction.atomic():
        current = _lock_customer(user)
        # Read-only access skips Points/Reward; remaining locks keep their order.
        redemption = Redemption.objects.select_for_update().filter(pk=redemption_id, user=current).first()
        if redemption is None:
            raise PermissionDenied
        kind = {'voucher': 'code', 'external': 'claim_link'}.get(redemption.snapshot_fulfillment_type)
        if (not kind or redemption.status != Redemption.Status.FULFILLED
                or redemption.completed_at is None or redemption.refunded_points or redemption.refunded_at):
            raise PrivateBenefitUnavailable
        if not RedemptionEvent.objects.filter(
            redemption=redemption, event_type='redeemed',
            points_delta=-redemption.points_spent, balance_after=redemption.balance_after,
        ).exists():
            raise PrivateBenefitUnavailable
        code = RewardCode.objects.select_for_update().filter(
            redemption=redemption, reward_id=redemption.reward_id, payload_kind=kind,
            is_active=True, assigned_at__isnull=False,
        ).first()
        at = timezone.now()
        if (code is None or (code.expires_at and code.expires_at <= at)
                or (redemption.snapshot_benefit_valid_until and redemption.snapshot_benefit_valid_until <= at)):
            raise PrivateBenefitUnavailable
    # All DB transactions have exited before plaintext is even constructed.
    return _decode_benefit(code, redemption)
