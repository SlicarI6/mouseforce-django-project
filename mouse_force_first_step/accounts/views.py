from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login
from django.contrib import messages
from .models import CustomUser  # Sau poți folosi: from django.contrib.auth import get_user_model
from django.contrib.auth import get_user_model
from django.contrib.auth import login
from .forms import CustomUserCreationForm
from datetime import datetime
from django.core.mail import send_mail
from django.http import JsonResponse
import json
import random
from django.utils.crypto import get_random_string
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from .forms import CaptchaTestForm

User = get_user_model()

def generate_unique_username(first_name, last_name, birth_date):
    base_username = f"{first_name.lower()}{last_name.lower()}"
    username = base_username

    birth_parts = []
    if birth_date:
        try:
            date_obj = datetime.strptime(birth_date, "%Y-%m-%d")
            birth_parts = [str(date_obj.year)[-2:], str(date_obj.day), str(date_obj.month)]
        except ValueError:
            pass  # format greșit sau necompletat

    suffix_index = 0
    while CustomUser.objects.filter(username=username).exists():
        suffix = birth_parts[suffix_index % len(birth_parts)] if birth_parts else str(suffix_index + 1)
        username = f"{base_username}{suffix}"
        suffix_index += 1

    return username




User = get_user_model()

def simple_user_signup_view(request):
    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')

        if User.objects.filter(email=email, role='customer').exists():
            messages.error(request, "❌ This email is already registered as a Customer IT Innovation. You cannot use it here.")
            return render(request, 'registration/simple_user_signup.html')

        if User.objects.filter(email=email, role='simple').exists():
            messages.error(request, "❌ Email already used, sign in.")
            return render(request, 'registration/simple_user_signup.html')

        try:
            validate_password(password)
        except ValidationError as e:
            messages.error(request, f"❌ {e.messages[0]}")
            return render(request, 'registration/simple_user_signup.html')

        base_username = email.split('@')[0][:13]
        username = base_username
        counter = 1
        while User.objects.filter(username=username).exists():
            suffix = str(counter)
            max_len = 13 - len(suffix)
            username = f"{base_username[:max_len]}{suffix}"
            counter += 1

        user = User(
            username=username,
            email=email,
            role='simple'
        )
        user.set_password(password)
        user.save()

        user.backend = 'django.contrib.auth.backends.ModelBackend'
        login(request, user)

        return redirect('simple_user_dashboard')

    return render(request, 'registration/simple_user_signup.html')






def customer_account_signup(request):
    if request.method == 'POST':
        email = request.POST.get('email')

        # Verificări email folosit deja
        if CustomUser.objects.filter(email=email, role='customer').exists(): 
            messages.error(request, "❌ This email is already registered as a Customer IT Innovation.")
            return render(request, 'registration/customer_account_signup.html', {
                'form': CustomUserCreationForm(),
                'show_customer_actions': True
            })

        if CustomUser.objects.filter(email=email, role='simple').exists():
            messages.error(request, "❌ This email is used for Skilled Worker Finder. Please use another email.")
            return render(request, 'registration/customer_account_signup.html', {
                'form': CustomUserCreationForm()
            })

        form = CustomUserCreationForm(request.POST, role='customer')

        if form.is_valid():
            password = form.cleaned_data.get('password1')

            try:
                validate_password(password)
            except ValidationError as e:
                form.add_error('password1', e)
                return render(request, 'registration/customer_account_signup.html', {
                    'form': form,
                    'show_customer_actions': True
                })

            user = form.save(commit=False)

            # Generare username unic max 13 caractere
            base_username = f"{user.first_name.lower()}{user.last_name.lower()}"[:13]
            username = base_username
            counter = 1
            while CustomUser.objects.filter(username=username).exists():
                suffix = str(counter)
                max_len = 13 - len(suffix)
                username = f"{base_username[:max_len]}{suffix}"
                counter += 1

            user.username = username
            user.role = 'customer'
            user.set_password(password)
            user.save()

            user.backend = 'mouse_force_first_step.accounts.auth_backends.EmailOrUsernameBackend'
            login(request, user)
            return redirect('customer_dashboard')

        else:
            return render(request, 'registration/customer_account_signup.html', {
                'form': form,
                'show_customer_actions': True
            })

    else:
        form = CustomUserCreationForm()
        return render(request, 'registration/customer_account_signup.html', {
            'form': form,
            'show_customer_actions': True
        })



