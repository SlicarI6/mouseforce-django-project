from django.urls import re_path
from . import consumers
from .consumers import NotificationConsumer


websocket_urlpatterns = [
    re_path(r'ws/chat/(?P<room_name>[^/]+)/$', consumers.ChatConsumer.as_asgi()),
    re_path(r'ws/notifications/(?P<username>\w+)/$', NotificationConsumer.as_asgi()),
]

