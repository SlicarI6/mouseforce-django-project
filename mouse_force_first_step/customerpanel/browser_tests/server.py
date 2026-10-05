"""Isolated browser fixture server, never imported by project URLs.

Run from the repository root: venv/Scripts/python -B -m
mouse_force_first_step.customerpanel.browser_tests.server
Uses temporary SQLite, real Django auth/CSRF/Points/Feedback, deterministic
News/Weather fixtures, and the existing real Jamendo service. No live user writes.
"""
import atexit
import base64
import contextlib
import io
import logging
import json
from hashlib import sha256
import os
import re
import tempfile
from datetime import timedelta
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
from cryptography.fernet import Fernet

os.environ['DJANGO_SETTINGS_MODULE'] = 'project_name.settings'
os.environ['DEBUG'] = 'False'
temporary = tempfile.TemporaryDirectory(prefix='customer-shell-browser-')
atexit.register(temporary.cleanup)
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from django.conf import settings
    settings.DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': str(Path(temporary.name) / 'test.sqlite3')}}
    if os.environ.get('CUSTOMER_BROWSER_PG_DATABASE'):
        # Explicitly isolated loopback cluster only; never accept a connection URL.
        browser_database = os.environ['CUSTOMER_BROWSER_PG_DATABASE']
        if not re.fullmatch(r'test_discounts_browser_[a-f0-9]+', browser_database):
            raise ValueError('An isolated Discounts browser database is required.')
        settings.DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql',
            'NAME': browser_database, 'HOST': '127.0.0.1', 'PORT': '55439', 'USER': 'section_tests', 'PASSWORD': ''}}
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
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    # Ephemeral keys and digital inventory exist only in this temporary database.
    settings.REWARDS_CODE_ENCRYPTION_KEY = Fernet.generate_key().decode()
    settings.REWARDS_CODE_FINGERPRINT_KEY = base64.urlsafe_b64encode(os.urandom(32)).decode()
    settings.REWARDS_CODE_KEY_ID = 'isolated-browser-test'
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
from ..models import CustomerPoints, Feedback, Notification, Reward, Redemption, RewardFulfillment, RewardRequest, RewardCode, RedemptionEvent, CustomerSectionUnlock
from ..models import Discount, CustomerDiscountAccess, DiscountVote


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

# Opt-in only: all AI requests in browser verification stay in this isolated server.
if os.environ.get('CUSTOMER_BROWSER_ASSISTANT_FIXTURE') == '1':
    import fakeredis
    from types import SimpleNamespace
    from .. import assistant
    assistant_redis = fakeredis.FakeRedis(decode_responses=True)
    assistant.limiter_client = lambda: assistant_redis

    class AssistantProviderFixture:
        def __init__(self, **kwargs):
            self.responses = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def create(self, **options):
            question = options['input'][-1]['content']
            searched = bool(options.get('tools'))
            annotations = [SimpleNamespace(type='url_citation', url='https://example.com/official', title='Official test source')] if searched else []
            output = [SimpleNamespace(type='message', content=[SimpleNamespace(annotations=annotations)])]
            if searched:
                output.insert(0, SimpleNamespace(type='web_search_call'))
            return SimpleNamespace(status='completed', output_text='Test assistant reply: ' + question, output=output)

    assistant.openai.OpenAI = AssistantProviderFixture


