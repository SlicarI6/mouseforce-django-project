"""Server-side GNews access and caching for the customer News page."""

from datetime import timezone
from threading import Lock

import requests
from decouple import config
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.utils.dateparse import parse_datetime
from django.utils.text import Truncator
from django.views.decorators.debug import sensitive_variables


NEWS_CACHE_KEY = 'customerpanel:gnews:categories:v2'
NEWS_CACHE_SECONDS = 3 * 60 * 60
NEWS_ERROR_CACHE_SECONDS = 60
GNEWS_URL = 'https://gnews.io/api/v4/top-headlines'
GNEWS_SEARCH_URL = 'https://gnews.io/api/v4/search'
ARTICLE_LIMIT = 9
NEWS_CATEGORIES = {
    'all': {'label': 'All', 'endpoint': GNEWS_URL, 'params': {}},
    'ai-tech': {
        'label': 'AI & Tech', 'endpoint': GNEWS_SEARCH_URL,
        'params': {'q': '"artificial intelligence" OR AI OR technology OR cybersecurity OR robotics'},
    },
    'gaming': {
        'label': 'Gaming', 'endpoint': GNEWS_SEARCH_URL,
        'params': {'q': '"video games" OR "video game" OR PlayStation OR Xbox OR Nintendo OR "PC gaming"'},
    },
    'money-deals': {
        'label': 'Money & Deals', 'endpoint': GNEWS_SEARCH_URL,
        'params': {'q': '"personal finance" OR savings OR discounts OR "shopping deals" OR "consumer prices"'},
    },
    'travel': {
        'label': 'Travel', 'endpoint': GNEWS_SEARCH_URL,
        'params': {'q': 'travel OR tourism OR flights OR hotels OR destinations'},
    },
    'entertainment': {
        'label': 'Entertainment', 'endpoint': GNEWS_URL,
        'params': {'category': 'entertainment'},
    },
    'sports': {
        'label': 'Sports', 'endpoint': GNEWS_URL,
        'params': {'category': 'sports'},
    },
    'world': {
        'label': 'World', 'endpoint': GNEWS_URL,
        'params': {'category': 'world'},
    },
}
_category_locks = {category: Lock() for category in NEWS_CATEGORIES}
_validate_url = URLValidator(schemes=['https', 'http'])


def normalise_news_category(category):
    return category if isinstance(category, str) and category in NEWS_CATEGORIES else 'all'


def news_cache_key(category='all'):
    return f'{NEWS_CACHE_KEY}:{normalise_news_category(category)}'


def _text(value, api_key):
    return value.strip().replace(api_key, '[redacted]') if isinstance(value, str) else ''


def _article_url(value, api_key):
    if not isinstance(value, str) or api_key in value:
        return ''
    try:
        _validate_url(value)
    except ValidationError:
        return ''
    return value


def _article(item, api_key):
    if not isinstance(item, dict):
        return None
    title = _text(item.get('title'), api_key)
    url = _article_url(item.get('url'), api_key)
    if not title or not url:
        return None
    try:
        published_at = parse_datetime(item.get('publishedAt', ''))
    except (TypeError, ValueError):
        published_at = None
    if published_at is not None and published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    source = item.get('source')
    return {
        'title': title,
        'url': url,
        'description': Truncator(_text(item.get('description'), api_key)).chars(220),
        'image': _article_url(item.get('image'), api_key),
        'source': _text(source.get('name'), api_key) if isinstance(source, dict) else '',
        'published_at': published_at,
    }


@sensitive_variables()
def _fetch_articles(category='all'):
    api_key = config('GNEWS_API_KEY', default='').strip()
    if not api_key:
        return None
    category_config = NEWS_CATEGORIES[normalise_news_category(category)]
    params = {'lang': 'en', 'max': ARTICLE_LIMIT, 'nullable': 'image'}
    params.update(category_config['params'])
    if category_config['endpoint'] == GNEWS_SEARCH_URL:
        params['sortby'] = 'publishedAt'
    try:
        # Keep the credential out of URLs, templates, caches and error messages.
        # Do not forward the authentication header to a redirect destination.
        with requests.get(
            category_config['endpoint'],
            params=params,
            headers={'X-Api-Key': api_key, 'Accept': 'application/json'},
            timeout=(3, 8),
            allow_redirects=False,
        ) as response:
            if response.status_code != 200:
                return None
            payload = response.json()
    except (requests.RequestException, ValueError):
        # Never log upstream responses or exceptions that may contain credentials.
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get('articles'), list):
        return None
    articles = []
    for item in payload['articles'][:ARTICLE_LIMIT]:
        article = _article(item, api_key)
        if article is not None:
            articles.append(article)
    return articles


def get_customer_news(category='all'):
    """Cache public article data, never authenticated page HTML or credentials."""
    category = normalise_news_category(category)
    key = news_cache_key(category)
    result = cache.get(key)
    if result is not None:
        return result
    # Coalesce concurrent requests in this worker; categories remain independent.
    with _category_locks[category]:
        result = cache.get(key)
        if result is not None:
            return result
        articles = _fetch_articles(category)
        result = {'articles': articles or [], 'unavailable': articles is None}
        cache.set(
            key, result,
            NEWS_ERROR_CACHE_SECONDS if result['unavailable'] else NEWS_CACHE_SECONDS,
        )
        return result
