"""Isolated browser fixture server, never imported by project URLs.

Run from the repository root: venv/Scripts/python -B -m
mouse_force_first_step.customerpanel.browser_tests.server
Uses temporary SQLite, real Django auth/CSRF/Points/Feedback, deterministic
News/Weather fixtures, and the existing real Jamendo service. No live user writes.
"""
import atexit
import contextlib
import io
import logging
import json
from hashlib import sha256
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

os.environ['DJANGO_SETTINGS_MODULE'] = 'project_name.settings'
os.environ['DEBUG'] = 'False'
temporary = tempfile.TemporaryDirectory(prefix='customer-shell-browser-')
atexit.register(temporary.cleanup)
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from django.conf import settings
    settings.DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': str(Path(temporary.name) / 'test.sqlite3')}}
    settings.CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
    settings.CHANNEL_LAYERS = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}
    settings.ROOT_URLCONF = __name__
    settings.ALLOWED_HOSTS = ['127.0.0.1', 'localhost', 'testserver']
    settings.SESSION_COOKIE_SECURE = False
    settings.CSRF_COOKIE_SECURE = False
    settings.SECURE_SSL_REDIRECT = False
    settings.STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                         'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
    settings.PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
    import django
    django.setup()
logging.disable(logging.CRITICAL)

from django.contrib.auth import get_user_model, login
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.core.management import call_command
from django.core.wsgi import get_wsgi_application
from django.http import JsonResponse
from django.urls import include, path
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from .. import views
from ..models import CustomerPoints, Feedback, Notification


def news_fixture(category='all'):
    return {'unavailable': False, 'articles': [
        {'title': f'{category.title()} story {i}', 'description': 'A short article used to verify the existing layout and navigation.',
         'url': f'https://example.com/article/{i}', 'image': '', 'source': 'Browser Test News',
         'published_at': timezone.now()} for i in range(1, 7)]}


def weather_fixture(city):
    if city.lower() == 'unknown':
        return {'status': 'not_found'}
    if city.lower() == 'unavailable':
        return {'status': 'unavailable'}
    condition = {'label': 'Clear sky', 'icon': '\u2600'}
    return {'status': 'ok', 'location': {'name': city, 'region': 'Browser Test'},
            'current': {'temperature': 24, 'feels_like': 25, 'condition': condition,
                        'wind_speed': 8, 'precipitation': 0, 'rain': 0},
            'days': [{'date': timezone.now().date() + timedelta(days=i), 'condition': condition,
                      'maximum': 26, 'minimum': 15, 'precipitation_probability': 10} for i in range(7)]}


views.get_customer_news = news_fixture
views.get_customer_weather = weather_fixture


@csrf_exempt
def fixture(request):
    user = get_user_model().objects.get(username='shell-customer')
    if request.GET.get('login') == '1':
        login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    if request.method == 'POST':
        day = int(request.POST.get('day', '0'))
        CustomerPoints.objects.update_or_create(user=user, defaults={
            'total_points': int(request.POST.get('points', '0')), 'streak_days': day,
            'last_daily_claim_date': timezone.now().date() - timedelta(days=int(request.POST.get('ago', '0'))) if day else None,
            'day_7_bonus_awarded': request.POST.get('day7') == '1',
            'day_14_bonus_awarded': request.POST.get('day14') == '1',
        })
        if request.POST.get('feedback_reset') == '1':
            Feedback.objects.filter(user=user).delete()
    return JsonResponse({'feedback': list(Feedback.objects.filter(user=user).values('message', 'rating', 'country'))})


urlpatterns = [path('__fixture__/', fixture), path('', include('project_name.urls'))]


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


if __name__ == '__main__':
    # Optional normalized REAL Jamendo metadata for repeatable network/media tests.
    # The production selector and endpoint are still used; no generated audio.
    if os.environ.get('CUSTOMER_BROWSER_MUSIC_FIXTURE'):
        from .. import music
        from django.core.cache import cache
        credential = music.config('JAMENDO_CLIENT_ID', default='').strip()
        tracks = json.loads(Path(os.environ['CUSTOMER_BROWSER_MUSIC_FIXTURE']).read_text(encoding='utf-8'))
        cache.set('customerpanel:jamendo:v1:' + sha256(credential.encode()).hexdigest(), tracks, 1800)
    call_command('migrate', verbosity=0, interactive=False)
    user = get_user_model().objects.create_user(username='shell-customer', role='customer', is_active=True)
    Notification.objects.create(user=user, message='Your account notification')
    server = make_server('127.0.0.1', 8766, StaticFilesHandler(get_wsgi_application()), ThreadedServer, QuietHandler)
    print('Browser fixture server ready at http://127.0.0.1:8766', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        from django.db import connections
        connections.close_all()
