from .section_test_support import stub_unlocked_navigation
import re
import time
from copy import deepcopy
from datetime import date, timedelta
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import requests
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import resolve, reverse

from . import weather
from .forms import CitySearchForm
from .views import customer_weather


@override_settings(CACHES={
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'customer-weather-tests',
    },
})
class CustomerWeatherTests(SimpleTestCase):
    def setUp(self):
        stub_unlocked_navigation(self)
        cache.clear()
        self.addCleanup(cache.clear)
        self.url = reverse('customer_weather')
        self.user = get_user_model()(username='customer', role='customer', is_active=True)
        self.geo_payload = {'results': [{
            'name': 'Bucharest', 'admin1': 'București', 'country': 'Romania',
            'latitude': 44.43225, 'longitude': 26.10626,
        }]}
        self.forecast_payload = {
            'timezone': 'Europe/Bucharest',
            'current': {
                'time': '2026-09-12T15:15', 'interval': 900,
                'temperature_2m': 24.2, 'apparent_temperature': 25.1,
                'weather_code': 2, 'is_day': 1, 'wind_speed_10m': 8.4,
                'precipitation': 0.2, 'rain': 0.1,
            },
            'daily': {
                'time': [(date(2026, 9, 12) + timedelta(days=i)).isoformat() for i in range(7)],
                'weather_code': [2, 0, 3, 61, 95, 45, 71],
                'temperature_2m_max': [26, 27, 24, 23, 22, 21, 20],
                'temperature_2m_min': [16, 17, 14, 13, 12, 11, 10],
                'precipitation_probability_max': [0, 10, 20, 70, 90, 20, 10],
            },
        }
        self.geo_response = self.api_response(self.geo_payload)
        self.forecast_response = self.api_response(self.forecast_payload)
        self.http_get = self.enterContext(patch.object(weather.requests, 'get', side_effect=self.api_get))

    @staticmethod
    def api_response(payload):
        response = MagicMock(status_code=200)
        response.json.return_value = payload
        response.__enter__.return_value = response
        return response

    def api_get(self, url, **kwargs):
        if url == weather.GEOCODING_URL:
            return self.geo_response
        self.assertEqual(url, weather.FORECAST_URL)
        return self.forecast_response

    def request(self, city='Bucharest', **extra):
        params = {} if city is None else {'city': city}
        request = RequestFactory().get(self.url, {**params, **extra})
        request.user = self.user
        return request

    def test_weather_route_resolves(self):
        self.assertEqual(self.url, '/customer/weather/')
        self.assertIs(resolve(self.url).func, customer_weather)

    def test_initial_page_renders_search_without_automatic_location_or_api_call(self):
        with self.assertTemplateUsed('customer_weather.html'), self.assertTemplateUsed('customer_base.html'):
            response = customer_weather(self.request(None))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Search for a city')
        self.assertContains(response, 'Enter a city above')
        self.assertNotContains(response, 'navigator.geolocation')
        self.assertNotContains(response, 'class="weather-current"')
        self.http_get.assert_not_called()

    def test_anonymous_user_redirects_even_when_weather_is_cached(self):
        customer_weather(self.request())
        self.http_get.reset_mock()
        request = self.request()
        request.user = AnonymousUser()
        response = customer_weather(request)
        self.assertEqual(response.status_code, 302)
        destination = urlsplit(response.url)
        self.assertEqual(destination.path, reverse('login_account_customer'))
        self.assertEqual(parse_qs(destination.query)['next'], [f'{self.url}?city=Bucharest'])
        self.http_get.assert_not_called()

    def test_inactive_and_non_customers_cannot_fetch_weather(self):
        for role, active, staff in (
            ('customer', False, False), ('user', True, False), ('user', True, True),
        ):
            with self.subTest(role=role, active=active, staff=staff):
                self.user.role = role
                self.user.is_active = active
                self.user.is_staff = staff
                self.user.is_superuser = staff
                with self.assertRaises(PermissionDenied):
                    customer_weather(self.request())
        self.http_get.assert_not_called()

    def test_city_search_geocodes_then_fetches_current_and_seven_days_together(self):
        response = customer_weather(self.request('  Bucharest  '))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.http_get.call_count, 2)
        geo, forecast = self.http_get.call_args_list
        self.assertEqual(geo.args, (weather.GEOCODING_URL,))
        self.assertEqual(geo.kwargs['params'], {
            'name': 'Bucharest', 'count': 1, 'language': 'en', 'format': 'json',
        })
        self.assertEqual(forecast.args, (weather.FORECAST_URL,))
        params = forecast.kwargs['params']
        self.assertEqual((params['latitude'], params['longitude']), (44.43225, 26.10626))
        self.assertEqual(params['forecast_days'], 7)
        self.assertEqual(params['timezone'], 'auto')
        self.assertEqual(params['temperature_unit'], 'celsius')
        self.assertEqual(params['wind_speed_unit'], 'kmh')
        self.assertEqual(params['precipitation_unit'], 'mm')
        self.assertIn('apparent_temperature', params['current'])
        self.assertIn('precipitation_probability_max', params['daily'])
        for call in (geo, forecast):
            self.assertEqual(call.kwargs['timeout'], (3, 8))
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertNotIn('apikey', call.kwargs['params'])
            self.assertEqual(call.kwargs['headers'], {'Accept': 'application/json'})

    def test_browser_cannot_override_coordinates_provider_or_forecast_options(self):
        customer_weather(self.request(latitude='0', longitude='0', url='https://example.com', forecast_days=100))
        params = self.http_get.call_args.kwargs['params']
        self.assertEqual(params['latitude'], 44.43225)
        self.assertEqual(params['longitude'], 26.10626)
        self.assertEqual(params['forecast_days'], 7)
        self.assertNotIn('url', params)

    def test_international_and_qualified_city_names_are_valid(self):
        for city in ('São Paulo', 'București', '東京', 'St. John’s', "L'Haÿ-les-Roses", 'Frankfurt (Oder)', 'Paris, France'):
            with self.subTest(city=city):
                form = CitySearchForm({'city': city})
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.cleaned_data['city'], city)
        form = CitySearchForm({'city': '  New   York  '})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['city'], 'New York')

    def test_invalid_inputs_show_validation_without_network_requests(self):
        for city in ('', ' ', 'A', '12345', 'a' * 81, '<script>alert(1)</script>', 'https://localhost', 'Paris\x00', 'New\nYork'):
            with self.subTest(city=city):
                response = customer_weather(self.request(city))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'role="alert"')
                self.assertNotContains(response, 'class="weather-current"')
                self.assertNotContains(response, '<script>alert(1)</script>')
        self.http_get.assert_not_called()

    def test_unknown_city_has_clean_message_and_does_not_fetch_forecast(self):
        for payload in ({}, {'results': []}):
            with self.subTest(payload=payload):
                cache.clear()
                self.http_get.reset_mock()
                self.geo_response.json.return_value = payload
                response = customer_weather(self.request('Unknownville'))
                self.assertContains(response, 'We couldn’t find that city.')
                self.assertNotContains(response, 'Weather is temporarily unavailable.')
                self.http_get.assert_called_once()

    def test_malformed_geocoding_data_shows_fallback(self):
        for payload in (None, [], {'results': None}, {'results': [None]}, {'results': [{}]},
                        {'results': [{'name': 'City', 'latitude': 100, 'longitude': 10}]}):
            with self.subTest(payload=payload):
                cache.clear()
                self.geo_response.json.return_value = payload
                response = customer_weather(self.request())
                self.assertContains(response, 'Weather is temporarily unavailable. Please try again later.')
        self.forecast_response.json.assert_not_called()

    def test_current_weather_and_seven_forecast_cards_render_without_database_access(self):
        response = customer_weather(self.request())
        for text in ('Bucharest', 'București, Romania', '24°C', 'Partly cloudy', 'Feels like', '25°C',
                     '8.4', 'km/h', '0.2', '0.1', 'mm', 'Last 15 min', '7 Day Forecast',
                     'High', 'Low', 'Precip. 0%', 'Precip. 90%', 'Light snow'):
            self.assertContains(response, text)
        self.assertContains(response, '<li class="weather-day">', count=7)
        self.assertContains(response, 'datetime="2026-09-18"')
        self.assertNotContains(response, weather.GEOCODING_URL)
        self.assertNotContains(response, weather.FORECAST_URL)
        self.assertNotContains(response, 'id="points-controls"')

    def test_current_time_and_forecast_dates_keep_location_local_time(self):
        with override_settings(TIME_ZONE='America/Los_Angeles', USE_TZ=True):
            response = customer_weather(self.request())
        self.assertContains(response, '12 Sep, 15:15')
        self.assertContains(response, 'local time')
        self.assertContains(response, 'datetime="2026-09-12"')

    def test_missing_optional_values_do_not_break_the_page(self):
        self.forecast_payload['current'].update(apparent_temperature=None, rain=None, time=None, interval=None)
        self.forecast_payload['daily'].pop('precipitation_probability_max')
        response = customer_weather(self.request())
        self.assertContains(response, '<li class="weather-day">', count=7)
        self.assertNotContains(response, 'Feels like')
        self.assertNotContains(response, 'weather-updated">')
        self.assertNotContains(response, '>None<')
        self.assertContains(response, 'Precip. unavailable', count=7)

    def test_zero_values_are_rendered_and_unknown_condition_is_safe(self):
        self.forecast_payload['current'].update(
            temperature_2m=0, apparent_temperature=0, wind_speed_10m=0, precipitation=0, rain=0,
            weather_code=999,
        )
        response = customer_weather(self.request())
        self.assertContains(response, '<span class="weather-temperature">0°C</span>', html=True)
        self.assertContains(response, '<dd>0°C</dd>', count=1, html=True)
        self.assertContains(response, '0.0 <small>', count=3)
        self.assertContains(response, 'Condition unavailable')

    def test_condition_mapping_handles_day_night_and_all_documented_codes(self):
        self.forecast_payload['current'].update(weather_code=0, is_day=0)
        result = weather.get_customer_weather('Bucharest')
        self.assertEqual(result['current']['condition'], {'label': 'Clear sky', 'icon': '☾'})
        for code in (0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67,
                     71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99):
            with self.subTest(code=code):
                self.assertNotEqual(weather._condition(code)['label'], 'Condition unavailable')

    def test_malformed_forecasts_show_safe_fallback(self):
        incomplete = deepcopy(self.forecast_payload)
        incomplete['daily']['time'] = ['2026-09-12']
        invalid_date = deepcopy(self.forecast_payload)
        invalid_date['daily']['time'][0] = '2026-99-99'
        invalid_temperature = deepcopy(self.forecast_payload)
        invalid_temperature['current']['temperature_2m'] = float('nan')
        for payload in (None, [], {}, {'current': [], 'daily': {}}, incomplete, invalid_date, invalid_temperature):
            with self.subTest(payload=payload):
                cache.clear()
                self.forecast_response.json.return_value = payload
                response = customer_weather(self.request())
                self.assertContains(response, 'Weather is temporarily unavailable. Please try again later.')

    def test_http_errors_and_redirects_from_either_api_show_fallback(self):
        for upstream in (self.geo_response, self.forecast_response):
            for status in (301, 400, 429, 500, 503):
                with self.subTest(upstream='geocoding' if upstream is self.geo_response else 'forecast', status=status):
                    cache.clear()
                    upstream.status_code = status
                    response = customer_weather(self.request())
                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, 'Weather is temporarily unavailable. Please try again later.')
            upstream.status_code = 200

    def test_timeouts_and_connection_failures_from_either_api_are_safe(self):
        for endpoint in (weather.GEOCODING_URL, weather.FORECAST_URL):
            for exception in (requests.Timeout, requests.ConnectionError):
                with self.subTest(endpoint=endpoint, exception=exception):
                    cache.clear()
                    def fail(url, **kwargs):
                        if url == endpoint:
                            raise exception('private provider error detail')
                        return self.api_get(url, **kwargs)
                    self.http_get.side_effect = fail
                    response = customer_weather(self.request())
                    self.assertContains(response, 'Weather is temporarily unavailable.')
                    self.assertNotContains(response, 'private provider error detail')

    def test_invalid_json_from_either_api_is_safe(self):
        for upstream in (self.geo_response, self.forecast_response):
            cache.clear()
            upstream.json.side_effect = ValueError('private provider error detail')
            response = customer_weather(self.request())
            self.assertContains(response, 'Weather is temporarily unavailable.')
            self.assertNotContains(response, 'private provider error detail')
            upstream.json.side_effect = None

    def test_provider_location_text_is_escaped(self):
        self.geo_payload['results'][0]['name'] = '<script>alert("city")</script>'
        response = customer_weather(self.request())
        self.assertNotContains(response, '<script>alert("city")</script>')
        self.assertContains(response, '&lt;script&gt;')

    def test_refresh_case_changes_and_other_customers_share_data_not_page_html(self):
        customer_weather(self.request())
        customer_weather(self.request('  BUCHAREST  '))
        self.user.username = 'another-customer'
        response = customer_weather(self.request())
        self.assertEqual(self.http_get.call_count, 2)
        self.assertContains(response, 'Welcome, another-customer')
        self.assertNotContains(response, 'Welcome, customer')

    def test_different_city_names_for_same_location_reuse_forecast_cache(self):
        weather.get_customer_weather('Bucharest')
        weather.get_customer_weather('București')
        self.assertEqual(self.http_get.call_count, 3)
        self.assertEqual(sum(call.args[0] == weather.FORECAST_URL for call in self.http_get.call_args_list), 1)

    def test_different_locations_have_separate_weather_caches(self):
        bucharest = weather.get_customer_weather('Bucharest')
        self.geo_payload['results'][0].update(name='London', latitude=51.50853, longitude=-0.12574)
        self.forecast_payload['current']['temperature_2m'] = 12
        london = weather.get_customer_weather('London')
        self.assertEqual(weather.get_customer_weather('Bucharest'), bucharest)
        self.assertEqual(weather.get_customer_weather('London'), london)
        self.assertEqual(bucharest['current']['temperature'], 24.2)
        self.assertEqual(london['current']['temperature'], 12)
        self.assertEqual(self.http_get.call_count, 4)

    def test_geocoding_and_forecast_caches_expire_after_thirty_minutes(self):
        clock = time.time()
        with patch('time.time', return_value=clock):
            weather.get_customer_weather('Bucharest')
        with patch('time.time', return_value=clock + 1799):
            weather.get_customer_weather('Bucharest')
        self.assertEqual(self.http_get.call_count, 2)
        with patch('time.time', return_value=clock + 1801):
            weather.get_customer_weather('Bucharest')
        self.assertEqual(self.http_get.call_count, 4)

    def test_not_found_result_is_cached_for_thirty_minutes(self):
        self.geo_response.json.return_value = {}
        clock = time.time()
        with patch('time.time', return_value=clock):
            weather.get_customer_weather('Unknownville')
        with patch('time.time', return_value=clock + 1799):
            weather.get_customer_weather('Unknownville')
        self.http_get.assert_called_once()
        with patch('time.time', return_value=clock + 1801):
            weather.get_customer_weather('Unknownville')
        self.assertEqual(self.http_get.call_count, 2)

    def test_temporary_failure_retries_after_one_minute(self):
        self.forecast_response.status_code = 503
        clock = time.time()
        with patch('time.time', return_value=clock):
            self.assertEqual(weather.get_customer_weather('Bucharest')['status'], 'unavailable')
        self.forecast_response.status_code = 200
        with patch('time.time', return_value=clock + 59):
            self.assertEqual(weather.get_customer_weather('Bucharest')['status'], 'unavailable')
        self.assertEqual(self.http_get.call_count, 2)
        with patch('time.time', return_value=clock + 61):
            self.assertEqual(weather.get_customer_weather('Bucharest')['status'], 'ok')
        self.assertEqual(self.http_get.call_count, 3)

    def test_shared_navigation_places_weather_directly_after_news(self):
        response = customer_weather(self.request(None))
        self.assertContains(response, f'<a href="{self.url}" aria-current="page">Weather</a>', html=True)
        for template in ('customer_weather.html', 'customer_dashboard.html'):
            html = render_to_string(template, {'city_form': CitySearchForm(), 'points_state': None}, request=self.request(None))
            navigation = html.split('<nav class="customer-secondary-nav"', 1)[1].split('</nav>', 1)[0]
            links = re.findall(r'href="([^"]+)"', navigation)
            self.assertEqual(links[links.index(reverse('customer_news')) + 1], self.url)
