from django.urls import re_path
from channels.security.websocket import AllowedHostsOriginValidator
from . import consumers
from .consumers import NotificationConsumer


websocket_urlpatterns = [
    re_path(r'ws/chat/(?P<room_name>[^/]+)/$', AllowedHostsOriginValidator(consumers.ChatConsumer.as_asgi())),
    re_path(r'ws/notifications/(?P<username>\w+)/$', NotificationConsumer.as_asgi()),
]

