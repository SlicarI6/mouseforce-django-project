from .section_test_support import stub_unlocked_navigation
import re
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event, get_ident
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.cache.backends.locmem import LocMemCache
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import resolve, reverse

from . import news
from .views import customer_news


@override_settings(CACHES={
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'customer-news-tests',
    },
})
class CustomerNewsTests(SimpleTestCase):
    # A test sentinel only; these tests never load a real key or contact GNews.
    test_key = 'test-gnews-secret-do-not-display'

    def setUp(self):
        stub_unlocked_navigation(self)
        for category in news.NEWS_CATEGORIES:
            cache.delete(news.news_cache_key(category))
            self.addCleanup(cache.delete, news.news_cache_key(category))
        self.config = self.enterContext(patch.object(news, 'config', return_value=self.test_key))
        self.http_get = self.enterContext(patch.object(news.requests, 'get'))
        self.api_response = self.http_get.return_value.__enter__.return_value
        self.api_response.status_code = 200
        self.article = {
            'title': 'A new discovery',
            'description': 'Researchers share a useful discovery with the world.',
            'url': 'https://example.com/discovery',
            'image': 'https://example.com/discovery.jpg',
            'source': {'name': 'Example News'},
            'publishedAt': '2026-09-12T10:30:00Z',
        }
        self.api_response.json.return_value = {'articles': [self.article]}
        self.url = reverse('customer_news')
        self.request = RequestFactory().get(self.url)
        self.request.user = get_user_model()(
            username='customer', role='customer', is_active=True,
        )

    def test_route_resolves_to_customer_news(self):
        self.assertEqual(self.url, '/customer/news/')
        self.assertIs(resolve(self.url).func, customer_news)

    def test_customer_sees_articles_in_shared_layout_without_database_access(self):
        with self.assertTemplateUsed('customer_news.html'), self.assertTemplateUsed('customer_base.html'):
            response = customer_news(self.request)
        self.assertEqual(response.status_code, 200)
        for text in (
            'Stay informed. Discover something new every day.',
            self.article['title'], self.article['description'], self.article['source']['name'],
            'src="https://example.com/discovery.jpg"',
            'href="https://example.com/discovery"', '12 Sep 2026, 10:30 UTC',
            'datetime="2026-09-12T10:30:00+00:00"',
            'rel="noopener noreferrer"', 'Read more',
        ):
            self.assertContains(response, text)
        self.assertContains(response, f'<a href="{self.url}" aria-current="page">News</a>', html=True)
        self.assertNotContains(response, self.test_key)
        self.assertNotContains(response, 'gnews.io/api/')
        self.assertNotContains(response, 'id="points-controls"')

    def test_anonymous_customer_is_redirected_even_when_articles_are_cached(self):
        customer_news(self.request)
        self.http_get.reset_mock()
        self.request.user = AnonymousUser()
        response = customer_news(self.request)
        self.assertEqual(response.status_code, 302)
        destination = urlsplit(response.url)
        self.assertEqual(destination.path, reverse('login_account_customer'))
        self.assertEqual(parse_qs(destination.query)['next'], [self.url])
        self.http_get.assert_not_called()

    def test_inactive_customers_and_non_customers_are_forbidden(self):
        for role, active, staff in (
            ('customer', False, False), ('user', True, False), ('user', True, True),
        ):
            with self.subTest(role=role, active=active, staff=staff):
                self.request.user.role = role
                self.request.user.is_active = active
                self.request.user.is_staff = staff
                self.request.user.is_superuser = staff
                with self.assertRaises(PermissionDenied):
                    customer_news(self.request)
        self.http_get.assert_not_called()

    def test_refreshes_and_other_customers_reuse_article_cache_not_page_html(self):
        customer_news(self.request)
        customer_news(self.request)
        self.request.user.username = 'second-customer'
        response = customer_news(self.request)
        self.assertContains(response, 'Welcome, second-customer')
        self.assertNotContains(response, 'Welcome, customer')
        self.http_get.assert_called_once()
        self.assertNotIn(self.test_key, repr(cache.get(news.news_cache_key())))

    def test_key_is_only_sent_in_header_and_redirects_are_disabled(self):
        news.get_customer_news()
        args, kwargs = self.http_get.call_args
        self.assertEqual(args, ('https://gnews.io/api/v4/top-headlines',))
        self.assertNotIn(self.test_key, repr(kwargs['params']))
        self.assertEqual(kwargs['headers']['X-Api-Key'], self.test_key)
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'], (3, 8))

    def test_http_errors_and_redirects_show_generic_message(self):
        for status in (301, 401, 403, 429, 500, 503):
            with self.subTest(status=status):
                cache.delete(news.news_cache_key())
                self.api_response.status_code = status
                response = customer_news(self.request)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'News is temporarily unavailable. Please try again later.')
                self.assertNotContains(response, self.test_key)
        self.api_response.json.assert_not_called()

    def test_network_errors_do_not_escape_to_page_or_logs(self):
        for error in (requests.Timeout(self.test_key), requests.ConnectionError(self.test_key)):
            with self.subTest(error=type(error).__name__):
                cache.delete(news.news_cache_key())
                self.http_get.side_effect = error
                with patch('sys.stdout') as stdout, patch('sys.stderr') as stderr:
                    response = customer_news(self.request)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'News is temporarily unavailable.')
                self.assertNotContains(response, self.test_key)
                stdout.write.assert_not_called()
                stderr.write.assert_not_called()

    def test_missing_key_shows_fallback_without_request(self):
        self.config.return_value = ''
        response = customer_news(self.request)
        self.assertContains(response, 'News is temporarily unavailable.')
        self.http_get.assert_not_called()

    def test_invalid_json_shows_fallback(self):
        self.api_response.json.side_effect = ValueError(self.test_key)
        response = customer_news(self.request)
        self.assertContains(response, 'News is temporarily unavailable.')
        self.assertNotContains(response, self.test_key)

    def test_invalid_payload_shapes_show_fallback(self):
        for payload in (None, [], {}, {'articles': None}, {'articles': 'invalid'}):
            with self.subTest(payload=payload):
                cache.delete(news.news_cache_key())
                self.api_response.json.return_value = payload
                self.assertTrue(news.get_customer_news()['unavailable'])

    def test_empty_articles_have_clean_cached_empty_state(self):
        self.api_response.json.return_value = {'articles': []}
        for _ in range(2):
            response = customer_news(self.request)
            self.assertContains(response, 'No news articles are available right now.')
            self.assertNotContains(response, 'News is temporarily unavailable.')
        self.http_get.assert_called_once()

    def test_optional_missing_fields_and_invalid_dates_do_not_break_cards(self):
        for published in (None, 'not a date', '2026-99-99T12:00:00Z'):
            with self.subTest(published=published):
                cache.delete(news.news_cache_key())
                self.article.update(image=None, description=None, source=None, publishedAt=published)
                response = customer_news(self.request)
                self.assertContains(response, self.article['title'])
                self.assertNotContains(response, '<img class="customer-news-image"')
                self.assertNotContains(response, '<time ')

    def test_malformed_articles_and_unsafe_links_are_not_rendered(self):
        invalid = [None, {}, {'title': 'Unsafe', 'url': 'javascript:alert(1)'}]
        self.article['image'] = 'data:text/html,unsafe'
        self.api_response.json.return_value = {'articles': invalid + [self.article]}
        response = customer_news(self.request)
        self.assertContains(
            response, '<article class="customer-news-card customer-news-featured customer-news-featured--text">', count=1,
        )
        self.assertNotContains(response, 'javascript:alert(1)')
        self.assertNotContains(response, 'data:text/html,unsafe')

    def test_article_text_is_escaped_and_descriptions_are_short(self):
        self.article['title'] = '<script>alert("news")</script>'
        self.article['description'] = 'Long description. ' * 40
        response = customer_news(self.request)
        self.assertNotContains(response, '<script>alert("news")</script>')
        self.assertContains(response, '&lt;script&gt;')
        self.assertLessEqual(len(news.get_customer_news()['articles'][0]['description']), 220)

    def test_upstream_echoed_credentials_are_not_cached_or_rendered(self):
        self.article.update(
            title=f'News {self.test_key}', description=self.test_key,
            image=f'https://example.com/image?key={self.test_key}',
            source={'name': self.test_key},
        )
        unsafe_article = deepcopy(self.article)
        unsafe_article['url'] = f'https://example.com/article?key={self.test_key}'
        self.api_response.json.return_value = {'articles': [self.article, unsafe_article]}
        response = customer_news(self.request)
        self.assertNotContains(response, self.test_key)
        self.assertNotIn(self.test_key, repr(cache.get(news.news_cache_key())))
        self.assertEqual(len(news.get_customer_news()['articles']), 1)

    def test_success_cache_expires_after_three_hours(self):
        clock = time.time()
        with patch('time.time', return_value=clock):
            news.get_customer_news()
        with patch('time.time', return_value=clock + 10799):
            news.get_customer_news()
        self.http_get.assert_called_once()
        with patch('time.time', return_value=clock + 10801):
            news.get_customer_news()
        self.assertEqual(self.http_get.call_count, 2)

    def test_failure_cache_expires_after_one_minute(self):
        self.api_response.status_code = 503
        clock = time.time()
        with patch('time.time', return_value=clock):
            self.assertTrue(news.get_customer_news()['unavailable'])
        with patch('time.time', return_value=clock + 59):
            news.get_customer_news()
        self.http_get.assert_called_once()
        self.api_response.status_code = 200
        with patch('time.time', return_value=clock + 61):
            self.assertFalse(news.get_customer_news()['unavailable'])
        self.assertEqual(self.http_get.call_count, 2)

    def test_only_nine_articles_are_displayed(self):
        self.api_response.json.return_value = {'articles': [self.article] * 12}
        self.assertEqual(len(news.get_customer_news()['articles']), 9)

    def category_request(self, category, **params):
        request = RequestFactory().get(self.url, {'category': category, **params})
        request.user = self.request.user
        return request

    def test_default_category_is_all_and_only_fetches_general_headlines(self):
        response = customer_news(self.request)
        self.http_get.assert_called_once()
        args, kwargs = self.http_get.call_args
        self.assertEqual(args[0], news.GNEWS_URL)
        self.assertNotIn('q', kwargs['params'])
        self.assertContains(
            response, '<a href="/customer/news/?category=all" aria-current="page">All</a>', html=True,
        )
        for slug, label in (
            ('ai-tech', 'AI & Tech'), ('gaming', 'Gaming'), ('money-deals', 'Money & Deals'),
            ('travel', 'Travel'), ('entertainment', 'Entertainment'),
            ('sports', 'Sports'), ('world', 'World'),
        ):
            self.assertContains(response, f'href="/customer/news/?category={slug}"')
            self.assertIsNone(cache.get(news.news_cache_key(slug)))

    def test_each_category_uses_its_server_defined_filter_on_demand(self):
        expected = {
            'ai-tech': ('AI & Tech', news.GNEWS_SEARCH_URL, 'q', '"artificial intelligence"'),
            'gaming': ('Gaming', news.GNEWS_SEARCH_URL, 'q', '"video games"'),
            'money-deals': ('Money & Deals', news.GNEWS_SEARCH_URL, 'q', '"personal finance"'),
            'travel': ('Travel', news.GNEWS_SEARCH_URL, 'q', 'travel'),
            'entertainment': ('Entertainment', news.GNEWS_URL, 'category', 'entertainment'),
            'sports': ('Sports', news.GNEWS_URL, 'category', 'sports'),
            'world': ('World', news.GNEWS_URL, 'category', 'world'),
        }
        for category, (label, endpoint, parameter, keyword) in expected.items():
            with self.subTest(category=category):
                self.http_get.reset_mock()
                response = customer_news(self.category_request(category))
                self.http_get.assert_called_once()
                args, kwargs = self.http_get.call_args
                self.assertEqual(args[0], endpoint)
                self.assertIn(keyword, kwargs['params'][parameter])
                if endpoint == news.GNEWS_SEARCH_URL:
                    self.assertEqual(kwargs['params']['sortby'], 'publishedAt')
                    self.assertLessEqual(len(kwargs['params']['q']), 200)
                else:
                    self.assertEqual(kwargs['params'][parameter], keyword)
                    self.assertNotIn('q', kwargs['params'])
                self.assertEqual(kwargs['headers']['X-Api-Key'], self.test_key)
                self.assertNotContains(response, self.test_key)
                self.assertContains(
                    response,
                    f'<a href="/customer/news/?category={category}" aria-current="page">{label}</a>',
                    html=True,
                )

    def test_category_caches_do_not_mix_articles_or_fetch_unrequested_categories(self):
        self.article['title'] = 'Gaming article'
        gaming = news.get_customer_news('gaming')
        self.article['title'] = 'Travel article'
        travel = news.get_customer_news('travel')
        self.assertEqual(news.get_customer_news('gaming'), gaming)
        self.assertEqual(news.get_customer_news('travel'), travel)
        self.assertEqual(gaming['articles'][0]['title'], 'Gaming article')
        self.assertEqual(travel['articles'][0]['title'], 'Travel article')
        self.assertEqual(self.http_get.call_count, 2)
        for category in ('all', 'ai-tech', 'money-deals', 'entertainment', 'sports', 'world'):
            self.assertIsNone(cache.get(news.news_cache_key(category)))

    def test_invalid_categories_and_custom_api_parameters_reuse_all_cache(self):
        customer_news(self.request)
        for invalid in ('unknown', '', '../../gaming', 'science', 'https://example.com/'):
            with self.subTest(category=invalid):
                response = customer_news(self.category_request(
                    invalid, q='browser-injected-query', max=100, apikey='browser-injected-key',
                ))
                self.assertContains(
                    response, '<a href="/customer/news/?category=all" aria-current="page">All</a>', html=True,
                )
                self.assertNotContains(response, 'browser-injected-query')
        self.http_get.assert_called_once()
        self.assertNotIn('apikey', self.http_get.call_args.kwargs['params'])
        self.assertEqual(news.news_cache_key('unknown'), news.news_cache_key('all'))

    def test_featured_uses_first_article_once_and_latest_uses_remainder(self):
        articles = [dict(self.article, title=f'Story {number}', url=f'https://example.com/{number}') for number in range(3)]
        self.api_response.json.return_value = {'articles': articles}
        for category, label in (('gaming', 'Gaming'), ('sports', 'Sports'), ('world', 'World')):
            with self.subTest(category=category):
                self.http_get.reset_mock()
                response = customer_news(self.category_request(category))
                self.http_get.assert_called_once()
                html = response.content.decode()
                latest_position = html.index('<h2 class="customer-news-section-heading">Latest News</h2>')
                self.assertLess(html.index('<h3>Story 0</h3>'), latest_position)
                for number in (1, 2):
                    self.assertGreater(html.index(f'<h3>Story {number}</h3>'), latest_position)
                for article in articles:
                    self.assertContains(response, f'<h3>{article["title"]}</h3>', count=1, html=True)
                    self.assertContains(response, f'href="{article["url"]}"', count=1)
                self.assertContains(response, '<article class="customer-news-card">', count=2)
                self.assertContains(response, f'<span class="customer-news-category-label">{label}</span>', html=True)

    def test_category_pills_place_sports_and_world_after_entertainment(self):
        response = customer_news(self.request)
        category_nav = response.content.decode().split('<nav class="customer-news-categories"', 1)[1].split('</nav>', 1)[0]
        self.assertEqual(
            re.findall(r'\?category=([a-z-]+)"', category_nav),
            ['all', 'ai-tech', 'gaming', 'money-deals', 'travel', 'entertainment', 'sports', 'world'],
        )

    def test_sports_and_world_keep_separate_three_hour_caches(self):
        clock = time.time()
        with patch('time.time', return_value=clock):
            self.article['title'] = 'Sports headline'
            sports = news.get_customer_news('sports')
            self.article['title'] = 'World headline'
            world = news.get_customer_news('world')
        with patch('time.time', return_value=clock + 10799):
            self.assertEqual(news.get_customer_news('sports'), sports)
            self.assertEqual(news.get_customer_news('world'), world)
            self.assertEqual(self.http_get.call_count, 2)
        self.assertEqual(sports['articles'][0]['title'], 'Sports headline')
        self.assertEqual(world['articles'][0]['title'], 'World headline')
        with patch('time.time', return_value=clock + 10801):
            news.get_customer_news('sports')
            self.assertEqual(self.http_get.call_count, 3)
            news.get_customer_news('world')
            self.assertEqual(self.http_get.call_count, 4)
        for category in ('all', 'ai-tech', 'gaming', 'money-deals', 'travel', 'entertainment'):
            self.assertIsNone(cache.get(news.news_cache_key(category)))

    def test_single_article_is_featured_without_an_extra_request(self):
        response = customer_news(self.request)
        self.assertContains(response, 'Featured Story')
        self.assertContains(response, 'Latest News')
        self.assertContains(response, '<h3>A new discovery</h3>', count=1, html=True)
        self.assertContains(response, 'No more articles are available in this category right now.')
        self.http_get.assert_called_once()

    def test_empty_and_unavailable_categories_keep_the_selected_filter(self):
        for status in (200, 503):
            with self.subTest(status=status):
                cache.delete(news.news_cache_key('gaming'))
                self.api_response.status_code = status
                self.api_response.json.return_value = {'articles': []}
                response = customer_news(self.category_request('gaming'))
                self.assertEqual(response.status_code, 200)
                self.assertContains(
                    response, '<a href="/customer/news/?category=gaming" aria-current="page">Gaming</a>', html=True,
                )
                self.assertNotContains(response, 'Featured Story')
                self.assertNotContains(response, '<article ')

    def test_category_expiry_is_independent(self):
        clock = time.time()
        with patch('time.time', return_value=clock):
            news.get_customer_news('gaming')
        with patch('time.time', return_value=clock + 3600):
            news.get_customer_news('travel')
        with patch('time.time', return_value=clock + 10801):
            news.get_customer_news('travel')
            self.assertEqual(self.http_get.call_count, 2)
            news.get_customer_news('gaming')
            self.assertEqual(self.http_get.call_count, 3)

    def test_category_failure_does_not_replace_other_category_cache(self):
        expected = news.get_customer_news('travel')
        self.api_response.status_code = 503
        self.assertTrue(news.get_customer_news('gaming')['unavailable'])
        self.assertEqual(news.get_customer_news('travel'), expected)
        news.get_customer_news('gaming')
        self.assertEqual(self.http_get.call_count, 2)

    def test_concurrent_category_requests_share_one_fetch(self):
        started, second_miss, release = Event(), Event(), Event()
        first_thread = []
        original_get = LocMemCache.get

        def observe_cache(backend, *args, **kwargs):
            result = original_get(backend, *args, **kwargs)
            if result is None and started.is_set() and get_ident() != first_thread[0]:
                second_miss.set()
            return result

        def fetch(category):
            first_thread.append(get_ident())
            started.set()
            if not release.wait(5):
                raise AssertionError('Timed out waiting for concurrent request')
            return [news._article(self.article, self.test_key)]

        with patch.object(news, '_fetch_articles', side_effect=fetch) as fetch_mock, \
                patch.object(LocMemCache, 'get', autospec=True, side_effect=observe_cache), \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(news.get_customer_news, 'gaming')
            try:
                self.assertTrue(started.wait(5))
                second = pool.submit(news.get_customer_news, 'gaming')
                self.assertTrue(second_miss.wait(5))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5), second.result(timeout=5))
            fetch_mock.assert_called_once_with('gaming')
