from .navigation import active_customer, customer_session_token, customer_shell
from datetime import date, timedelta
from dataclasses import asdict
from functools import wraps
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, transaction
from django.utils.timezone import now
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from .forms import CitySearchForm, FeedbackForm
from .models import Feedback
from django.http import JsonResponse
from django.contrib import messages
import cloudinary.uploader  # ✅ Import necesar pentru Cloudinary
from django.contrib.auth import get_user_model
from django.http import HttpResponseForbidden
from django.contrib.admin.views.decorators import staff_member_required
from .models import Message
from .models import Notification
import json
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.utils.cache import add_never_cache_headers
from django.urls import reverse
import openai
import os
from django.conf import settings
from .points import claim_daily_points, claim_streak_bonus, get_points_state
from .news import NEWS_CATEGORIES, get_customer_news, normalise_news_category
from .weather import get_customer_weather
from .music import get_random_music_track
from .reward_catalogue import REWARD_CATEGORIES, published_rewards, present_reward
from .reward_catalogue import FULFILLMENT_LABELS
from .reward_confirmation import ConfirmationError, create_reward_confirmation, read_confirmation_token
from .reward_requests import RewardRequestForm, submission_token, submit_reward_request
from .reward_redemption import redeem_reward
from .reward_fulfillment import FULFILLMENT_FIELDS, FulfillmentForm, FulfillmentInputError
from types import SimpleNamespace
from .reward_private_access import PrivateBenefitUnavailable, get_private_reward_benefit
from .models import Redemption, RewardFulfillment, RewardRequest
from .section_access import section_required
from .section_views import customer_section_access, customer_section_unlock
from .discount_views import (customer_discounts, customer_discount_detail, customer_discount_confirmation,
    customer_discount_unlock, customer_discount_vote)


def _points_claim_response(request, claim):
    """Accept a claim action only; reward values come exclusively from Django."""
    if not request.user.is_authenticated:
        return JsonResponse({'status': 'unauthenticated', 'bonus_awarded': False, 'awarded_amount': 0}, status=401)

    invalid_payload = bool(request.GET)
    if request.content_type == 'application/json':
        try:
            payload = json.loads(request.body) if request.body else {}
        except (ValueError, UnicodeDecodeError):
            invalid_payload = True
        else:
            invalid_payload |= not isinstance(payload, dict) or bool(payload)
    elif request.content_type in ('application/x-www-form-urlencoded', 'multipart/form-data'):
        invalid_payload |= bool(set(request.POST) - {'csrfmiddlewaretoken'}) or bool(request.FILES)
    else:
        invalid_payload |= bool(request.body)

    if invalid_payload:
        return JsonResponse({'status': 'invalid_request', 'bonus_awarded': False, 'awarded_amount': 0}, status=400)
    try:
        # Keep the service's user lock until the response state is read, so a
        # competing claim cannot change it between the award and this snapshot.
        with transaction.atomic():
            result = claim(request.user)
            payload = asdict(result)
            payload['state'] = get_points_state(request.user)
    except PermissionDenied:
        return JsonResponse({'status': 'forbidden', 'bonus_awarded': False, 'awarded_amount': 0}, status=403)
    return JsonResponse(payload)


@require_POST
@csrf_protect
def claim_bonus(request):
    return _points_claim_response(request, claim_streak_bonus)


@require_POST
@csrf_protect
def claim_daily(request):
    return _points_claim_response(request, claim_daily_points)


@login_required
@customer_shell('dashboard')
def customer_dashboard(request):
    return render(request, 'customer_dashboard.html', {
        'room_name': request.user.username,
        'points_state': get_points_state(request.user)
        if request.user.is_active and request.user.role == 'customer' else None,
    })


@login_required(login_url='login_account_customer')
@customer_shell('how_points_work')
def customer_how_points_work(request):
    if not request.user.is_active or request.user.role != 'customer':
        raise PermissionDenied
    return render(request, 'customer_how_points_work.html', {
        'customer_active_page': 'how_points_work',
    })


@login_required(login_url='login_account_customer')
@customer_shell('rewards')
@require_GET
def customer_rewards(request):
    if not active_customer(request):
        raise PermissionDenied
    balance = get_points_state(request.user)['total_points']
    return render(request, 'customer_rewards.html', {
        'customer_active_page': 'rewards',
        'rewards_balance': balance,
        'reward_categories': REWARD_CATEGORIES,
        'rewards': [present_reward(row, balance) for row in published_rewards(request.user)],
    })


