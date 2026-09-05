from django.urls import path
from django.views.generic import TemplateView

from . import views
from mouse_force_first_step.mycookieprivacy import views as cookie_views
from .views import contact_view

urlpatterns = [
    path('', views.index, name='homepage'),
    path('accept_cookies/', cookie_views.accept_cookies, name='accept_cookies'),
    path('contact/', contact_view, name='contact'),

    # Static template pages
    path('about/', TemplateView.as_view(template_name='about.html'), name='about'),
    path('privacy_policy/', TemplateView.as_view(template_name='privacy_policy.html'), name='privacy_policy'),
    path('terms/', TemplateView.as_view(template_name='terms.html'), name='terms'),
    path('startupguid/', TemplateView.as_view(template_name='startupguid.html'), name='startupguid'),
    path('marketingresources/', TemplateView.as_view(template_name='marketingresources.html'), name='marketingresources'),
]
