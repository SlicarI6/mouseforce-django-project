"""Server-side points operations. Call with the authenticated request.user."""

from dataclasses import dataclass
from datetime import date, timedelta, timezone as datetime_timezone

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from .models import CustomerPoints


DAILY_POINTS = 10
DAY_7_BONUS = 35
DAY_14_BONUS = 50


def get_points_state(user):
    """Read dashboard state without creating records, resetting, or awarding."""
    if not user.is_authenticated or not user.is_active or user.role != 'customer':
        raise PermissionDenied('Only active customers can access points.')

    points = CustomerPoints.objects.filter(user=user).first()
    today = timezone.localdate(timezone=datetime_timezone.utc)
    last_claim = points.last_daily_claim_date if points else None
    stored_streak = points.streak_days if points else 0
    broken = last_claim is not None and last_claim < today - timedelta(days=1)
    future_date = last_claim is not None and last_claim > today
    displayed_streak = 0 if broken else stored_streak
    day7_awarded = points.day_7_bonus_awarded if points else False
    day14_awarded = points.day_14_bonus_awarded if points else False
    live_streak = last_claim is not None and not broken and not future_date
    day7_claimable = live_streak and stored_streak == 7 and not day7_awarded
    day14_claimable = live_streak and stored_streak == 14 and not day14_awarded
    daily_claimable = last_claim is None or last_claim < today

    if day7_claimable:
        bonus_label = 'Claim +35 Points'
        next_milestone = 'Day 7: +35 Points'
    elif day14_claimable:
        bonus_label = 'Claim +50 Points'
        next_milestone = 'Day 14: +50 Points'
    else:
        bonus_label = 'Bonus unavailable'
        if displayed_streak < 7:
            next_milestone = 'Next: Day 7 (+35 Points)'
        elif displayed_streak < 14:
            next_milestone = 'Next: Day 14 (+50 Points)'
        else:
            next_milestone = 'Next daily claim starts a new Day 1 cycle'

    return {
        'total_points': points.total_points if points else 0,
        'displayed_streak': displayed_streak,
        'daily_claimable': daily_claimable,
        'day_7_bonus_claimable': day7_claimable,
        'day_14_bonus_claimable': day14_claimable,
        'streak_broken': broken,
        'day_7_bonus_awarded': day7_awarded,
        'day_14_bonus_awarded': day14_awarded,
        'daily_label': 'Claim +10' if daily_claimable else ('Unavailable' if future_date else 'Claimed today'),
        'streak_label': f'{displayed_streak} ' + ('Day in a Row' if displayed_streak == 1 else 'Days in a Row'),
        'bonus_label': bonus_label,
        'next_milestone_label': next_milestone,
        'streak_status': 'Streak broken — next daily claim starts Day 1.' if broken else '',
    }


@dataclass(frozen=True)
class DailyClaimResult:
    status: str
    total_points: int
    streak_days: int
    last_daily_claim_date: date
    daily_points_awarded: int = 0
    bonus_points_awarded: int = 0


def _lock_customer(user):
    """Call inside an atomic reward operation, before reading points state."""
    if not user.is_authenticated:
        raise PermissionDenied('Only active customers can claim points.')

    User = get_user_model()
    try:
        locked_user = User.objects.select_for_update().get(pk=user.pk)
    except User.DoesNotExist as exc:
        raise PermissionDenied('Only active customers can claim points.') from exc
    if not locked_user.is_active or locked_user.role != 'customer':
        raise PermissionDenied('Only active customers can claim points.')
    return locked_user


@transaction.atomic
def claim_daily_points(user):
    """Claim once per UTC date; all eligibility is read under the user lock.

    Every reward operation must acquire this same user lock before reading or
    creating CustomerPoints. This also serializes simultaneous first claims.
    """
    locked_user = _lock_customer(user)

    points, _ = CustomerPoints.objects.get_or_create(user=locked_user)
    # Read the clock after locking, explicitly in UTC even if another timezone
    # has been activated for the current request.
    today = timezone.localdate(timezone=datetime_timezone.utc)
    last_claim = points.last_daily_claim_date

    if last_claim is not None and last_claim >= today:
        return DailyClaimResult(
            status='already_claimed' if last_claim == today else 'future_claim_date',
            total_points=points.total_points,
            streak_days=points.streak_days,
            last_daily_claim_date=last_claim,
        )

    consecutive = last_claim == today - timedelta(days=1)
    bonus = 0
    if consecutive:
        if points.streak_days == 7 and not points.day_7_bonus_awarded:
            bonus = DAY_7_BONUS
            points.day_7_bonus_awarded = True
        elif points.streak_days == 14 and not points.day_14_bonus_awarded:
            bonus = DAY_14_BONUS
            points.day_14_bonus_awarded = True

    # Settle an eligible old-cycle bonus before resetting the cycle. A missed
    # calendar day discards unpaid bonuses without changing accumulated points.
    points.total_points += bonus
    if not consecutive or points.streak_days == 14:
        points.streak_days = 1
        points.day_7_bonus_awarded = False
        points.day_14_bonus_awarded = False
    else:
        points.streak_days += 1

    points.total_points += DAILY_POINTS
    points.last_daily_claim_date = today
    points.save(update_fields=[
        'total_points',
        'last_daily_claim_date',
        'streak_days',
        'day_7_bonus_awarded',
        'day_14_bonus_awarded',
    ])
    return DailyClaimResult(
        status='claimed',
        total_points=points.total_points,
        streak_days=points.streak_days,
        last_daily_claim_date=today,
        daily_points_awarded=DAILY_POINTS,
        bonus_points_awarded=bonus,
    )


@dataclass(frozen=True)
class BonusClaimResult:
    status: str
    bonus_awarded: bool
    awarded_amount: int
    total_points: int
    streak_days: int
    day_7_bonus_awarded: bool
    day_14_bonus_awarded: bool
    last_daily_claim_date: date | None


@transaction.atomic
def claim_streak_bonus(user):
    """Claim an unpaid milestone without changing daily progress or its date.

    A milestone stays claimable on its UTC claim day and the following UTC
    day, until the consecutive daily claim settles it. After a missed day it
    expires, even if no daily claim has yet reset the stored streak.
    """
    locked_user = _lock_customer(user)
    points = CustomerPoints.objects.filter(user=locked_user).first()
    if points is None:
        return BonusClaimResult('not_eligible', False, 0, 0, 0, False, False, None)

    today = timezone.localdate(timezone=datetime_timezone.utc)
    last_claim = points.last_daily_claim_date
    amount = 0
    status = 'not_eligible'
    if last_claim is not None and last_claim > today:
        status = 'future_claim_date'
    elif last_claim is not None and last_claim < today - timedelta(days=1):
        status = 'expired'
    elif last_claim is not None and points.streak_days in (7, 14):
        field, reward = (
            ('day_7_bonus_awarded', DAY_7_BONUS)
            if points.streak_days == 7
            else ('day_14_bonus_awarded', DAY_14_BONUS)
        )
        if getattr(points, field):
            status = 'already_awarded'
        else:
            amount = reward
            points.total_points += amount
            setattr(points, field, True)
            points.save(update_fields=['total_points', field])
            status = 'claimed'

    return BonusClaimResult(
        status=status,
        bonus_awarded=amount > 0,
        awarded_amount=amount,
        total_points=points.total_points,
        streak_days=points.streak_days,
        day_7_bonus_awarded=points.day_7_bonus_awarded,
        day_14_bonus_awarded=points.day_14_bonus_awarded,
        last_daily_claim_date=points.last_daily_claim_date,
    )
