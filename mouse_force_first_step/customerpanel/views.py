from datetime import date, timedelta
from dataclasses import asdict
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils.timezone import now
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.shortcuts import render, redirect
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
import openai
import os
from django.conf import settings
from .points import claim_daily_points, claim_streak_bonus, get_points_state
from .news import NEWS_CATEGORIES, get_customer_news, normalise_news_category
from .weather import get_customer_weather
from .music import get_random_music_track


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
def customer_dashboard(request):
    return render(request, 'customer_dashboard.html', {
        'room_name': request.user.username,
        'points_state': get_points_state(request.user)
        if request.user.is_active and request.user.role == 'customer' else None,
    })


@login_required(login_url='login_account_customer')
def customer_how_points_work(request):
    if not request.user.is_active or request.user.role != 'customer':
        raise PermissionDenied
    return render(request, 'customer_how_points_work.html', {
        'customer_active_page': 'how_points_work',
    })


@login_required(login_url='login_account_customer')
def customer_discounts(request):
    if not request.user.is_active or request.user.role != 'customer':
        raise PermissionDenied
    return render(request, 'customer_discounts.html', {
        'customer_active_page': 'discounts',
    })


@login_required(login_url='login_account_customer')
def customer_offers(request):
    if not request.user.is_active or request.user.role != 'customer':
        raise PermissionDenied
    return render(request, 'customer_offers.html', {
        'customer_active_page': 'offers',
    })


@login_required(login_url='login_account_customer')
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