@login_required(login_url='login_account_customer')
@customer_shell('reward_detail')
@require_GET
def customer_reward_detail(request, reward_id):
    if not active_customer(request):
        raise PermissionDenied
    row = get_object_or_404(published_rewards(request.user), pk=reward_id)
    balance = get_points_state(request.user)['total_points']
    return render(request, 'customer_reward_detail.html', {
        'customer_active_page': 'rewards', 'rewards_balance': balance,
        'reward': present_reward(row, balance),
    })


# These messages are allowlisted; neither browser input nor private inventory is
# ever interpolated into an error, redirect URL or log.
REWARD_ACTION_ERRORS = {
    'request_unavailable': 'This request no longer has an offer available for acceptance.',
    'request_changed': 'The offer for this request changed. Please review it again.',
    'invalid_confirmation': 'Please review this reward again before redeeming.',
    'expired_confirmation': 'Your confirmation has expired. Please review the details again.',
    'wrong_confirmation_owner': 'Please review this reward again in your current account.',
    'offer_changed': 'This reward has changed. Review the updated details before confirming.',
    'balance_changed': 'Your Points balance has changed. Review your updated balance before confirming.',
    'insufficient_points': 'You do not have enough Points for this reward. Keep collecting and check back soon.',
    'reward_unavailable': 'This reward is no longer available to your account.',
    'out_of_stock': 'This reward is currently out of stock. No Points were spent.',
    'limit_reached': 'You have reached the redemption limit for this reward.',
    'redemption_not_enabled': 'Redemption for this type of reward is not available yet.',
    'inventory_unavailable': 'This reward is temporarily unavailable. Please try again later.',
}


