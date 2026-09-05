from django.urls import path
from .views import customer_dashboard
from . import views
from .views import load_notifications
from .views import ask_openai


urlpatterns = [
    path('dashboard/', customer_dashboard, name='customer_dashboard'),
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
