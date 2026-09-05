from django.urls import path
from .views import simple_user_dashboard, send_verification_code
from .views import verify_code_view
from .views import unsubscribe_view
from . import views

urlpatterns = [
    path('dashboard/', simple_user_dashboard, name='simple_user_dashboard'),
    path('subscribe/send-code/', send_verification_code, name='send_verification_code'),
    path('subscribe/verify-code/', verify_code_view, name='verify_code'),
    path('unsubscribe/', unsubscribe_view, name='unsubscribe'),
     # 🔄 Chat: redirect către camera proprie
    path('chat/me/', views.user_chat_redirect, name='simple_user_chat_redirect'),

    # 🔄 Chat: cameră individuală
    path('chat/<str:room_name>/', views.chat_room, name='simple_chat_room'),

    # 🔄 Notificări
    path('load-notifications/', views.load_notifications, name='simple_load_notifications'),
    path('mark-notifications-read/', views.mark_notifications_as_read, name='simple_mark_notifications_as_read'),
    path('simple/ask-openai/', views.ask_openai_simple_user, name='ask_openai_simple_user'),
]
