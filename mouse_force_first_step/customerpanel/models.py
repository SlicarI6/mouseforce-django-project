from django.db import models
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.core.validators import RegexValidator, URLValidator
import uuid

User = get_user_model()


class RewardCategory(models.TextChoices):
    BEAUTY = 'beauty', 'Beauty'
    FOOD_DRINK = 'food-drink', 'Food & Drink'
    TRAVEL = 'travel', 'Travel'
    ENTERTAINMENT = 'entertainment', 'Entertainment'
    TECH_GAMING = 'tech-gaming', 'Tech & Gaming'
    SHOPPING = 'shopping', 'Shopping'
    EXPERIENCES = 'experiences', 'Experiences'


class RewardType(models.TextChoices):
    VOUCHER = 'voucher', 'Voucher/code'
    EXTERNAL = 'external', 'Partner claim link'
    PHYSICAL = 'physical', 'Physical product'
    MANUAL = 'manual', 'Manual fulfillment'


class Reward(models.Model):
    """Catalogue data only. Saving a reward never changes customer points."""

    class AccessScope(models.TextChoices):
        ALL_CUSTOMERS = 'all_customers', 'All customers'
        SELECTED_CUSTOMERS = 'selected_customers', 'Selected customers'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=200)
    short_description = models.CharField(max_length=400)
    full_description = models.TextField()
    category = models.CharField(max_length=20, choices=RewardCategory.choices)
    image_url = models.URLField(max_length=2048, blank=True, validators=[URLValidator(schemes=['https'])])
    image_alt = models.CharField(max_length=200, blank=True)
    points_required = models.PositiveBigIntegerField()
    fulfillment_type = models.CharField(max_length=12, choices=RewardType.choices)
    partner_name = models.CharField(max_length=200, blank=True)
    information_url = models.URLField(max_length=2048, blank=True, validators=[URLValidator(schemes=['https'])])
    country_code = models.CharField(max_length=2, blank=True, validators=[RegexValidator(r'^[A-Z]{2}$')])
    region = models.CharField(max_length=100, blank=True)
    city = models.CharField(max_length=100, blank=True)
    location_details = models.CharField(max_length=300, blank=True)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    benefit_valid_until = models.DateTimeField(null=True, blank=True)
    terms = models.TextField()
    fulfillment_instructions = models.TextField(blank=True)
    requires_shipping_address = models.BooleanField(default=False, help_text='Manual rewards only; physical rewards always require delivery details.')
    requires_contact_details = models.BooleanField(default=False, help_text='Manual rewards only; physical rewards always require a name and email.')
    requires_region = models.BooleanField(default=False, help_text='Only valid for rewards requiring a shipping address.')
    requires_phone = models.BooleanField(default=False, help_text='Requires contact details. Never collected for voucher or external rewards.')
    stock_remaining = models.PositiveIntegerField(null=True, blank=True, help_text='Physical/manual inventory only. Blank means unlimited; zero means unavailable.')
    max_redemptions_per_customer = models.PositiveIntegerField(default=1, null=True, blank=True, help_text='Blank means repeatable without a per-customer limit. Enforced by the future locked redemption service.')
    is_active = models.BooleanField(default=False)
    is_limited_time = models.BooleanField(default=False)
    is_exclusive = models.BooleanField(default=False)
    access_scope = models.CharField(max_length=20, choices=AccessScope.choices, default=AccessScope.ALL_CUSTOMERS)
    eligible_users = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name='eligible_rewards', limit_choices_to={'role': 'customer', 'is_active': True})
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', 'id']
        indexes = [
            models.Index(fields=['is_active', 'category'], name='reward_catalog_idx'),
            models.Index(fields=['is_active', 'valid_until'], name='reward_expiry_idx'),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(points_required__gt=0), name='reward_positive_cost'),
            models.CheckConstraint(condition=models.Q(stock_remaining__isnull=True) | models.Q(stock_remaining__gte=0), name='reward_nonnegative_stock'),
            models.CheckConstraint(condition=models.Q(max_redemptions_per_customer__isnull=True) | models.Q(max_redemptions_per_customer__gt=0), name='reward_positive_customer_limit'),
            models.CheckConstraint(condition=models.Q(revision__gt=0), name='reward_positive_revision'),
            models.CheckConstraint(condition=models.Q(valid_from__isnull=True) | models.Q(valid_until__isnull=True) | models.Q(valid_until__gt=models.F('valid_from')), name='reward_valid_date_order'),
            models.CheckConstraint(condition=models.Q(benefit_valid_until__isnull=True) | models.Q(valid_from__isnull=True) | models.Q(benefit_valid_until__gt=models.F('valid_from')), name='reward_benefit_after_start'),
            models.CheckConstraint(condition=models.Q(benefit_valid_until__isnull=True) | models.Q(valid_until__isnull=True) | models.Q(benefit_valid_until__gte=models.F('valid_until')), name='reward_benefit_after_window'),
            models.CheckConstraint(condition=models.Q(category__in=RewardCategory.values), name='reward_valid_category'),
            models.CheckConstraint(condition=models.Q(fulfillment_type__in=RewardType.values), name='reward_valid_type'),
            models.CheckConstraint(condition=models.Q(access_scope__in=['all_customers', 'selected_customers']), name='reward_valid_access_scope'),
            models.CheckConstraint(condition=models.Q(stock_remaining__isnull=True) | models.Q(fulfillment_type__in=['physical', 'manual']), name='reward_single_inventory_source'),
            models.CheckConstraint(condition=models.Q(fulfillment_type__in=['physical', 'manual']) | models.Q(requires_shipping_address=False, requires_contact_details=False, requires_region=False, requires_phone=False), name='reward_delivery_types_only'),
            models.CheckConstraint(condition=models.Q(requires_region=False) | models.Q(fulfillment_type='physical') | models.Q(requires_shipping_address=True), name='reward_region_needs_shipping'),
            models.CheckConstraint(condition=models.Q(requires_phone=False) | models.Q(fulfillment_type='physical') | models.Q(requires_contact_details=True), name='reward_phone_needs_contact'),
        ]

    def __str__(self):
        return self.title


