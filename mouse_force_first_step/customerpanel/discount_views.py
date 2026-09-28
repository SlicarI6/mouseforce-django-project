"""Customer Discount endpoints. Public catalogue projection and CSRF-only writes."""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, transaction
from django.http import JsonResponse, HttpResponseRedirect
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST

from . import discounts
from .navigation import active_customer, customer_session_token, customer_shell
from .points import get_points_state
from .section_access import has_section_access, locked_response

ERRORS = {
    'unavailable': 'This deal is no longer available to unlock or vote on.',
    'invalid_confirmation': 'Please reopen the confirmation and try again.',
    'expired_confirmation': 'Your confirmation expired. Please review it again.',
    'offer_changed': 'This deal has changed. Review its updated details before unlocking.',
    'balance_changed': 'Your Points balance changed. Please review it and confirm again.',
    'insufficient_points': 'You don’t have enough Points to unlock this deal yet.',
    'invalid_vote': 'Choose a positive, negative or cleared vote.',
}


def discount_action(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not active_customer(request):
            return JsonResponse({'error': 'customer_session'}, status=403 if request.user.is_authenticated else 401)
        if not has_section_access(request.user, 'discounts'):
            return locked_response(request, 'discounts')
        return view(request, *args, **kwargs)
    return wrapped


def response(request, payload, status=200):
    return JsonResponse({**payload, 'session': customer_session_token(request)}, status=status)


@login_required(login_url='login_account_customer')
@customer_shell('discounts')
@require_GET
def customer_discounts(request):
    if not active_customer(request):
        raise PermissionDenied
    context = discounts.catalogue(request.user, request.GET)
    context['customer_active_page'] = 'discounts'
    if request.headers.get('X-Discount-Results') == '1':
        return response(request, {'html': render_to_string('includes/customer_discount_results.html', context, request=request),
            'url': context['discount_url'], 'count': context['discount_count']})
    return render(request, 'customer_discounts.html', context)


@login_required(login_url='login_account_customer')
@customer_shell('discounts')
@require_GET
def customer_discount_detail(request, discount_id):
    if not active_customer(request):
        raise PermissionDenied
    deal = discounts.get_customer_deal(request.user, discount_id)
    rendered = render(request, 'customer_discount_detail.html', {
        'customer_active_page': 'discounts', 'deal': discounts.present_deal(deal, detail=True),
        'discount_balance': get_points_state(request.user)['total_points'],
    })
    rendered['Referrer-Policy'] = 'no-referrer'
    return rendered


@never_cache
@require_GET
@discount_action
def customer_discount_confirmation(request, discount_id):
    try:
        quote = discounts.create_deal_confirmation(request.user, discount_id,
            session_key=request.session.session_key or '', expected_offer=request.GET.get('offer'))
    except discounts.DealError as error:
        return response(request, {'error': error.code, 'message': ERRORS[error.code]}, 409)
    return response(request, quote)


def valid_post(request, fields):
    return (not request.GET and not request.FILES
        and request.content_type in ('application/x-www-form-urlencoded', 'multipart/form-data')
        and not set(request.POST) - (set(fields) | {'csrfmiddlewaretoken'})
        and all(len(request.POST.getlist(key)) == 1 for key in request.POST))


@transaction.non_atomic_requests
@never_cache
@require_POST
@csrf_protect
@sensitive_post_parameters('confirmation_token')
@discount_action
def customer_discount_unlock(request, discount_id):
    if not valid_post(request, {'confirmation_token'}):
        return response(request, {'error': 'invalid_request', 'message': 'Invalid deal unlock request.'}, 400)
    try:
        result = discounts.unlock_deal(request.user, discount_id, request.POST.get('confirmation_token', ''),
            session_key=request.session.session_key or '')
    except discounts.DealError as error:
        return response(request, {'error': error.code, 'message': ERRORS[error.code]}, 409)
    except DatabaseError:
        return response(request, {'error': 'unconfirmed', 'message': 'We could not confirm access. Please retry; you will not be charged twice.'}, 503)
    result.update(redirect_url=reverse('customer_discount_detail', args=[discount_id]), message='This deal is unlocked for your account.')
    # No code or retailer URL in this response. A separate GET reads committed access.
    return response(request, result)


@transaction.non_atomic_requests
@never_cache
@require_POST
@csrf_protect
@discount_action
def customer_discount_vote(request, discount_id):
    if not valid_post(request, {'value'}) or request.POST.get('value') not in ('-1', '0', '1'):
        return response(request, {'error': 'invalid_request', 'message': 'Invalid vote.'}, 400)
    try:
        result = discounts.set_vote(request.user, discount_id, int(request.POST['value']))
    except discounts.DealError as error:
        return response(request, {'error': error.code, 'message': ERRORS[error.code]}, 409)
    except DatabaseError:
        return response(request, {'error': 'unconfirmed', 'message': 'Your vote could not be confirmed. Please try again.'}, 503)
    if 'application/json' not in request.headers.get('Accept', ''):
        redirect = HttpResponseRedirect(reverse('customer_discount_detail', args=[discount_id]))
        redirect.status_code = 303
        return redirect
    return response(request, result)
