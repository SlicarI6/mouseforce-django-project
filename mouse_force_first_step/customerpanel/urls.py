from django.urls import path
from .views import customer_dashboard
from . import views
from .views import load_notifications
from .views import ask_openai


urlpatterns = [
    path('dashboard/', customer_dashboard, name='customer_dashboard'),
    path('how-points-work/', views.customer_how_points_work, name='customer_how_points_work'),
    path('discounts/', views.customer_discounts, name='customer_discounts'),
    path('offers/', views.customer_offers, name='customer_offers'),
    path('news/', views.customer_news, name='customer_news'),
    path('weather/', views.customer_weather, name='customer_weather'),
    path('music/track/', views.customer_music_track, name='customer_music_track'),
    path('points/claim-bonus/', views.claim_bonus, name='claim_streak_bonus'),
    path('points/claim-daily/', views.claim_daily, name='claim_daily_points'),
    path('dashboard/feedback/', views.customer_dashboard_feedback, name='customer_dashboard_feedback'),
    path('save-feedback/', views.save_feedback, name='save_feedback'),
    path('update-profile-picture/', views.update_profile_picture, name='update_profile_picture'),

    # 🔄 Chat: redirect către camera proprie (ex: /chat/me/ => /chat/username/)
    path('chat/me/', views.user_chat_redirect, name='user_chat_redirect'),

    # 🔄 Chat: cameră individuală (ex: /chat/paula/)
    path('chat/<str:room_name>/', views.chat_room, name='chat_room'),

    # 🔒 Admin vede toți userii
    path('admin/chat/users/', views.chat_user_list, name='chat_user_list'),
    
    path('load-notifications/', load_notifications, name='load_notifications'),
    path('mark-notifications-read/', views.mark_notifications_as_read, name='mark_notifications_as_read'),
    path("ask-openai/", ask_openai, name="ask_openai"),
    
]
