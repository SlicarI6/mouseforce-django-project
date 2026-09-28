"""Permanent customer section access. All spending is serialized with Points."""
from functools import wraps
from uuid import uuid4

from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils.cache import add_never_cache_headers, patch_vary_headers
from django.utils.crypto import salted_hmac

from .models import CustomerPoints, CustomerSectionUnlock
from .points import _lock_customer, get_points_state

UNLOCK_COST = 10
CONFIRMATION_SECONDS = 15 * 60
TOKEN_SALT = 'customerpanel.section-unlock.v1'
SECTIONS = dict(CustomerSectionUnlock.Section.choices)
PAGE_SECTIONS = {name: name for name in SECTIONS}
PAGE_SECTIONS.update({name: 'rewards' for name in (
    'reward_detail', 'reward_confirm', 'redemption_result', 'redemption_history',
    'reward_requests', 'reward_request_new', 'reward_request_detail',
)})


class UnlockError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def active_customer(user):
    return user.is_authenticated and user.is_active and user.role == 'customer'


def validate_section(section):
    if section not in SECTIONS:
        raise Http404('Unknown customer section.')


def has_section_access(user, section):
    validate_section(section)
    return active_customer(user) and CustomerSectionUnlock.objects.filter(user=user, section=section).exists()


def get_section_state(user):
    if not active_customer(user):
        raise PermissionDenied
    unlocked = set(CustomerSectionUnlock.objects.filter(user=user).values_list('section', flat=True))
    return {'unlocked': sorted(unlocked), 'cost': UNLOCK_COST, 'points': get_points_state(user)}


def session_fingerprint(user, session_key):
    return salted_hmac(TOKEN_SALT, f'{user.pk}:{session_key}:{user.get_session_auth_hash()}').hexdigest()


def create_unlock_confirmation(user, section, *, session_key):
    validate_section(section)
    state = get_section_state(user)
    balance = state['points']['total_points']
    unlocked = section in state['unlocked']
    claims = {'v': 1, 'u': str(user.pk), 's': session_fingerprint(user, session_key),
              'section': section, 'cost': UNLOCK_COST, 'balance': balance, 'intent': str(uuid4())}
    return {'section': section, 'label': SECTIONS[section], 'cost': UNLOCK_COST,
            'balance': balance, 'balance_after': balance if unlocked else balance - UNLOCK_COST if balance >= UNLOCK_COST else None,
            'unlocked': unlocked, 'can_unlock': not unlocked and balance >= UNLOCK_COST,
            'token': signing.dumps(claims, salt=TOKEN_SALT), 'state': state}


@transaction.atomic
def unlock_section(user, section, token, *, session_key):
    validate_section(section)
    current = _lock_customer(user)
    points = CustomerPoints.objects.select_for_update().filter(user=current).first()
    # The common User lock also serializes the absence of an unlock/Points row.
    existing = CustomerSectionUnlock.objects.select_for_update().filter(user=current, section=section).first()
    if existing:
        return {'unlocked': True, 'already_unlocked': True, 'points_spent': 0,
                'unlock_id': str(existing.pk), 'section': section, 'state': get_section_state(current)}
    if not isinstance(token, str) or len(token) > 4096:
        raise UnlockError('invalid_confirmation')
    try:
        claims = signing.loads(token, salt=TOKEN_SALT, max_age=CONFIRMATION_SECONDS)
    except signing.SignatureExpired:
        raise UnlockError('expired_confirmation') from None
    except (signing.BadSignature, ValueError, TypeError):
        raise UnlockError('invalid_confirmation') from None
    if (not isinstance(claims, dict) or claims.get('v') != 1 or claims.get('u') != str(current.pk)
            or claims.get('s') != session_fingerprint(current, session_key)
            or claims.get('section') != section or claims.get('cost') != UNLOCK_COST):
        raise UnlockError('invalid_confirmation')
    balance = points.total_points if points else 0
    if balance < UNLOCK_COST:
        raise UnlockError('insufficient_points')
    if claims.get('balance') != balance:
        raise UnlockError('balance_changed')
    points.total_points = balance - UNLOCK_COST
    points.save(update_fields=['total_points'])
    receipt = CustomerSectionUnlock.objects.create(user=current, section=section,
        points_spent=UNLOCK_COST, balance_after=points.total_points, source='points')
    return {'unlocked': True, 'already_unlocked': False, 'points_spent': UNLOCK_COST,
            'unlock_id': str(receipt.pk), 'section': section, 'state': get_section_state(current)}


def locked_response(request, section):
    """No section content or provider calls occur before access is granted."""
    validate_section(section)
    if not active_customer(request.user):
        raise PermissionDenied
    if request.method != 'GET' or request.headers.get('X-Customer-Navigation') == '1' or 'application/json' in request.headers.get('Accept', ''):
        response = JsonResponse({'error': 'section_locked', 'section': section}, status=403)
    else:
        request.customer_section_locked = section
        response = render(request, 'customer_section_locked.html', {
            'customer_active_page': section, 'locked_section': section, 'section_label': SECTIONS[section],
            'unlock_quote': create_unlock_confirmation(request.user, section,
                session_key=getattr(getattr(request, 'session', None), 'session_key', '') or ''),
        }, status=403)
    response['X-Customer-Section-Locked'] = section
    add_never_cache_headers(response)
    patch_vary_headers(response, ['Cookie'])
    return response


def section_required(section):
    """For protected actions that do not use the HTML customer_shell decorator."""
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not active_customer(request.user):
                # Preserve the protected action's existing authentication response.
                return view(request, *args, **kwargs)
            if not has_section_access(request.user, section):
                return locked_response(request, section)
            return view(request, *args, **kwargs)
        return wrapped
    return decorate