def _private_reward_response(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        add_never_cache_headers(response)
        response['Referrer-Policy'] = 'no-referrer'
        response['X-Content-Type-Options'] = 'nosniff'
        return response
    return wrapped


def _reward_confirmation_context(request, reward_id, request_id=None):
    context = {'customer_active_page': 'rewards', 'reward_id': reward_id}
    try:
        quote = create_reward_confirmation(request.user, reward_id, session_key=request.session.session_key, request_id=request_id)
        context.update(confirmation=quote, reward=quote.details,
            fulfillment_label=FULFILLMENT_LABELS[quote.details['fulfillment_type']],
            fulfillment_form=FulfillmentForm(SimpleNamespace(**quote.details)),
            is_fulfillment=quote.details['fulfillment_type'] in ('physical', 'manual'),
            review_message=REWARD_ACTION_ERRORS.get(request.GET.get('review'), ''))
    except ConfirmationError as error:
        context['unavailable'] = REWARD_ACTION_ERRORS.get(error.code, REWARD_ACTION_ERRORS['reward_unavailable'])
    return context


REQUEST_PUBLIC_FIELDS = ('id', 'title', 'category', 'description', 'country_code', 'city', 'reference_url',
    'status', 'staff_response', 'approved_reward_id', 'redemption_id', 'created_at')


def _owned_request(request, request_id):
    if not active_customer(request):
        raise PermissionDenied
    return get_object_or_404(RewardRequest.objects.filter(user=request.user).values(*REQUEST_PUBLIC_FIELDS), pk=request_id)


@_private_reward_response
@login_required(login_url='login_account_customer')
@customer_shell('reward_requests')
@require_GET
def customer_reward_requests(request):
    if not active_customer(request):
        raise PermissionDenied
    from django.core.paginator import Paginator
    rows = RewardRequest.objects.filter(user=request.user).values('id', 'title', 'status', 'redemption_id', 'created_at').order_by('-created_at')
    return render(request, 'customer_reward_requests.html', {'customer_active_page': 'rewards',
        'requests_page': Paginator(rows, 20).get_page(request.GET.get('page'))})


@_private_reward_response
@login_required(login_url='login_account_customer')
@customer_shell('reward_request_new')
@csrf_protect
@sensitive_post_parameters()
@sensitive_variables()
def customer_reward_request_new(request):
    if not active_customer(request):
        raise PermissionDenied
    if request.method not in ('GET', 'POST'):
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(['GET', 'POST'])
    form = RewardRequestForm(initial={'submission_token': submission_token(request.user, request.session.session_key)})
    if request.method == 'POST':
        if (request.GET or request.FILES or request.content_type not in ('application/x-www-form-urlencoded', 'multipart/form-data')
                or set(request.POST) - (set(form.fields) | {'csrfmiddlewaretoken'})
                or any(len(request.POST.getlist(name)) != 1 for name in request.POST)):
            return JsonResponse({'error': 'Invalid reward request.'}, status=400)
        record, form = submit_reward_request(request.user, request.POST, session_key=request.session.session_key)
        if record:
            url = reverse('customer_reward_request_detail', args=[record.pk]) + '?submitted=1'
            if request.headers.get('Accept') == 'application/json':
                return JsonResponse({'redirect_url': url})
            response = redirect(url)
            response.status_code = 303
            return response
        if request.headers.get('Accept') == 'application/json':
            return JsonResponse({'error': 'Please check your request details. If the form expired, reopen it.',
                'field_errors': form.errors.get_json_data(escape_html=False)}, status=422)
    return render(request, 'customer_reward_request_new.html', {'customer_active_page': 'rewards', 'form': form}, status=422 if form.is_bound else 200)


@_private_reward_response
@login_required(login_url='login_account_customer')
@customer_shell('reward_request_detail')
@require_GET
def customer_reward_request_detail(request, request_id):
    record = _owned_request(request, request_id)
    offer = None
    if record['status'] == 'approved' and record['approved_reward_id'] and not record['redemption_id']:
        row = published_rewards(request.user).filter(pk=record['approved_reward_id']).first()
        if row:
            offer = present_reward(row, get_points_state(request.user)['total_points'])
    return render(request, 'customer_reward_request_detail.html', {'customer_active_page': 'rewards',
        'reward_request': record, 'offer': offer, 'request_status': 'Accepted' if record['redemption_id'] else RewardRequest.Status(record['status']).label,
        'submitted': request.GET.get('submitted') == '1'})


@_private_reward_response
@login_required(login_url='login_account_customer')
@customer_shell('reward_confirm')
@require_GET
def customer_reward_request_confirm(request, request_id):
    record = _owned_request(request, request_id)
    if record['redemption_id']:
        return redirect('customer_redemption_result', redemption_id=record['redemption_id'])
    context = {'customer_active_page': 'rewards', 'unavailable': REWARD_ACTION_ERRORS['request_unavailable']}
    if record['approved_reward_id']:
        context = _reward_confirmation_context(request, record['approved_reward_id'], request_id)
    context['reward_request_id'] = request_id
    return render(request, 'customer_reward_confirm.html', context)


def _confirmation_request_id(request):
    # Recover only authenticated signed context; never take request IDs from POST fields.
    try:
        return read_confirmation_token(request.user, request.POST.get('confirmation_token'),
            session_key=request.session.session_key, allow_expired=True).request_id
    except (ConfirmationError, PermissionDenied):
        return None


@_private_reward_response
@login_required(login_url='login_account_customer')
@customer_shell('reward_confirm')
@require_GET
def customer_reward_confirm(request, reward_id):
    if not active_customer(request):
        raise PermissionDenied
    return render(request, 'customer_reward_confirm.html', _reward_confirmation_context(request, reward_id))


@transaction.non_atomic_requests
@_private_reward_response
@require_POST
@csrf_protect
@customer_shell('reward_confirm')
@sensitive_post_parameters()
@sensitive_variables()
def customer_reward_redeem(request, reward_id):
    if not active_customer(request):
        return JsonResponse({'error': 'Please sign in with an active customer account.'}, status=403)
    invalid = (bool(request.GET) or bool(request.FILES)
        or request.content_type not in ('application/x-www-form-urlencoded', 'multipart/form-data')
        or set(request.POST) - ({'csrfmiddlewaretoken', 'confirmation_token'} | FULFILLMENT_FIELDS)
        or any(len(request.POST.getlist(name)) != 1 for name in request.POST)
        or len(request.POST.getlist('confirmation_token')) != 1)
    if invalid:
        return JsonResponse({'error': 'Invalid redemption request. Please review the reward again.'}, status=400)
    wants_json = request.headers.get('Accept') == 'application/json'
    try:
        result = redeem_reward(request.user, reward_id, request.POST['confirmation_token'],
            session_key=request.session.session_key,
            fulfillment_data={name: request.POST[name] for name in FULFILLMENT_FIELDS if name in request.POST})
    except FulfillmentInputError as error:
        if wants_json:
            return JsonResponse({'error': 'Please check your fulfillment details.',
                'field_errors': error.form.errors.get_json_data(escape_html=False)}, status=422)
        context = _reward_confirmation_context(request, reward_id, _confirmation_request_id(request))
        context['fulfillment_form'] = error.form
        return render(request, 'customer_reward_confirm.html', context, status=422)
    except PermissionDenied:
        return JsonResponse({'error': 'Please sign in with an active customer account.'}, status=403)
    except ConfirmationError as error:
        code = error.code if error.code in REWARD_ACTION_ERRORS else 'invalid_confirmation'
        request_id = _confirmation_request_id(request)
        review_url = (reverse('customer_reward_request_confirm', args=[request_id]) if request_id
            else reverse('customer_reward_confirm', args=[reward_id])) + '?review=' + code
        if wants_json:
            return JsonResponse({'error': REWARD_ACTION_ERRORS[code], 'review_url': review_url}, status=409)
        response = redirect(review_url)
        response.status_code = 303
        return response
    except DatabaseError:
        # A connection failure can leave commit outcome uncertain. The same
        # signed intent is safe to retry, including after a lost success response.
        return JsonResponse({'error': 'We could not confirm the result. Retry this confirmation to safely check it.'}, status=503)
    result_url = reverse('customer_redemption_result', args=[result.redemption_id])
    if wants_json:
        return JsonResponse({'redirect_url': result_url, 'replayed': result.replayed})
    response = redirect(result_url)
    response.status_code = 303
    return response


@_private_reward_response
@login_required(login_url='login_account_customer')
@customer_shell('redemption_result')
@require_GET
def customer_redemption_result(request, redemption_id):
    if not active_customer(request):
        raise PermissionDenied
    record = get_object_or_404(Redemption.objects.filter(user=request.user).values(
        'id', 'status', 'points_spent', 'balance_after', 'created_at', 'completed_at', 'refunded_at', 'refunded_points',
        'snapshot_title', 'snapshot_partner_name', 'snapshot_fulfillment_type',
        'snapshot_valid_until', 'snapshot_benefit_valid_until', 'snapshot_fulfillment_instructions',
    ), pk=redemption_id)
    fulfillment = None
    if record['snapshot_fulfillment_type'] in ('physical', 'manual'):
        fulfillment = RewardFulfillment.objects.filter(redemption_id=record['id'], redemption__user=request.user).values(
            'status', 'recipient_name', 'contact_email', 'contact_phone', 'address_line_1', 'address_line_2',
            'city', 'region', 'postal_code', 'country_code', 'request_details', 'customer_update').first()
        if fulfillment:
            fulfillment['status_label'] = RewardFulfillment.Status(fulfillment['status']).label
    return render(request, 'customer_redemption_result.html', {
        'customer_active_page': 'rewards', 'redemption': record,
        'fulfillment': fulfillment,
        'current_balance': get_points_state(request.user)['total_points'],
        'fulfillment_label': FULFILLMENT_LABELS.get(record['snapshot_fulfillment_type'], 'Reward'),
        'fulfillment_status': Redemption.Status(record['status']).label,
        'can_reveal': record['status'] == 'fulfilled' and record['completed_at'] is not None
            and record['refunded_at'] is None and record['snapshot_fulfillment_type'] in ('voucher', 'external'),
    })


@_private_reward_response
@login_required(login_url='login_account_customer')
@customer_shell('redemption_history')
@require_GET
def customer_redemption_history(request):
    if not active_customer(request):
        raise PermissionDenied
    from django.core.paginator import Paginator
    records = Redemption.objects.filter(user=request.user).values(
        'id', 'snapshot_title', 'snapshot_fulfillment_type', 'created_at', 'points_spent',
        'status', 'refunded_at', 'refunded_points', 'fulfillment__status').order_by('-created_at', '-id')
    page = Paginator(records, 20).get_page(request.GET.get('page'))
    for record in page.object_list:
        record['type_label'] = FULFILLMENT_LABELS.get(record['snapshot_fulfillment_type'], 'Reward')
        record['status_label'] = Redemption.Status(record['status']).label
        record['fulfillment_label'] = RewardFulfillment.Status(record['fulfillment__status']).label if record['fulfillment__status'] else ''
    return render(request, 'customer_redemption_history.html', {'customer_active_page': 'rewards', 'history_page': page})


@transaction.non_atomic_requests
@_private_reward_response
@require_POST
@csrf_protect
@sensitive_variables()
@section_required('rewards')
def customer_redemption_reveal(request, redemption_id):
    if not active_customer(request):
        return JsonResponse({'error': 'Please sign in with an active customer account.'}, status=403)
    if (request.GET or request.FILES or set(request.POST) - {'csrfmiddlewaretoken'}
            or request.content_type not in ('application/x-www-form-urlencoded', 'multipart/form-data')):
        return JsonResponse({'error': 'Invalid reveal request.'}, status=400)
    try:
        benefit = get_private_reward_benefit(request.user, redemption_id)
    except PermissionDenied:
        return JsonResponse({'error': 'This reward benefit is not available to your account.'}, status=404)
    except (PrivateBenefitUnavailable, DatabaseError):
        return JsonResponse({'error': 'Your private benefit is temporarily unavailable. Please try again later.'}, status=409)
    # This is the sole customer serialization of plaintext private inventory.
    return JsonResponse({'kind': benefit.payload_kind, 'value': benefit.value, 'expires_at': benefit.expires_at})


@login_required(login_url='login_account_customer')
@customer_shell('offers')
def customer_offers(request):
    if not request.user.is_active or request.user.role != 'customer':
        raise PermissionDenied
    return render(request, 'customer_offers.html', {
        'customer_active_page': 'offers',
    })


@login_required(login_url='login_account_customer')
@customer_shell('news')
def customer_news(request):
    if not request.user.is_active or request.user.role != 'customer':
        raise PermissionDenied
    category = normalise_news_category(request.GET.get('category', 'all'))
    news = get_customer_news(category)
    return render(request, 'customer_news.html', {
        'customer_active_page': 'news',
        'news': news,
        'news_categories': [
            {'slug': slug, 'label': config['label']}
            for slug, config in NEWS_CATEGORIES.items()
        ],
        'selected_category': category,
        'selected_category_label': NEWS_CATEGORIES[category]['label'],
        'featured_article': news['articles'][0] if news['articles'] else None,
        'latest_articles': news['articles'][1:],
    })


@login_required(login_url='login_account_customer')
@customer_shell('weather')
def customer_weather(request):
    if not request.user.is_active or request.user.role != 'customer':
        raise PermissionDenied
    form = CitySearchForm(request.GET if 'city' in request.GET else None)
    weather = None
    if form.is_bound and form.is_valid():
        weather = get_customer_weather(form.cleaned_data['city'])
    return render(request, 'customer_weather.html', {
        'customer_active_page': 'weather',
        'city_form': form,
        'weather': weather,
    })


@require_GET
@never_cache
def customer_music_track(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Please sign in to play music.'}, status=401)
    if not request.user.is_active or request.user.role != 'customer':
        return JsonResponse({'error': 'Music is available to customers only.'}, status=403)
    exclusions = request.GET.get('exclude', '')
    excluded_ids = exclusions.split(',') if exclusions else []
    if (
        set(request.GET) - {'exclude'} or len(request.GET.getlist('exclude')) > 1
        or len(exclusions) > 259 or len(excluded_ids) > 20
        or any(not value.isascii() or not value.isdigit() or not 1 <= len(value) <= 12 for value in excluded_ids)
    ):
        return JsonResponse({'error': 'Invalid music request.'}, status=400)
    track = get_random_music_track(set(excluded_ids))
    if track is None:
        return JsonResponse({'error': 'Music is temporarily unavailable. Please try again.'}, status=503)
    return JsonResponse({'track': track})


@login_required
def customer_dashboard_feedback(request):
    if request.method == 'POST':
        form = FeedbackForm(request.POST, request.FILES)
        if form.is_valid():
            feedback = form.save(commit=False)
            feedback.user = request.user
            feedback.save()
            return redirect('all_feedbacks')
    else:
        form = FeedbackForm()
    return render(request, 'customer_dashboard.html', {'form': form})


def feedback_view(request):
    today = date.today().isoformat()
    return render(request, 'customer_dashboard.html', {'today_date': today})


@csrf_exempt
@login_required
def save_feedback(request):
    if request.method == "POST":
        recent_feedbacks = Feedback.objects.filter(
            user=request.user,
            created_at__gte=now() - timedelta(hours=24)
        )
        if recent_feedbacks.count() >= 2:
            return JsonResponse({"status": "limit"})

        msg = request.POST.get("message", "").strip()
        rating = request.POST.get("rating")
        country = request.POST.get("country", "")
        focus = request.POST.get("development_focus", "")

        if msg:
            Feedback.objects.create(
                user=request.user,
                message=msg,
                rating=rating or None,
                country=country,
                development_focus=focus
            )
            return JsonResponse({"status": "ok"})
    return JsonResponse({"status": "error"}, status=400)


# ✅ View-ul corect pentru salvarea pozei în Cloudinary






@login_required
def update_profile_picture(request):
    print("📥 Upload request primit.")

    if request.method == 'POST' and 'profile_picture' in request.FILES:
        picture = request.FILES['profile_picture']
        print("🟡 Fișier primit:", picture)
        print("🟡 Tip fișier:", picture.content_type)

        if picture.content_type.startswith('image/'):
            try:
                result = cloudinary.uploader.upload(picture)
                print("🟢 Cloudinary upload result:", result)

                user = get_user_model().objects.get(pk=request.user.pk)
                print("👤 Utilizator găsit:", user.username)

                user.profile_picture = result['secure_url']
                user.save()

                print("✅ Poză salvată în user:", user.profile_picture)

            except Exception as e:
                print("❌ Eroare la upload sau salvare:", e)
        else:
            print("❌ Tip invalid, nu e imagine.")
    else:
        print("❌ POST lipsă sau fără fișier.")

    return redirect(
        'simple_user_dashboard'
        if request.user.role in ('simple', 'user')
        else 'customer_dashboard'
    )



@login_required
def chat_room(request, room_name):
    if not request.user.is_staff and request.user.username != room_name:
        return redirect('chat_room', room_name=request.user.username)
    
    
    # ✅ Marchează notificările ca citite când utilizatorul intră în cameră
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)

    messages = Message.objects.filter(room_name=room_name).order_by('timestamp')

    return render(request, 'chat_room.html', {
        'room_name': room_name,
        'messages': messages
    })


@login_required
def user_chat_redirect(request):
    return redirect('chat_room', room_name=request.user.username)


@staff_member_required
def chat_user_list(request):
    User = get_user_model()
    users = User.objects.exclude(is_superuser=True)
    return render(request, 'admin_chat_user_list.html', {'users': users})



@csrf_exempt
@login_required
def load_notifications(request):
    if request.method == "POST":
        data = json.loads(request.body.decode("utf-8"))
        if data.get("mark_read"):
            Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
            
    total_notifications = Notification.objects.filter(user=request.user).count()
    print("🟢 TOTAL notifications in DB:", total_notifications)

    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')[:5]

    # 🔵 Aici testăm ce primește browserul
    for n in notifications:
        print("🔵 Notification in slice:", n.message, "| created_at:", n.created_at, "| is_read:", n.is_read)

    data = [
        {
            'message': n.message,
            'created_at': n.created_at.strftime("%d.%m.%Y %H:%M"),
            'is_read': n.is_read
        }
        for n in notifications
    ]
    return JsonResponse({'notifications': data})



@require_POST
@login_required
def mark_notifications_as_read(request):
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return JsonResponse({"status": "ok"})


@csrf_exempt
@login_required
def ask_openai(request):
    if request.method == "POST":
        body = json.loads(request.body)
        question = body.get("question", "")

        client = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o",
            temperature=0.8,
            max_tokens=100,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful IT assistant. "
                        "Reply like a real human, in short messages. "
                        "Avoid repeating the same ideas. "
                        "You work for a platform that offers:\n"
                        "- automation scripts\n"
                        "- website development\n"
                        "- IT help and support\n"
                        "Many services are free to grow our user base. Complex ones may be paid."
                    )
                },
                {"role": "user", "content": question}
            ]
        )
        answer = response.choices[0].message.content
        return JsonResponse({"response": answer})


@require_GET
@never_cache
def customer_session(request):
    if not active_customer(request):
        return JsonResponse({'error': 'Customer session unavailable.'},
                            status=403 if request.user.is_authenticated else 401)
    return JsonResponse({'session': customer_session_token(request)})
