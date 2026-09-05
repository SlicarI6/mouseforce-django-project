from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.core.mail import send_mail
from django.conf import settings
from .models import Subscription, SubscriptionVerification
import random
from mouse_force_first_step.customerpanel.models import Message, Notification
import json
import openai

@login_required
def simple_user_dashboard(request):
    user_subscription = Subscription.objects.filter(user=request.user, is_active=True).first()
    return render(request, 'simple_user_dashboard.html', {
        'has_subscription': bool(user_subscription)
    })


@login_required
def send_verification_code(request):
    if request.method == 'POST':
        user = request.user
        code = str(random.randint(100000, 999999))

        verification, created = SubscriptionVerification.objects.get_or_create(user=user)
        verification.code = code
        verification.is_verified = False
        verification.save()

        send_mail(
            subject='Your Subscription Verification Code',
            message=f'Your code is: {code}',
            from_email=settings.DEFAULT_FROM_EMAIL,  # <- ✅ aici era problema: lipsea virgula
            recipient_list=[user.email],
            fail_silently=False,
        )

        return JsonResponse({'success': True, 'message': 'Verification code sent'})
    
    return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)


@csrf_exempt  # doar pentru testare, poți înlocui cu CSRF token în JS
@login_required
def verify_code_view(request):
    if request.method == 'POST':
        user = request.user
        code = request.POST.get('code')

        try:
            verification = SubscriptionVerification.objects.get(user=user)
            if verification.code == code:
                verification.is_verified = True
                verification.save()

                subscription, _ = Subscription.objects.get_or_create(user=user)
                subscription.is_active = True
                subscription.save()

                return JsonResponse({'success': True, 'message': 'Subscription activated!'})
            else:
                return JsonResponse({'success': False, 'message': 'Invalid code.'})
        except SubscriptionVerification.DoesNotExist:
            return JsonResponse({'success': False, 'message': 'No verification record found.'})

    return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)


@require_POST
@login_required
def unsubscribe_view(request):
    try:
        subscription = Subscription.objects.get(user=request.user)
        subscription.is_active = False
        subscription.save()
    except Subscription.DoesNotExist:
        pass
    return redirect('simple_user_dashboard')


@login_required
def chat_room(request, room_name):
    if not request.user.is_staff and request.user.username != room_name:
        return redirect('simple_chat_room', room_name=request.user.username)

    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)

    messages = Message.objects.filter(room_name=room_name).order_by('timestamp')

    return render(request, 'chat_room_simple_user.html', {
        'room_name': room_name,
        'messages': messages
    })

@login_required
def user_chat_redirect(request):
    return redirect('simple_chat_room', room_name=request.user.username)

@csrf_exempt
@login_required
def load_notifications(request):
    if request.method == "POST":
        data = json.loads(request.body.decode("utf-8"))
        if data.get("mark_read"):
            Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)

    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')[:10]
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
def ask_openai_simple_user(request):
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
                        "You are a helpful assistant for local services. "
                        "Reply friendly and short. "
                        "You represent a community that offers:\n"
                        "- Cleaning services\n"
                        "- Physical jobs\n"
                        "- Maintenance\n"
                        "- House help\n"
                        "Keep your tone warm and supportive."
                        "If you got stuck ask the user to go and leave a message to chatme."
                    )
                },
                {"role": "user", "content": question}
            ]
        )
        answer = response.choices[0].message.content
        return JsonResponse({"response": answer})