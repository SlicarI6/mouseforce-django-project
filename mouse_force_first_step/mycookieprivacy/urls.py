from django.urls import path
from . import views
from django.urls import include, path
urlpatterns = [
    path('', views.privacy_policy, name='privacy_policy'),

    path('run_migrations/', views.run_migrations),
]