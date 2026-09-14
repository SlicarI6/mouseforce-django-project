"""Metadata for the opt-in customer shell; never used to authorize rewards."""

from functools import wraps

from django.http import JsonResponse
from django.utils.cache import add_never_cache_headers, patch_vary_headers
from django.utils.crypto import salted_hmac


def customer_session_token(request):
    session = getattr(request, 'session', None)
    key = getattr(session, 'session_key', '') or ''
    identity = f'{request.user.pk}:{key}:{request.user.get_session_auth_hash()}'
    return salted_hmac('customer-shell-session', identity).hexdigest()


def active_customer(request):
    return (request.user.is_authenticated and request.user.is_active
            and request.user.role == 'customer')


def customer_shell(page):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            eligible = active_customer(request)
            if request.headers.get('X-Customer-Navigation') == '1' and not eligible:
                return JsonResponse({'error': 'Customer session unavailable.'},
                                    status=403 if request.user.is_authenticated else 401)
            request.customer_shell_page = page
            request.customer_shell_session = customer_session_token(request) if eligible else ''
            response = view(request, *args, **kwargs)
            add_never_cache_headers(response)
            patch_vary_headers(response, ['Cookie'])
            return response
        return wrapped
    return decorate
