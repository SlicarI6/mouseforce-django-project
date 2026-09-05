
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST
from .models import UserCookies
from mouse_force_first_step.mycookieprivacy.models import UserCookies
from django.utils.timezone import now
import json
from django.core.management import call_command
# @csrf_exempt  # Temporar, dacă ai probleme cu CSRF (ideal să fie configurat corect)
def privacy_policy(request):
    if request.method == 'GET':
        return render(request, 'privacy_policy.html')  # sau numele corect al template-ului

    if request.method == 'POST':
        try:
            print("📥 Request Body (RAW):", request.body)  # ← Afișează datele primite în POST
            
            data = json.loads(request.body)
            advertising = data.get('advertising', False)
            analytics = data.get('analytics', False)
            personalisation = data.get('personalisation', False)
            user_ip = request.META.get('REMOTE_ADDR', 'unknown')
            print(f"IP: {user_ip}, Advertising: {advertising}, Analytics: {analytics}, personalisation{personalisation}")

            print("✅ Date salvate:", advertising, analytics, personalisation, user_ip)

            UserCookies.objects.create(
                user_ip=user_ip,
                accepted_cookies=True,
                functional_advertising=advertising,
                functional_analytics=analytics,
                functional_personalisation=personalisation,
                accepted_at=now()
            )

            return JsonResponse({'status': 'success', 'message': 'Preferences saved'})
        except Exception as e:
            print("⚠️ Eroare:", str(e))  # ← Afișează erorile în terminal
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

    return JsonResponse({'status': 'error', 'message': 'Invalid request'}, status=400)


@csrf_protect
@require_POST
def accept_cookies(request):
    print("✅ BODY:", request.body)
    print("✅ META:", request.META)
    print("🟢 FORM primit:", request.POST)
    
    try:
        print("Token primit:", request.META.get("HTTP_X_CSRFTOKEN"))

        if request.content_type == 'application/json':
            data = json.loads(request.body)
        else:
            data = request.POST

        accepted_cookies = data.get('accepted', False)
        print("✅ accepted_cookies:", accepted_cookies)

        user_ip = request.META.get('REMOTE_ADDR')

        UserCookies.objects.create(
            user_ip=user_ip,
            accepted_cookies=accepted_cookies in [True, 'true', 'True', '1', 'on'],
            functional_advertising=False,
            functional_analytics=False,
            functional_personalisation=True,
            accepted_at=now()
        )

        message = 'Cookies accepted' if accepted_cookies else 'Cookies rejected'
        return JsonResponse({'status': 'success', 'message': message})

    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

def run_migrations(request):
    call_command('migrate', 'mycookieprivacy')
    return JsonResponse({'status': 'ok', 'message': 'Migration applied'})