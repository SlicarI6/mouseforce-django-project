"""Read-only customer projections. Never load or return private inventory values."""
from urllib.parse import urlsplit

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import URLValidator
from django.db.models import Count, Exists, IntegerField, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import Reward, RewardCategory, RewardCode, Redemption

REWARD_CATEGORIES = (('all', 'All'), *RewardCategory.choices)
PUBLIC_FIELDS = (
    'id', 'title', 'short_description', 'full_description', 'category', 'image_url',
    'image_alt', 'points_required', 'fulfillment_type', 'partner_name', 'information_url',
    'country_code', 'region', 'city', 'location_details', 'valid_from', 'valid_until',
    'benefit_valid_until', 'terms', 'fulfillment_instructions', 'stock_remaining',
    'max_redemptions_per_customer', 'is_limited_time', 'is_exclusive',
)
FULFILLMENT_LABELS = {
    'voucher': 'Voucher or discount code', 'external': 'Partner offer',
    'physical': 'Physical product', 'manual': 'Personally arranged reward',
}


def published_rewards(user, *, at=None):
    if not user.is_authenticated or not user.is_active or user.role != 'customer':
        raise PermissionDenied
    at = at if at is not None else timezone.now()
    selected = Reward.eligible_users.through.objects.filter(reward_id=OuterRef('pk'), customuser_id=user.pk)
    usable_codes = RewardCode.objects.filter(
        reward_id=OuterRef('pk'), is_active=True, redemption__isnull=True,
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=at))
    # Count records only; no snapshots, fulfillment notes or audit data are read.
    usage = Redemption.objects.filter(reward_id=OuterRef('pk'), user_id=user.pk).order_by().values('reward_id').annotate(total=Count('pk')).values('total')
    return Reward.objects.filter(is_active=True).filter(
        Q(valid_from__isnull=True) | Q(valid_from__lte=at),
    ).filter(Q(valid_until__isnull=True) | Q(valid_until__gt=at)).alias(
        selected_customer=Exists(selected),
    ).filter(Q(access_scope='all_customers') | Q(access_scope='selected_customers', selected_customer=True)).annotate(
        has_usable_inventory=Exists(usable_codes),
        customer_usage=Coalesce(Subquery(usage, output_field=IntegerField()), 0),
    ).values(*PUBLIC_FIELDS, 'has_usable_inventory', 'customer_usage')


def public_https_url(value):
    if not value:
        return ''
    try:
        URLValidator(schemes=['https'])(value)
        parsed = urlsplit(value)
        if parsed.username or parsed.password or any(char.isspace() for char in value):
            return ''
    except (ValidationError, ValueError):
        return ''
    return value


def present_reward(row, balance):
    """Explicit public dictionary, not a model with accessible private relations."""
    reward = {field: row[field] for field in PUBLIC_FIELDS}
    in_stock = row['has_usable_inventory'] if row['fulfillment_type'] in ('voucher', 'external') else (row['stock_remaining'] is None or row['stock_remaining'] > 0)
    limit = row['max_redemptions_per_customer']
    limit_reached = limit is not None and row['customer_usage'] >= limit
    needed = max(0, row['points_required'] - balance)
    available = in_stock and not limit_reached and needed == 0
    reward.update(
        category_label=RewardCategory(row['category']).label,
        fulfillment_label=FULFILLMENT_LABELS[row['fulfillment_type']],
        image_url=public_https_url(row['image_url']),
        information_url=public_https_url(row['information_url']),
        points_needed=needed, in_stock=in_stock, limit_reached=limit_reached,
        available=available,
        status='Available' if available else 'Reward limit reached' if limit_reached else 'Currently unavailable' if not in_stock else 'Locked',
    )
    return reward
