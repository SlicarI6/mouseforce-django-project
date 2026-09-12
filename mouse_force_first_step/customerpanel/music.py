"""Server-side Jamendo discovery. Only credential-free stream data is public."""

import logging
from hashlib import sha256
from secrets import choice
from threading import Lock
from urllib.parse import parse_qsl, unquote, urlsplit

import requests
from decouple import config
from django.core.cache import cache
from django.views.decorators.debug import sensitive_variables


JAMENDO_TRACKS_URL = 'https://api.jamendo.com/v3.0/tracks/'
MUSIC_CACHE_SECONDS = 30 * 60
MUSIC_ERROR_CACHE_SECONDS = 60
TRACK_POOL_LIMIT = 100
_pool_lock = Lock()


class _JamendoRequestLogFilter(logging.Filter):
    def filter(self, record):
        # This project enables DEBUG logging. Jamendo requires client_id in the
        # query string, so suppress only its credential-bearing request records.
        message = record.getMessage()
        return not ('client_id=' in message and (
            'api.jamendo.com' in message or '/v3.0/tracks/' in message
        ))


for _logger_name in ('urllib3.connectionpool', 'urllib3.util.retry'):
    logging.getLogger(_logger_name).addFilter(_JamendoRequestLogFilter())


def _contains_credential(value, client_id):
    # Reject literal and percent-encoded credentials, including double encoding.
    for _ in range(3):
        if client_id in value:
            return True
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return False


def _text(value, client_id):
    if not isinstance(value, str) or _contains_credential(value, client_id):
        return ''
    return ''.join(char for char in value if char.isprintable()).strip()[:200]


def _stream_url(value, client_id):
    if not isinstance(value, str) or len(value) > 2000 or _contains_credential(value, client_id):
        return ''
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ''
        if (
            parsed.scheme != 'https' or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment
            or not hostname.endswith('.storage.jamendo.com')
            or any(char.isspace() or ord(char) < 32 for char in value)
        ):
            return ''
        if any(key.lower() in ('client_id', 'client_secret', 'apikey', 'api_key', 'access_token')
               for key, _ in parse_qsl(parsed.query)):
            return ''
    except ValueError:
        return ''
    # Never pass Jamendo API URLs (which can contain client_id) to the browser.
    return value


@sensitive_variables()
def _fetch_pool(client_id):
    try:
        with requests.get(
            JAMENDO_TRACKS_URL,
            params={
                'client_id': client_id, 'format': 'json', 'limit': TRACK_POOL_LIMIT,
                'audioformat': 'mp31', 'order': 'popularity_month',
                'groupby': 'artist_id', 'type': 'single albumtrack',
            },
            headers={'Accept': 'application/json'}, timeout=(3, 10), allow_redirects=False,
        ) as response:
            if response.status_code != 200:
                return []
            payload = response.json()
    except (requests.RequestException, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    headers, results = payload.get('headers'), payload.get('results')
    if (not isinstance(headers, dict) or headers.get('status') != 'success'
            or headers.get('code') != 0 or not isinstance(results, list)):
        return []
    tracks, seen = [], set()
    for item in results[:TRACK_POOL_LIMIT]:
        if not isinstance(item, dict):
            continue
        track_id = item.get('id')
        if not isinstance(track_id, str) or not track_id.isascii() or not track_id.isdigit() or not 1 <= len(track_id) <= 12:
            continue
        name = _text(item.get('name'), client_id)
        artist = _text(item.get('artist_name'), client_id)
        audio = _stream_url(item.get('audio'), client_id)
        if track_id in seen or _contains_credential(track_id, client_id) or not name or not artist or not audio:
            continue
        seen.add(track_id)
        tracks.append({'id': track_id, 'name': name, 'artist_name': artist, 'audio_url': audio})
    return tracks


@sensitive_variables()
def get_random_music_track(excluded_ids=()):
    client_id = config('JAMENDO_CLIENT_ID', default='').strip()
    if not client_id:
        return None
    key = 'customerpanel:jamendo:v1:' + sha256(client_id.encode()).hexdigest()
    tracks = cache.get(key)
    if tracks is None:
        with _pool_lock:
            tracks = cache.get(key)
            if tracks is None:
                tracks = _fetch_pool(client_id)
                cache.set(key, tracks, MUSIC_CACHE_SECONDS if tracks else MUSIC_ERROR_CACHE_SECONDS)
    eligible = [track for track in tracks if track['id'] not in excluded_ids]
    return choice(eligible) if eligible else None
