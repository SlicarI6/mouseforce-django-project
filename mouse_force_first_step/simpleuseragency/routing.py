from django.urls import re_path
from mouse_force_first_step.customerpanel.consumers import NotificationConsumer

websocket_urlpatterns = [
    re_path(r'ws/notifications/(?P<username>[\w\.]+)/$', NotificationConsumer.as_asgi()),

]