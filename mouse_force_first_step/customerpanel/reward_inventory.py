"""Staff-only private inventory import. No allocation, reveal or Points writes."""
import base64
import hashlib
import hmac
import json
import re

from cryptography.fernet import Fernet
from decouple import config
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from urllib.parse import urlsplit

from .models import Reward, RewardCode

MAX_IMPORT_CODES = 200


def lock_staff_users(user, eligible_ids=()):
    """Inside atomic: user locks precede Reward/code locks, including Admin logs.

    Lock submitted eligible users before inserting M2M foreign keys, so a future
    customer redemption taking User -> Reward locks cannot deadlock with Admin.
    """
    if not user.is_authenticated:
        raise PermissionDenied
    ids = {user.pk}
    for value in eligible_ids:
        value = str(value)
        if value.isascii() and value.isdigit() and len(value) <= 19 and 0 < int(value) < 2 ** 63:
            ids.add(int(value))
    users = get_user_model().objects.select_for_update().filter(pk__in=ids).order_by('pk')
    actor = next((candidate for candidate in list(users) if candidate.pk == user.pk), None)
    if actor is None or not actor.is_active or not actor.is_staff:
        raise PermissionDenied
    return actor


@sensitive_variables()
def import_key_material():
    """Dedicated, persistent keys; never fall back to Django SECRET_KEY."""
    try:
        encryption = getattr(settings, 'REWARDS_CODE_ENCRYPTION_KEY', None)
        if encryption is None:
            encryption = config('REWARDS_CODE_ENCRYPTION_KEY', default='')
        fingerprint = getattr(settings, 'REWARDS_CODE_FINGERPRINT_KEY', None)
        if fingerprint is None:
            fingerprint = config('REWARDS_CODE_FINGERPRINT_KEY', default='')
        key_id = getattr(settings, 'REWARDS_CODE_KEY_ID', None)
        if key_id is None:
            key_id = config('REWARDS_CODE_KEY_ID', default='rewards-v1')
        cipher = Fernet(encryption.encode('ascii'))
        fingerprint_key = base64.b64decode(fingerprint.encode('ascii'), altchars=b'-_', validate=True)
        if len(fingerprint_key) != 32 or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', key_id):
            raise ValueError
        return cipher, fingerprint_key, key_id
    except (ValueError, TypeError, UnicodeError, AttributeError):
        raise ValidationError('Private inventory import is not configured. Check the dedicated Rewards keys and restart Django.') from None


@sensitive_variables()
def validate_private_values(values, kind):
    """Return canonical values; errors never include a code or private URL."""
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= MAX_IMPORT_CODES:
        raise ValidationError(f'Enter between 1 and {MAX_IMPORT_CODES} private values, one per line.')
    normalised = []
    for value in values:
        if not isinstance(value, str):
            raise ValidationError('Each private value must be text.')
        value = value.strip()
        if not value or len(value) > 2048 or not value.isprintable():
            raise ValidationError('Each private value must contain 1–2048 printable characters on one line.')
        if kind == RewardCode.PayloadKind.CLAIM_LINK:
            try:
                URLValidator(schemes=['https'])(value)
                parsed = urlsplit(value)
                if parsed.username or parsed.password or any(char.isspace() for char in value):
                    raise ValueError
            except (ValidationError, ValueError):
                raise ValidationError('Private claim links must be valid HTTPS URLs without embedded credentials.') from None
        normalised.append(value)
    if len(set(normalised)) != len(normalised):
        raise ValidationError('This batch contains duplicate private values. Nothing was imported.')
    return normalised


@sensitive_variables()
@transaction.atomic
def import_private_inventory(user, reward_id, values, *, expires_at=None, is_active=True):
    actor = lock_staff_users(user)
    if not actor.has_perm('customerpanel.add_rewardcode') or not actor.has_perm('customerpanel.change_reward'):
        raise PermissionDenied
    try:
        reward = Reward.objects.select_for_update().get(pk=reward_id)
    except (Reward.DoesNotExist, ValidationError):
        raise ValidationError('Choose an existing voucher or external reward.') from None
    kinds = {'voucher': RewardCode.PayloadKind.CODE, 'external': RewardCode.PayloadKind.CLAIM_LINK}
    if reward.fulfillment_type not in kinds:
        raise ValidationError('Only voucher and external rewards can receive private inventory.')
    if expires_at is not None and expires_at <= timezone.now():
        raise ValidationError('The inventory expiry must be in the future.')
    kind = kinds[reward.fulfillment_type]
    values = validate_private_values(values, kind)
    cipher, fingerprint_key, key_id = import_key_material()
    fingerprints = [hmac.new(fingerprint_key, (kind + '\0' + value).encode(), hashlib.sha256).hexdigest() for value in values]
    if RewardCode.objects.filter(reward=reward, payload_fingerprint__in=fingerprints).exists():
        raise ValidationError('One or more private values already exist for this reward. Nothing was imported.')
    rows = [RewardCode(
        reward=reward, payload_kind=kind, encryption_key_id=key_id,
        encrypted_payload=cipher.encrypt(json.dumps({'reward': str(reward.pk), 'kind': kind, 'value': value}).encode()),
        payload_fingerprint=fingerprint, expires_at=expires_at, is_active=is_active,
    ) for value, fingerprint in zip(values, fingerprints)]
    try:
        with transaction.atomic():
            RewardCode.objects.bulk_create(rows)
    except IntegrityError:
        raise ValidationError('Inventory could not be imported. Check for duplicate values and try again.') from None
    reward.revision += 1
    reward.save(update_fields=['revision', 'updated_at'])
    return reward, rows
