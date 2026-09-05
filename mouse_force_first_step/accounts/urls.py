from django.urls import path
from django.contrib.auth import views as auth_views
from . import views
from django.contrib.auth.views import LogoutView
from .views import (
    select_role_view,
    customer_account_signup,
    simple_user_signup_view,
    simple_user_password_reset_request,
    google_account_not_registered,
)
from captcha.views import captcha_refresh

urlpatterns = [
    path('login/simple-user/', views.login_simple_user_view, name='login_simple_user'),
    path('login/customer-account/', views.login_account_customer_view, name='login_account_customer'),
    path('simple-user-dashboard/', views.simple_user_dashboard_view, name='simple_user_dashboard'),

    path('logout/', LogoutView.as_view(next_page='homepage'), name='logout'),

    path('select-role/', select_role_view, name='select_role'),
    path('customer-signup/', customer_account_signup, name='customer_account_signup'),
    path('simple_user_signup/', simple_user_signup_view, name='simple_user_signup'),
    path('reset-password-simple/', simple_user_password_reset_request, name='simple_user_password_reset'),

    path('check-email/simple-user/', views.check_email_exists_simple_user, name='check_email_exists_simple_user'),
    path('accounts/check-email-customer/', views.check_email_exists_customer_account, name='check_email_exists_customer_account'),
    path('check-username-customer/', views.check_username_exists_customer_account, name='check_username_exists_customer_account'),
    path('recover-username-customer/', views.recover_username_customer, name='recover_username_customer'),

    path('not-registered/', google_account_not_registered, name='google_not_registered'),
    path('post-login/', views.post_login_redirect, name='post_login_redirect'),
    path('google-account-not-registered/', google_account_not_registered, name='google_account_not_registered'),

    # ✅ Flow complet pentru resetare parolă
    path('accounts/password-reset/', auth_views.PasswordResetView.as_view(
        template_name='registration/password_reset_simple_user.html'), name='password_reset'),

    path('accounts/password-reset/done/', auth_views.PasswordResetDoneView.as_view(
        template_name='registration/password_reset_done.html'), name='password_reset_done'),

    path('accounts/reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(
        template_name='registration/password_reset_confirm.html'), name='password_reset_confirm'),

    path('accounts/reset/done/', auth_views.PasswordResetCompleteView.as_view(
        template_name='registration/password_reset_complete.html'), name='password_reset_complete'),

    path("captcha/refresh/", captcha_refresh, name="captcha-refresh"),
]
