from mouse_force_first_step.mycookieprivacy.models import UserCookies
from django.utils.timezone import now
from django.shortcuts import render
from django.contrib import messages
from .models import ContactSubmission
from django.shortcuts import redirect

def index(request):
    user_ip = request.META.get('REMOTE_ADDR')
    already_set = UserCookies.objects.filter(user_ip=user_ip).exists()
    # return render(request, 'index.html', {'show_cookie_banner': True}) // daca doresti sa apara bannerul sau overlay again
    return render(request, 'index.html', {'show_cookie_banner': not already_set})


def contact_view(request):
    if request.method == 'POST':
        full_name = request.POST.get('full_name')
        business_email = request.POST.get('business_email')
        company = request.POST.get('company')
        current_locations = request.POST.get('current_locations')
        message_text = request.POST.get('message')
        consent = request.POST.get('consent') == 'on'

        # Salvează în DB
        ContactSubmission.objects.create(
            full_name=full_name,
            business_email=business_email,
            company=company,
            current_locations=current_locations,
            message=message_text,
            consent=consent
        )

        # ✅ Setează mesaj de succes
        messages.success(request, 'Thank you for your message! We will get back to you soon.')

        # ✅ Redirecționează la index
        return redirect('homepage')

    return render(request, 'contact.html')