"""Discount catalogue, permanent access receipts and one vote per customer."""
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator, URLValidator
from django.db import models
from django.utils import timezone


class Discount(models.Model):
    class Category(models.TextChoices):
        FOOD_DRINK = 'food-drink', 'Food & Drink'
        FASHION = 'fashion', 'Fashion'
        BEAUTY = 'beauty', 'Beauty'
        TECH_GAMING = 'tech-gaming', 'Tech & Gaming'
        TRAVEL = 'travel', 'Travel'
        ENTERTAINMENT = 'entertainment', 'Entertainment'
        SHOPPING = 'shopping', 'Shopping'
        OTHER = 'other', 'Other'

    class DealType(models.TextChoices):
        PERCENT = 'percent', 'Percent off'
        MONEY = 'money', 'Money off'
        FIXED = 'fixed', 'Fixed price'
        FREE = 'free', 'Free'
        BOGO = 'bogo', '2-for-1 / Buy one get one'
        OTHER = 'other', 'Special offer / Other'

    CHANNELS = [('online', 'Online'), ('in-store', 'In-store'), ('app', 'App'), ('dine-in', 'Dine-in')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    brand = models.CharField(max_length=120)
    title = models.CharField(max_length=200)
    short_description = models.CharField(max_length=320, blank=True)
    details = models.TextField(blank=True)
    category = models.CharField(max_length=20, choices=Category.choices)
    deal_type = models.CharField(max_length=12, choices=DealType.choices, default=DealType.OTHER)
    value_label = models.CharField(max_length=80, help_text='For example: 30% off, From £5, or Second main for £1.')
    percentage_value = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal('0.01')), MaxValueValidator(100)])
    money_off_value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal('0.01'))])
    currency = models.CharField(max_length=3, default='GBP', validators=[RegexValidator(r'^[A-Z]{3}$')])
    eligibility = models.TextField(blank=True, help_text='Requirements such as student status, new customers, days or activation conditions.')
    country = models.CharField(max_length=80, blank=True, help_text='Use a consistent country name; blank means not specified.')
    region = models.CharField(max_length=160, blank=True)
    usage_channels = models.JSONField(default=list, blank=True)
    promo_code = models.CharField(max_length=160, blank=True, help_text='Retained server-side until this customer has free or purchased access.')
    official_url = models.URLField(max_length=2048, blank=True, validators=[URLValidator(schemes=['https'])])
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    ongoing = models.BooleanField(default=False)
    terms_summary = models.TextField(blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    image_url = models.URLField(max_length=2048, blank=True, validators=[URLValidator(schemes=['https'])])
    image_alt = models.CharField(max_length=200, blank=True)
    search_keywords = models.CharField(max_length=500, blank=True)
    points_to_unlock_deal = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=False)
    featured = models.BooleanField(default=False)
    free_published_at = models.DateTimeField(null=True, blank=True, editable=False,
        help_text='A deal published for free stays free. Create a separate deal for a paid version.')
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-featured', '-created_at', 'id']
        indexes = [models.Index(fields=['active', 'category'], name='discount_category_idx'),
                   models.Index(fields=['active', 'valid_until'], name='discount_expiry_idx')]
        constraints = [
            models.CheckConstraint(condition=models.Q(points_to_unlock_deal__gte=0), name='discount_nonnegative_cost'),
            models.CheckConstraint(condition=models.Q(percentage_value__isnull=True) | models.Q(percentage_value__gt=0, percentage_value__lte=100), name='discount_percent_range'),
            models.CheckConstraint(condition=models.Q(money_off_value__isnull=True) | models.Q(money_off_value__gt=0), name='discount_money_positive'),
            models.CheckConstraint(condition=models.Q(valid_from__isnull=True) | models.Q(valid_until__isnull=True) | models.Q(valid_until__gt=models.F('valid_from')), name='discount_date_order'),
            models.CheckConstraint(condition=models.Q(ongoing=False) | models.Q(valid_until__isnull=True), name='discount_ongoing_no_expiry'),
            models.CheckConstraint(condition=models.Q(active=False) | models.Q(ongoing=True) | models.Q(valid_until__isnull=False), name='discount_published_window'),
            models.CheckConstraint(condition=models.Q(free_published_at__isnull=True) | models.Q(points_to_unlock_deal=0), name='discount_published_free'),
        ]

    def clean(self):
        super().clean()
        if not isinstance(self.usage_channels, list) or any(not isinstance(v, str) or v not in dict(self.CHANNELS) for v in self.usage_channels):
            raise ValidationError({'usage_channels': 'Choose supported usage channels.'})
        from urllib.parse import urlsplit
        for name in ('official_url', 'image_url'):
            value = getattr(self, name)
            if value:
                try:
                    parsed = urlsplit(value)
                    valid = parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
                except ValueError:
                    valid = False
                if not valid:
                    raise ValidationError({name: 'Use an HTTPS URL without embedded credentials.'})
        if self.active and not (self.promo_code.strip() or self.official_url):
            raise ValidationError('Add a retailer code or official deal URL before activating this deal.')
        if self.free_published_at and self.points_to_unlock_deal:
            raise ValidationError({'points_to_unlock_deal': 'Published free deals stay free. Create a separate paid deal.'})

    def save(self, *args, **kwargs):
        if self.active and not self.points_to_unlock_deal and not self.free_published_at:
            self.free_published_at = timezone.now()
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = set(kwargs['update_fields']) | {'free_published_at'}
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.brand} — {self.title}'


class CustomerDiscountAccess(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='discount_access', editable=False)
    discount = models.ForeignKey(Discount, on_delete=models.PROTECT, related_name='customer_access', editable=False)
    points_spent = models.PositiveIntegerField(editable=False)
    balance_after = models.PositiveBigIntegerField(editable=False)
    unlocked_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ['-unlocked_at', 'id']
        constraints = [models.UniqueConstraint(fields=['user', 'discount'], name='discount_access_once'),
                       models.CheckConstraint(condition=models.Q(points_spent__gt=0), name='discount_access_paid')]

    def __str__(self):
        return f'Deal access {self.pk}'


class DiscountVote(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='discount_votes')
    discount = models.ForeignKey(Discount, on_delete=models.CASCADE, related_name='votes')
    value = models.SmallIntegerField(choices=[(-1, 'Negative'), (1, 'Positive')])
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'discount'], name='discount_vote_once'),
                       models.CheckConstraint(condition=models.Q(value__in=[-1, 1]), name='discount_vote_value')]

    def __str__(self):
        return f'Deal vote {self.pk}'