class Redemption(models.Model):
    """Purchase record, not a purchase operation. Snapshots are DB-immutable."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        FULFILLED = 'fulfilled', 'Fulfilled'
        CANCELLED = 'cancelled', 'Cancelled'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='reward_redemptions', editable=False)
    reward = models.ForeignKey(Reward, on_delete=models.PROTECT, related_name='redemptions', editable=False)
    idempotency_key = models.UUIDField(editable=False)
    points_spent = models.PositiveBigIntegerField(editable=False)
    balance_after = models.PositiveBigIntegerField(editable=False)
    snapshot_title = models.CharField(max_length=200, editable=False)
    snapshot_description = models.TextField(editable=False)
    snapshot_fulfillment_type = models.CharField(max_length=12, choices=RewardType.choices, editable=False)
    snapshot_partner_name = models.CharField(max_length=200, blank=True, editable=False)
    snapshot_terms = models.TextField(editable=False)
    snapshot_fulfillment_instructions = models.TextField(blank=True, editable=False)
    snapshot_information_url = models.URLField(max_length=2048, blank=True, editable=False)
    snapshot_country_code = models.CharField(max_length=2, blank=True, editable=False)
    snapshot_region = models.CharField(max_length=100, blank=True, editable=False)
    snapshot_city = models.CharField(max_length=100, blank=True, editable=False)
    snapshot_location_details = models.CharField(max_length=300, blank=True, editable=False)
    snapshot_valid_from = models.DateTimeField(null=True, blank=True, editable=False)
    snapshot_valid_until = models.DateTimeField(null=True, blank=True, editable=False)
    snapshot_benefit_valid_until = models.DateTimeField(null=True, blank=True, editable=False)
    snapshot_reward_revision = models.PositiveIntegerField(editable=False)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    refunded_points = models.PositiveBigIntegerField(default=0)
    refunded_at = models.DateTimeField(null=True, blank=True)
    stock_reserved_quantity = models.PositiveSmallIntegerField(default=0, editable=False)
    stock_restored_at = models.DateTimeField(null=True, blank=True, editable=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', 'id']
        indexes = [
            models.Index(fields=['user', '-created_at'], name='redemption_history_idx'),
            models.Index(fields=['user', 'reward'], name='redemption_customer_reward_idx'),
            models.Index(fields=['status', 'created_at'], name='redemption_queue_idx'),
        ]
        constraints = [
            models.UniqueConstraint(fields=['user', 'idempotency_key'], name='redemption_unique_attempt'),
            models.CheckConstraint(condition=models.Q(points_spent__gt=0), name='redemption_positive_cost'),
            models.CheckConstraint(condition=models.Q(snapshot_reward_revision__gt=0), name='redemption_positive_revision'),
            models.CheckConstraint(condition=models.Q(snapshot_fulfillment_type__in=RewardType.values), name='redemption_valid_type'),
            models.CheckConstraint(condition=models.Q(status__in=['pending', 'processing', 'fulfilled', 'cancelled']), name='redemption_valid_status'),
            models.CheckConstraint(condition=models.Q(snapshot_valid_from__isnull=True) | models.Q(snapshot_valid_until__isnull=True) | models.Q(snapshot_valid_until__gt=models.F('snapshot_valid_from')), name='redemption_valid_date_order'),
            models.CheckConstraint(condition=models.Q(refunded_points=0, refunded_at__isnull=True) | models.Q(refunded_points=models.F('points_spent'), refunded_at__isnull=False), name='redemption_full_refund_pair'),
            models.CheckConstraint(condition=models.Q(refunded_at__isnull=True) | models.Q(refunded_at__gte=models.F('created_at')), name='redemption_refund_date_order'),
            models.CheckConstraint(condition=models.Q(completed_at__isnull=True) | models.Q(completed_at__gte=models.F('created_at')), name='redemption_completed_order'),
            models.CheckConstraint(condition=models.Q(stock_reserved_quantity__in=[0, 1]), name='redemption_single_stock_unit'),
            models.CheckConstraint(condition=models.Q(stock_reserved_quantity=0) | models.Q(snapshot_fulfillment_type__in=['physical', 'manual']), name='redemption_stock_type'),
            models.CheckConstraint(condition=models.Q(stock_restored_at__isnull=True) | models.Q(stock_reserved_quantity=1, refunded_at__isnull=False, stock_restored_at__gte=models.F('refunded_at')), name='redemption_stock_restore_pair'),
        ]

    def __str__(self):
        return f'Redemption {self.pk}'


class RewardCode(models.Model):
    """Private inventory; no plaintext field, decryption, import or public API.

    The future importer must supply authenticated ciphertext and a keyed
    fingerprint. Key provisioning/encryption and reveal workflows are not part
    of Stage 2A. Never put codes in Reward descriptions or audit messages.
    """

    class PayloadKind(models.TextChoices):
        CODE = 'code', 'Voucher code'
        CLAIM_LINK = 'claim_link', 'Private claim link'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reward = models.ForeignKey(Reward, on_delete=models.PROTECT, related_name='private_codes')
    payload_kind = models.CharField(max_length=12, choices=PayloadKind.choices, editable=False)
    encrypted_payload = models.BinaryField(editable=False)
    encryption_key_id = models.CharField(max_length=64, editable=False)
    payload_fingerprint = models.CharField(max_length=64, editable=False, validators=[RegexValidator(r'^[0-9a-f]{64}$')])
    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    redemption = models.OneToOneField(Redemption, on_delete=models.PROTECT, null=True, blank=True, related_name='private_code', editable=False)
    assigned_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        indexes = [
            models.Index(fields=['reward', 'expires_at'], condition=models.Q(is_active=True, redemption__isnull=True), name='rewardcode_available_idx'),
        ]
        constraints = [
            models.UniqueConstraint(fields=['reward', 'payload_fingerprint'], name='rewardcode_unique_fingerprint'),
            models.CheckConstraint(condition=~models.Q(encrypted_payload=b''), name='rewardcode_payload_not_empty'),
            models.CheckConstraint(condition=~models.Q(encryption_key_id=''), name='rewardcode_key_not_empty'),
            models.CheckConstraint(condition=models.Q(payload_fingerprint__regex=r'^[0-9a-f]{64}$'), name='rewardcode_fingerprint_format'),
            models.CheckConstraint(condition=models.Q(payload_kind__in=['code', 'claim_link']), name='rewardcode_valid_kind'),
            models.CheckConstraint(condition=models.Q(redemption__isnull=True, assigned_at__isnull=True) | models.Q(redemption__isnull=False, assigned_at__isnull=False), name='rewardcode_assignment_pair'),
            models.CheckConstraint(condition=models.Q(assigned_at__isnull=True) | models.Q(assigned_at__gte=models.F('created_at')), name='rewardcode_assignment_date'),
            models.CheckConstraint(condition=models.Q(assigned_at__isnull=True) | models.Q(expires_at__isnull=True) | models.Q(assigned_at__lt=models.F('expires_at')), name='rewardcode_unexpired_assignment'),
        ]

    def __str__(self):
        return f'Reward code {self.pk}'


class RewardFulfillment(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        DISPATCHED = 'dispatched', 'Dispatched'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    redemption = models.OneToOneField(Redemption, on_delete=models.PROTECT, related_name='fulfillment')
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    recipient_name = models.CharField(max_length=200, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=32, blank=True)
    address_line_1 = models.CharField(max_length=255, blank=True)
    address_line_2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    region = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=32, blank=True)
    country_code = models.CharField(max_length=2, blank=True, validators=[RegexValidator(r'^[A-Z]{2}$')])
    request_details = models.TextField(blank=True)
    customer_update = models.TextField(blank=True)
    internal_notes = models.TextField(blank=True)
    tracking_reference = models.CharField(max_length=120, blank=True)
    tracking_url = models.URLField(max_length=2048, blank=True, validators=[URLValidator(schemes=['https'])])
    processed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='processed_reward_fulfillments')
    dispatched_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['status', 'created_at'], name='fulfillment_queue_idx')]
        constraints = [
            models.CheckConstraint(condition=models.Q(status__in=['pending', 'processing', 'dispatched', 'completed', 'cancelled']), name='fulfillment_valid_status'),
            models.CheckConstraint(condition=models.Q(dispatched_at__isnull=True) | models.Q(dispatched_at__gte=models.F('created_at')), name='fulfillment_dispatch_order'),
            models.CheckConstraint(condition=models.Q(completed_at__isnull=True) | models.Q(completed_at__gte=models.F('created_at')), name='fulfillment_completed_order'),
            models.CheckConstraint(condition=models.Q(dispatched_at__isnull=True) | models.Q(completed_at__isnull=True) | models.Q(completed_at__gte=models.F('dispatched_at')), name='fulfillment_delivery_order'),
        ]

    def __str__(self):
        return f'Reward fulfillment {self.pk}'


class RewardRequest(models.Model):
    """An unpriced request. Approval and saving never spend Points."""

    class Status(models.TextChoices):
        SUBMITTED = 'submitted', 'Submitted'
        IN_REVIEW = 'in_review', 'In review'
        APPROVED = 'approved', 'Approved'
        DECLINED = 'declined', 'Declined'
        CLOSED = 'closed', 'Closed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='reward_requests')
    title = models.CharField(max_length=200)
    description = models.TextField()
    category = models.CharField(max_length=20, choices=RewardCategory.choices, blank=True)
    country_code = models.CharField(max_length=2, blank=True, validators=[RegexValidator(r'^[A-Z]{2}$')])
    city = models.CharField(max_length=100, blank=True)
    reference_url = models.URLField(max_length=2048, blank=True, validators=[URLValidator(schemes=['https'])])
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SUBMITTED)
    staff_response = models.TextField(blank=True)
    internal_notes = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='reviewed_reward_requests')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    approved_reward = models.ForeignKey(Reward, on_delete=models.PROTECT, null=True, blank=True, related_name='customer_requests')
    redemption = models.OneToOneField(Redemption, on_delete=models.PROTECT, null=True, blank=True, related_name='original_request')
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', '-created_at'], name='rewardrequest_customer_idx'),
            models.Index(fields=['status', 'created_at'], name='rewardrequest_queue_idx'),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(category='') | models.Q(category__in=RewardCategory.values), name='rewardrequest_valid_category'),
            models.CheckConstraint(condition=models.Q(status__in=['submitted', 'in_review', 'approved', 'declined', 'closed']), name='rewardrequest_valid_status'),
            models.CheckConstraint(condition=~models.Q(status='approved') | models.Q(approved_reward__isnull=False), name='rewardrequest_approved_reward'),
            models.CheckConstraint(condition=models.Q(redemption__isnull=True) | models.Q(approved_reward__isnull=False), name='rewardrequest_redemption_offer'),
            models.CheckConstraint(condition=models.Q(reviewed_at__isnull=True) | models.Q(reviewed_at__gte=models.F('created_at')), name='rewardrequest_review_order'),
        ]

    def __str__(self):
        return f'Reward request {self.pk}'


class RedemptionEvent(models.Model):
    """Append-only audit records, enforced by the Rewards schema migration."""

    class EventType(models.TextChoices):
        REDEEMED = 'redeemed', 'Redeemed'
        PROCESSING = 'processing', 'Processing'
        FULFILLED = 'fulfilled', 'Fulfilled'
        CANCELLED = 'cancelled', 'Cancelled'
        REFUNDED = 'refunded', 'Refunded'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    redemption = models.ForeignKey(Redemption, on_delete=models.PROTECT, related_name='events', editable=False)
    event_type = models.CharField(max_length=12, choices=EventType.choices, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='reward_audit_events', editable=False)
    customer_message = models.TextField(blank=True, editable=False)
    internal_note = models.TextField(blank=True, editable=False)
    points_delta = models.BigIntegerField(default=0, editable=False)
    balance_after = models.PositiveBigIntegerField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ['created_at', 'id']
        indexes = [models.Index(fields=['redemption', 'created_at'], name='redemptionevent_history_idx')]
        constraints = [
            models.UniqueConstraint(fields=['redemption', 'event_type'], condition=models.Q(event_type__in=['redeemed', 'refunded']), name='redemptionevent_once_per_charge'),
            models.CheckConstraint(condition=models.Q(event_type='redeemed', points_delta__lt=0, balance_after__isnull=False) | models.Q(event_type='refunded', points_delta__gt=0, balance_after__isnull=False) | models.Q(event_type__in=['processing', 'fulfilled', 'cancelled'], points_delta=0), name='redemptionevent_valid_delta'),
        ]

    def __str__(self):
        return f'Redemption event {self.pk}'

class CustomerPoints(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        primary_key=True,
        on_delete=models.CASCADE,
    )
    total_points = models.PositiveBigIntegerField(default=0)
    last_daily_claim_date = models.DateField(null=True, blank=True)
    streak_days = models.PositiveSmallIntegerField(default=0)
    day_7_bonus_awarded = models.BooleanField(default=False)
    day_14_bonus_awarded = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(streak_days__gte=0, streak_days__lte=14),
                name='customerpoints_streak_range',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(streak_days=0, last_daily_claim_date__isnull=True)
                    | models.Q(streak_days__gte=1, last_daily_claim_date__isnull=False)
                ),
                name='customerpoints_claim_date',
            ),
            models.CheckConstraint(
                condition=models.Q(day_7_bonus_awarded=False) | models.Q(streak_days__gte=7),
                name='customerpoints_day7_eligible',
            ),
            models.CheckConstraint(
                condition=models.Q(day_14_bonus_awarded=False) | models.Q(streak_days=14),
                name='customerpoints_day14_eligible',
            ),
        ]


class Feedback(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    rating = models.IntegerField(choices=[(i, str(i)) for i in range(1, 6)], null=True, blank=True)
    country = models.CharField(max_length=100, blank=True)
    development_focus = models.CharField(max_length=200, blank=True)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    audio = models.FileField(upload_to='feedback_audio/', null=True, blank=True)

    def __str__(self):
        return f"Feedback from {self.user.username} - {self.created_at.strftime('%Y-%m-%d')}"
    
    
class Message(models.Model):
    room_name = models.CharField(max_length=255)
    sender = models.ForeignKey(get_user_model(), on_delete=models.CASCADE)
    content = models.TextField()
    timestamp = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.sender.username}: {self.content[:20]}"


class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Notificare pentru {self.user.username} - {'citită' if self.is_read else 'necitită'}"