def login_simple_user_view(request):
    request.session['selected_role'] = 'simple'

    if request.method == 'POST':
        email = request.POST.get('username')
        password = request.POST.get('password')

        user = authenticate(request, username=email, password=password)
        if user and user.role == 'simple':
            login(request, user)
            return redirect('simple_user_dashboard')
        else:
            # Dacă parola este greșită sau emailul greșit, rămâi pe password
            return render(request, 'registration/login_simple_user.html', {
                'role': 'simple',
                'step': 'password',
                'email_for_password': email,
                'error_message': "Incorrect password. Forgot password? Enter your email to reset.",
            })


    return render(request, 'registration/login_simple_user.html', {'role': 'simple'})





def login_account_customer_view(request):
    if request.method == 'POST':
        credential = request.POST.get('username', '')
        password = request.POST.get('password', '')

        username = None

        try:
            if '@' in credential:
                user_obj = User.objects.get(email=credential)
            else:
                user_obj = User.objects.get(username=credential)
            username = user_obj.username
        except User.DoesNotExist:
            username = None

        if username:
            user = authenticate(request, username=username, password=password)
        else:
            user = None

        if user is not None:
            login(request, user)
            return redirect('customer_dashboard')
        else:
            selected_method = 'email' if '@' in credential else 'username'
            context = {
                'error_message': "Incorrect credentials. Forgot your password?",
                'step': 'password',
                'selected_method': selected_method,
                'email_for_password': credential if selected_method == 'email' else '',
                'username_for_password': credential if selected_method == 'username' else '',
            }
            return render(request, 'registration/login_account_customer.html', context)

    return render(request, 'registration/login_account_customer.html')





def select_role_view(request):
    if request.method == 'POST':
        selected_role = request.POST.get('role')
        request.session['selected_role'] = selected_role

        if selected_role == 'simple':
            return redirect('login_simple_user')
        else:
            return redirect('login_account_customer')

    return render(request, 'init_session.html')



def simple_user_password_reset_request(request):
    if request.method == 'POST':
        email = request.POST.get('email')
        try:
            user = CustomUser.objects.get(email=email, role='simple')  # Folosim corect CustomUser
            # Creează o parolă temporară de 6 caractere
            new_password = get_random_string(8)
            user.set_password(new_password)
            user.save()

            # Trimite email
            send_mail(
                'Password Reset',
                f'Your new password is: {new_password}',
                'no-reply@example.com',  # poți schimba aici cu adresa ta reală
                [email],
                fail_silently=False,
            )
            messages.success(request, 'Check your email for the new password.')
        except CustomUser.DoesNotExist:
            messages.error(request, 'Email not found or invalid user.')

    return render(request, 'registration/password_reset_simple_user.html')


def check_email_exists_simple_user(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        email = data.get('email')
        user = User.objects.filter(email=email).first()
        exists = user is not None and user.role == 'simple'
        return JsonResponse({'exists': exists, 'role': user.role if user else None})


    
def check_email_exists_customer_account(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            email = data.get('email')
            if not email:
                return JsonResponse({'error': 'Missing email'}, status=400)

            user = User.objects.filter(email=email).first()

            if user:
                return JsonResponse({
                    'exists': True,
                    'role': user.role  # doar dacă ai `role` în model
                })
            else:
                return JsonResponse({'exists': False})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)


    
    
def check_username_exists_customer_account(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        username = data.get('username')
        exists = User.objects.filter(username=username, role='customer').exists()
        print("CHECK CUSTOMER USERNAME:", username, "| EXISTS:", exists)
        return JsonResponse({'exists': exists})

    
    
def recover_username_customer(request):
    if request.method == 'POST':
        import json
        data = json.loads(request.body)
        email = data.get('email')
        try:
            user = User.objects.get(email=email)
            if user.role == 'customer':
                return JsonResponse({'success': True, 'username': user.username})
            else:
                return JsonResponse({'success': False, 'error': 'This email belongs to a Simple User.'})
        except User.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'This email does not exist.'})
        
        
def google_account_not_registered(request):
    return render(request, 'registration/google_account_not_registered.html')

def post_login_redirect(request):
    user = request.user
    if hasattr(user, 'customer'):
        return redirect('customer_dashboard')  # dacă este Customer
    else:
        return redirect('google_account_not_registered')  # dacă NU este Customer



def simple_user_dashboard_view(request):
    return render(request, 'dashboard/simple_user_dashboard.html')



def test_captcha_view(request):
    if request.method == 'POST':
        form = CaptchaTestForm(request.POST)
        if form.is_valid():
            return render(request, 'success.html')
        else:
            # Dacă form nu este valid, returnezi cu erorile
            return render(request, 'registration/customer_account_signup.html', {
                'form': form,
                'show_customer_actions': True
            })
    else:
        form = CaptchaTestForm()
        return render(request, 'registration/customer_account_signup.html', {
            'form': form,
            'show_customer_actions': True
        })
