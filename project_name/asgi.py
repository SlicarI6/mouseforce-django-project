import os
import django
from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

# ✅ Inițializare settings + Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'project_name.settings')
django.setup()


from mouse_force_first_step.customerpanel.routing import websocket_urlpatterns as customerpanel_ws
from mouse_force_first_step.simpleuseragency.routing import websocket_urlpatterns as simpleuserpanel_ws

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": AuthMiddlewareStack(
        URLRouter(customerpanel_ws + simpleuserpanel_ws)
    ),
})