@csrf_exempt
def fixture(request):
    user = get_user_model().objects.get(username='shell-customer')
    if request.GET.get('login') == '1':
        login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    if request.GET.get('login') in ('discount-other', 'discount-staff') and os.environ.get('CUSTOMER_BROWSER_DISCOUNT_FIXTURE') == '1':
        login(request, get_user_model().objects.get(username=request.GET['login']), backend='django.contrib.auth.backends.ModelBackend')
    if request.GET.get('login') == 'staff' and os.environ.get('CUSTOMER_BROWSER_REQUEST_FIXTURE') == '1':
        login(request, get_user_model().objects.get(username='browser-inventory-staff'), backend='django.contrib.auth.backends.ModelBackend')
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
        if 'rewards_active' in request.POST:
            Reward.objects.update(is_active=request.POST['rewards_active'] == '1')
        if request.POST.get('reward_change') == 'price':
            Reward.objects.filter(title='Browser test voucher').update(points_required=120)
        if os.environ.get('CUSTOMER_BROWSER_DISCOUNT_FIXTURE') == '1' and request.POST.get('discount_change'):
            deal = Discount.objects.get(brand=request.POST['discount_brand'])
            if request.POST['discount_change'] == 'price':
                deal.points_to_unlock_deal += 1
            elif request.POST['discount_change'] == 'expire':
                deal.ongoing = False
                deal.valid_until = timezone.now() - timedelta(seconds=1)
            deal.save()
    return JsonResponse({
        'feedback': list(Feedback.objects.filter(user=user).values('message', 'rating', 'country')),
        'digital_rewards': list(Reward.objects.filter(fulfillment_type__in=['voucher', 'external']).values('id', 'fulfillment_type')),
        'fulfillment_rewards': list(Reward.objects.filter(title__startswith='Browser fulfillment').values('id', 'fulfillment_type', 'stock_remaining')),
        'fulfillments': RewardFulfillment.objects.filter(redemption__user=user).count(),
        'redemptions': Redemption.objects.filter(user=user).count(),
        'refunds': list(Redemption.objects.filter(user=user, refunded_at__isnull=False).values('id', 'refunded_points', 'stock_reserved_quantity', 'stock_restored_at', 'balance_after')),
        'refund_events': RedemptionEvent.objects.filter(redemption__user=user, event_type='refunded').count(),
        'assigned_codes': RewardCode.objects.filter(redemption__user=user).count(),
        'reward_requests': list(RewardRequest.objects.filter(user=user).values('id', 'status', 'approved_reward_id', 'redemption_id')),
        'points': CustomerPoints.objects.filter(user=user).values_list('total_points', flat=True).first(),
        'section_unlocks': list(CustomerSectionUnlock.objects.filter(user=user).values('section', 'points_spent', 'balance_after')),
        'discounts': list(Discount.objects.values('id', 'brand', 'title', 'points_to_unlock_deal')) if os.environ.get('CUSTOMER_BROWSER_DISCOUNT_FIXTURE') == '1' else [],
        'discount_access': list(CustomerDiscountAccess.objects.filter(user=user).values('discount_id', 'points_spent')) if os.environ.get('CUSTOMER_BROWSER_DISCOUNT_FIXTURE') == '1' else [],
        'discount_votes': list(DiscountVote.objects.filter(user=user).values('discount_id', 'value')) if os.environ.get('CUSTOMER_BROWSER_DISCOUNT_FIXTURE') == '1' else [],
    })


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
    if os.environ.get('CUSTOMER_BROWSER_UNLOCK_FIXTURE') != '1':
        # Old browser scenarios start with previously paid access, preserving
        # their existing balance expectations. This is never production data.
        from ..section_test_support import seed_paid_access
        seed_paid_access(user, sections=CustomerSectionUnlock.Section.values)
    # Stage 1 examples remain useful only as isolated browser-test seed data.
    # Customer views never import the demo helper or fall back to these records.
    from ..reward_demos import DEMO_REWARDS
    for item in DEMO_REWARDS:
        Reward.objects.create(title=item['title'], category=item['category'], points_required=item['points'],
            short_description='A public test reward.', full_description='Full reward information for browser verification.',
            terms='Test terms and conditions.', fulfillment_type='physical', stock_remaining=4, is_active=True,
            partner_name='Test Partner', information_url='https://example.com/reward-info',
            image_url='https://example.com/reward.png' if item['category'] == 'beauty' else '', image_alt='Reward selection',
            is_exclusive=item['badge'] == 'Exclusive', is_limited_time=item['badge'] == 'Limited time',
            valid_until=timezone.now() + timedelta(days=30))
    Notification.objects.create(user=user, message='Your account notification')
    if os.environ.get('CUSTOMER_BROWSER_DISCOUNT_FIXTURE') == '1':
        from ..section_test_support import seed_paid_access
        other = get_user_model().objects.create_user(username='discount-other', role='customer')
        seed_paid_access(other, sections=('discounts',))
        get_user_model().objects.create_user(username='discount-staff', role='customer', is_staff=True, is_superuser=True)
        fixtures = [
            ('Test Bistro', 'Second main for £1', 'food-drink', 'fixed', 5, 'United Kingdom'),
            ('Test Style', '30% off selected styles', 'fashion', 'percent', 0, 'United Kingdom'),
            ('Test Journey', '£8 off your first trip', 'travel', 'money', 20, 'France'),
            ('Test Beauty', '10% student discount', 'beauty', 'percent', 0, 'United Kingdom'),
            ('Test Games', 'Two games for one', 'tech-gaming', 'bogo', 10, 'United Kingdom'),
            ('Test Cinema', 'Free cinema upgrade', 'entertainment', 'free', 5, 'United Kingdom'),
            ('Test Escape', 'Weekend experience', 'travel', 'other', 5, 'Spain'),
        ]
        for index, (brand, title, category, kind, cost, country) in enumerate(fixtures):
            Discount.objects.create(brand=brand, title=title, category=category, deal_type=kind,
                value_label=title, short_description='A fictional deal for isolated browser testing.',
                details='Read the complete test offer before unlocking. No real retailer or purchase is involved.',
                eligibility='Selected items only. Check the retailer conditions.', country=country,
                usage_channels=['online', 'in-store'], terms_summary='Test data only. Subject to retailer availability.',
                promo_code=f'BROWSER-DEAL-CODE-{index}', official_url=f'https://example.test/deals/{index}',
                ongoing=index == 3, valid_until=None if index == 3 else timezone.now() + timedelta(days=10),
                points_to_unlock_deal=cost, active=True, featured=index == 0,
                search_keywords='weekend dining' if index == 0 else '', last_verified_at=timezone.now())
    if os.environ.get('CUSTOMER_BROWSER_FULFILLMENT_FIXTURE') == '1':
        for kind in ('physical', 'manual'):
            Reward.objects.create(title='Browser fulfillment ' + kind, category='shopping', points_required=100,
                short_description='An isolated fulfillment test reward.',
                full_description='Test the existing confirmation and fulfillment flow with fictional delivery details.',
                terms='Isolated test only. No real delivery or purchase.', fulfillment_type=kind,
                stock_remaining=2, max_redemptions_per_customer=None, is_active=True,
                requires_contact_details=kind == 'manual', requires_phone=kind == 'manual',
                fulfillment_instructions='We review and process your request manually. No delivery date is guaranteed.',
                country_code='GB' if kind == 'physical' else '')
        if os.environ.get('CUSTOMER_BROWSER_REQUEST_FIXTURE') == '1':
            manual = Reward.objects.get(title='Browser fulfillment manual')
            manual.access_scope = 'selected_customers'
            manual.save(update_fields=['access_scope'])
            manual.eligible_users.add(user)
    if os.environ.get('CUSTOMER_BROWSER_REDEMPTION_FIXTURE') == '1':
        from ..reward_inventory import import_private_inventory
        staff = get_user_model().objects.create_user(username='browser-inventory-staff', role='customer', is_staff=True, is_superuser=True)
        for kind in ('voucher', 'external'):
            reward = Reward.objects.create(title='Browser test ' + kind, category='shopping', points_required=100,
                short_description='Test-only reward for reviewing the customer flow.',
                full_description='Receive a private test voucher or partner benefit. This is isolated demonstration inventory.',
                terms='Test inventory only. One benefit per redemption. No real purchases or partner offers.',
                fulfillment_type=kind, is_active=True, max_redemptions_per_customer=None,
                partner_name='Demo Partner', city='London', country_code='GB',
                fulfillment_instructions='Reveal your private benefit after confirming your redemption.',
                valid_until=timezone.now() + timedelta(days=30))
            values = [f'BROWSER-TEST-VOUCHER-{i}' if kind == 'voucher' else f'https://partner.example/test-only-claim/{i}' for i in range(1, 5)]
            import_private_inventory(staff, reward.pk, values, expires_at=timezone.now() + timedelta(days=45))
    port = int(os.environ.get('CUSTOMER_BROWSER_PORT', '8766'))
    server = make_server('127.0.0.1', port, StaticFilesHandler(get_wsgi_application()), ThreadedServer, QuietHandler)
    print(f'Browser fixture server ready at http://127.0.0.1:{port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        from django.db import connections
        connections.close_all()
