import io
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import quote

import requests
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import resolve, reverse

from . import music
from .views import customer_music_track


@override_settings(CACHES={
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'customer-music-tests',
    },
})
class CustomerMusicTests(SimpleTestCase):
    client_id = 'test-client-credential/private'

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.config = self.enterContext(patch.object(music, 'config', return_value=self.client_id))
        self.http_get = self.enterContext(patch.object(music.requests, 'get'))
        self.response = self.http_get.return_value.__enter__.return_value
        self.response.status_code = 200
        self.tracks = [{
            'id': str(number), 'name': f'Track {number}',
            'artist_name': f'Real Artist {number}',
            'album_name': 'This is not the artist',
            'audio': f'https://prod-1.storage.jamendo.com/?trackid={number}&format=mp31&from=app',
        } for number in range(1, 4)]
        self.payload = {'headers': {'status': 'success', 'code': 0}, 'results': self.tracks}
        self.response.json.return_value = self.payload
        self.url = reverse('customer_music_track')
        self.user = get_user_model()(username='customer', role='customer', is_active=True)

    def request(self, params=None, method='get'):
        request = getattr(RequestFactory(), method)(self.url, params or {})
        request.user = self.user
        return request

    @staticmethod
    def body(response):
        return json.loads(response.content)

    def test_route_is_customer_music_track(self):
        self.assertEqual(self.url, '/customer/music/track/')
        self.assertIs(resolve(self.url).func, customer_music_track)

    def test_active_customer_receives_safe_track_metadata_without_database_access(self):
        response = customer_music_track(self.request())
        self.assertEqual(response.status_code, 200)
        track = self.body(response)['track']
        self.assertEqual(set(track), {'id', 'name', 'artist_name', 'audio_url'})
        self.assertEqual(track['name'], f'Track {track["id"]}')
        self.assertEqual(track['artist_name'], f'Real Artist {track["id"]}')
        self.assertNotIn(self.client_id, response.content.decode())
        self.assertNotIn('api.jamendo.com', response.content.decode())

    def test_anonymous_request_is_rejected_even_with_cached_tracks(self):
        customer_music_track(self.request())
        self.http_get.reset_mock()
        request = self.request()
        request.user = AnonymousUser()
        self.assertEqual(customer_music_track(request).status_code, 401)
        self.http_get.assert_not_called()

    def test_inactive_and_non_customers_are_rejected_including_staff(self):
        for role, active, staff in (('customer', False, False), ('user', True, False), ('user', True, True)):
            with self.subTest(role=role, active=active, staff=staff):
                self.user.role = role
                self.user.is_active = active
                self.user.is_staff = staff
                self.user.is_superuser = staff
                self.assertEqual(customer_music_track(self.request()).status_code, 403)
        self.config.assert_not_called()
        self.http_get.assert_not_called()

    def test_only_get_is_accepted(self):
        for method in ('post', 'put', 'delete', 'patch'):
            with self.subTest(method=method):
                self.assertEqual(customer_music_track(self.request(method=method)).status_code, 405)
        self.http_get.assert_not_called()

    def test_browser_cannot_supply_api_options_or_credentials(self):
        for params in ({'client_id': 'browser'}, {'client_secret': 'browser'}, {'url': 'http://localhost'},
                       {'limit': 200}, {'order': 'random'}, {'user_id': 1}):
            with self.subTest(params=params):
                self.assertEqual(customer_music_track(self.request(params)).status_code, 400)
        self.config.assert_not_called()
        self.http_get.assert_not_called()

    def test_invalid_exclusions_are_rejected_before_api_request(self):
        for value in ('abc', '-1', '1,,2', '1.5', '1' * 13, '١', ','.join(str(n) for n in range(21)), ['1', '2']):
            with self.subTest(value=value):
                self.assertEqual(customer_music_track(self.request({'exclude': value})).status_code, 400)
        self.http_get.assert_not_called()

    def test_current_track_and_recent_failures_are_excluded(self):
        response = customer_music_track(self.request({'exclude': '1,2'}))
        self.assertEqual(self.body(response)['track']['id'], '3')

    def test_excluded_tracks_are_not_repeated_when_pool_is_exhausted(self):
        response = customer_music_track(self.request({'exclude': '1,2,3'}))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('track', self.body(response))

    def test_random_selection_happens_on_each_request_from_cached_pool(self):
        with patch.object(music, 'choice', side_effect=lambda tracks: tracks[0]) as choose:
            first = self.body(customer_music_track(self.request()))['track']
            second = self.body(customer_music_track(self.request({'exclude': first['id']})))['track']
        self.assertEqual(choose.call_count, 2)
        self.assertNotEqual(first['id'], second['id'])
        self.http_get.assert_called_once()

    def test_read_only_request_uses_client_id_and_fixed_server_options(self):
        customer_music_track(self.request())
        self.config.assert_called_once_with('JAMENDO_CLIENT_ID', default='')
        args, kwargs = self.http_get.call_args
        self.assertEqual(args, (music.JAMENDO_TRACKS_URL,))
        self.assertEqual(kwargs['params'], {
            'client_id': self.client_id, 'format': 'json', 'limit': 100,
            'audioformat': 'mp31', 'order': 'popularity_month',
            'groupby': 'artist_id', 'type': 'single albumtrack',
        })
        self.assertEqual(kwargs['timeout'], (3, 10))
        self.assertFalse(kwargs['allow_redirects'])
        self.assertNotIn('client_secret', kwargs['params'])

    def test_missing_client_id_has_safe_fallback_without_request(self):
        self.config.return_value = ''
        response = customer_music_track(self.request())
        self.assertEqual(response.status_code, 503)
        self.assertIn('temporarily unavailable', self.body(response)['error'])
        self.http_get.assert_not_called()

    def test_http_errors_and_redirects_have_generic_fallback(self):
        for status in (301, 302, 400, 401, 403, 429, 500, 503):
            with self.subTest(status=status):
                cache.clear()
                self.response.status_code = status
                response = customer_music_track(self.request())
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(self.client_id, response.content.decode())
        self.response.json.assert_not_called()

    def test_network_exceptions_do_not_expose_credentials_or_provider_details(self):
        for error in (requests.Timeout, requests.ConnectionError):
            with self.subTest(error=error):
                cache.clear()
                self.http_get.side_effect = error(self.client_id)
                with patch('sys.stdout') as stdout, patch('sys.stderr') as stderr:
                    response = customer_music_track(self.request())
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(self.client_id, response.content.decode())
                stdout.write.assert_not_called()
                stderr.write.assert_not_called()

    def test_invalid_json_has_safe_fallback(self):
        self.response.json.side_effect = ValueError(self.client_id)
        self.assertEqual(customer_music_track(self.request()).status_code, 503)

    def test_verbose_transport_logging_cannot_disclose_the_client_id(self):
        for logger_name in ('urllib3.connectionpool', 'urllib3.util.retry'):
            with self.subTest(logger=logger_name):
                logger = logging.getLogger(logger_name)
                output = io.StringIO()
                handler = logging.StreamHandler(output)
                with patch.object(logger, 'handlers', [handler]), patch.object(logger, 'propagate', False):
                    logger.warning('https://api.jamendo.com:443 "GET %s HTTP/1.1" 200',
                                   '/v3.0/tracks/?client_id=' + quote(self.client_id, safe=''))
                    logger.warning('Retrying request %s', '/v3.0/tracks/?client_id=' + self.client_id)
                    self.assertEqual(output.getvalue(), '')
                    logger.warning('Unrelated weather request')
                    self.assertIn('Unrelated weather request', output.getvalue())

    def test_malformed_payloads_and_api_errors_have_safe_fallback(self):
        for payload in (None, [], {}, {'headers': None, 'results': []},
                        {'headers': {'status': 'failed', 'code': 1, 'error_message': self.client_id}, 'results': self.tracks},
                        {'headers': {'status': 'success', 'code': 0}, 'results': None}):
            with self.subTest(payload=payload):
                cache.clear()
                self.response.json.return_value = payload
                response = customer_music_track(self.request())
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(self.client_id, response.content.decode())

    def test_empty_catalog_is_cached_briefly(self):
        self.payload['results'] = []
        for _ in range(2):
            self.assertEqual(customer_music_track(self.request()).status_code, 503)
        self.http_get.assert_called_once()

    def test_unsafe_or_credential_bearing_audio_urls_are_never_returned(self):
        urls = (
            '', None, 'javascript:alert(1)', 'http://prod-1.storage.jamendo.com/track',
            'https://example.com/audio.mp3', 'https://prod-1.storage.jamendo.com.evil.example/audio',
            'https://evil@prod-1.storage.jamendo.com/audio', 'https://prod-1.storage.jamendo.com:9000/audio',
            'https://api.jamendo.com/v3.0/tracks/file/?client_id=' + self.client_id,
            'https://prod-1.storage.jamendo.com/?client_id=anything',
            'https://prod-1.storage.jamendo.com/?from=' + quote(self.client_id, safe=''),
            'https://prod-1.storage.jamendo.com/?from=' + quote(quote(self.client_id, safe=''), safe=''),
        )
        for url in urls:
            with self.subTest(url=url):
                cache.clear()
                self.payload['results'] = [dict(self.tracks[0], audio=url)]
                self.assertEqual(customer_music_track(self.request()).status_code, 503)

    def test_missing_or_malformed_tracks_are_skipped_and_ids_are_unique(self):
        invalid = [None, {}, dict(self.tracks[0], id=1), dict(self.tracks[0], id='abc'),
                   dict(self.tracks[0], name=None), dict(self.tracks[0], artist_name=None)]
        self.payload['results'] = invalid + [self.tracks[0], self.tracks[0], self.tracks[1]]
        with patch.object(music, 'choice', side_effect=lambda tracks: tracks[-1]) as choose:
            response = customer_music_track(self.request())
        self.assertEqual(len(choose.call_args.args[0]), 2)
        self.assertEqual(self.body(response)['track']['id'], '2')

    def test_echoed_credentials_in_any_public_field_are_removed_from_pool(self):
        for field in ('id', 'name', 'artist_name'):
            with self.subTest(field=field):
                cache.clear()
                self.payload['results'] = [dict(self.tracks[0], **{field: self.client_id})]
                self.assertIsNone(music.get_random_music_track())

    def test_pool_is_limited_to_one_hundred_tracks(self):
        self.payload['results'] = [dict(self.tracks[0], id=str(i)) for i in range(150)]
        with patch.object(music, 'choice', side_effect=lambda tracks: tracks[0]) as choose:
            music.get_random_music_track()
        self.assertEqual(len(choose.call_args.args[0]), 100)

    def test_catalog_cache_expires_after_thirty_minutes(self):
        clock = time.time()
        with patch('time.time', return_value=clock):
            music.get_random_music_track()
        with patch('time.time', return_value=clock + 1799):
            music.get_random_music_track()
        self.http_get.assert_called_once()
        with patch('time.time', return_value=clock + 1801):
            music.get_random_music_track()
        self.assertEqual(self.http_get.call_count, 2)

    def test_temporary_failure_cache_expires_after_one_minute(self):
        self.response.status_code = 503
        clock = time.time()
        with patch('time.time', return_value=clock):
            self.assertIsNone(music.get_random_music_track())
        self.response.status_code = 200
        with patch('time.time', return_value=clock + 59):
            self.assertIsNone(music.get_random_music_track())
        self.http_get.assert_called_once()
        with patch('time.time', return_value=clock + 61):
            self.assertIsNotNone(music.get_random_music_track())
        self.assertEqual(self.http_get.call_count, 2)

    def test_changed_client_id_uses_separate_cache_without_disclosing_it(self):
        music.get_random_music_track()
        self.config.return_value = 'changed-private-client-id'
        response = customer_music_track(self.request())
        self.assertEqual(self.http_get.call_count, 2)
        self.assertNotIn(self.config.return_value, response.content.decode())

    def test_concurrent_discovery_requests_share_one_catalog_request(self):
        with ThreadPoolExecutor(max_workers=4) as workers:
            tracks = list(workers.map(lambda _: music.get_random_music_track(), range(4)))
        self.assertTrue(all(tracks))
        self.http_get.assert_called_once()

    def test_random_json_response_is_not_browser_cached(self):
        response = customer_music_track(self.request())
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('private', response['Cache-Control'])

    def test_shared_player_renders_without_requesting_music_or_exposing_credentials(self):
        for page in ('dashboard', 'how_points_work', 'discounts', 'offers', 'news', 'weather'):
            with self.subTest(page=page):
                html = render_to_string(f'customer_{page}.html', {'points_state': None}, request=self.request())
                self.assertEqual(html.count('id="customer-music-control"'), 1)
                self.assertIn(f'data-track-url="{self.url}"', html)
                self.assertIn('<audio class="customer-music-audio" preload="none" hidden></audio>', html)
                self.assertNotIn(self.client_id, html)
                self.assertNotIn('api.jamendo.com', html)
                self.assertNotIn('Music playback is coming soon', html)
        self.http_get.assert_not_called()
        self.config.assert_not_called()
