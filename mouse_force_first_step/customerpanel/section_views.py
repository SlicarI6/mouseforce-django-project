"""Thin HTTP adapters for section access; GET never purchases or creates rows."""
from urllib.parse import urlsplit

from django.db import DatabaseError, transaction
from django.http import JsonResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse, resolve, Resolver404
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_http_methods

from .section_access import (PAGE_SECTIONS, SECTIONS, UnlockError, active_customer,
    create_unlock_confirmation, get_section_state, unlock_section, validate_section)

ERRORS = {
    'insufficient_points': 'You don’t have enough Points. You need 10 Points to unlock this section.',
    'balance_changed': 'Your balance has changed. Please review it and confirm again.',
    'expired_confirmation': 'Please review your balance and confirm again.',
    'invalid_confirmation': 'Please reopen the confirmation and try again.',
}


def _denied(request):
    return JsonResponse({'error': 'Customer session unavailable.'},
                        status=403 if request.user.is_authenticated else 401)


def _session_state(request, state):
    from .navigation import customer_session_token
    return {**state, 'session': customer_session_token(request)}


def safe_destination(section, value):
    fallback = reverse('customer_' + section)
    if not isinstance(value, str) or not value.startswith('/') or value.startswith('//') or '\\' in value:
        return fallback
    try:
        url = urlsplit(value)
        if url.netloc or url.scheme:
            return fallback
        name = resolve(url.path).url_name.removeprefix('customer_')
    except (ValueError, Resolver404, AttributeError):
        return fallback
    if name == 'reward_request_confirm':
        name = 'reward_confirm'
    return value if PAGE_SECTIONS.get(name) == section else fallback


@never_cache
@require_GET
def customer_section_access(request):
    if not active_customer(request.user):
        return _denied(request)
    return JsonResponse(_session_state(request, get_section_state(request.user)))


@transaction.non_atomic_requests
@never_cache
@require_http_methods(['GET', 'POST'])
@csrf_protect
@sensitive_post_parameters('confirmation_token')
def customer_section_unlock(request, section):
    if not active_customer(request.user):
        return _denied(request)
    validate_section(section)
    session_key = request.session.session_key or ''
    if request.method == 'GET':
        quote = create_unlock_confirmation(request.user, section, session_key=session_key)
        quote['state'] = _session_state(request, quote['state'])
        return JsonResponse(quote)
    allowed = {'csrfmiddlewaretoken', 'confirmation_token', 'next'}
    if (request.GET or request.FILES or request.content_type not in ('application/x-www-form-urlencoded', 'multipart/form-data')
            or set(request.POST) - allowed or any(len(request.POST.getlist(key)) != 1 for key in request.POST)):
        return JsonResponse({'error': 'invalid_request', 'message': 'Invalid unlock request.'}, status=400)
    wants_json = 'application/json' in request.headers.get('Accept', '')
    destination = safe_destination(section, request.POST.get('next', ''))
    try:
        result = unlock_section(request.user, section, request.POST.get('confirmation_token', ''), session_key=session_key)
    except UnlockError as error:
        if wants_json:
            return JsonResponse({'error': error.code, 'message': ERRORS[error.code],
                                 'state': _session_state(request, get_section_state(request.user))}, status=409)
        from .navigation import customer_session_token
        request.customer_shell_page = section
        request.customer_shell_session = customer_session_token(request)
        request.customer_section_locked = section
        quote = create_unlock_confirmation(request.user, section, session_key=session_key)
        request.customer_section_access = quote['state']
        return render(request, 'customer_section_locked.html', {
            'locked_section': section, 'section_label': SECTIONS[section], 'unlock_quote': quote,
            'unlock_error': ERRORS[error.code], 'unlock_next': destination,
        }, status=409)
    except DatabaseError:
        return JsonResponse({'error': 'unconfirmed', 'message': 'We could not confirm the unlock. Please retry; you will not be charged twice.'}, status=503)
    if wants_json:
        result['state'] = _session_state(request, result['state'])
        return JsonResponse({**result, 'message': f'{SECTIONS[section]} unlocked permanently.'})
    response = HttpResponseRedirect(destination)
    response.status_code = 303
    return response
