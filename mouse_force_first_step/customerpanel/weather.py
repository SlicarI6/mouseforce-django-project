"""Keyless Open-Meteo lookups for the customer Weather page."""

from datetime import date, datetime
from hashlib import sha256
from math import isfinite

import requests
from django.core.cache import cache


GEOCODING_URL = 'https://geocoding-api.open-meteo.com/v1/search'
FORECAST_URL = 'https://api.open-meteo.com/v1/forecast'
WEATHER_CACHE_SECONDS = 30 * 60
WEATHER_ERROR_CACHE_SECONDS = 60
CURRENT_FIELDS = (
    'temperature_2m', 'apparent_temperature', 'weather_code', 'is_day',
    'wind_speed_10m', 'precipitation', 'rain',
)
DAILY_FIELDS = (
    'weather_code', 'temperature_2m_max', 'temperature_2m_min',
    'precipitation_probability_max',
)

# WMO weather interpretation codes, supplied by Open-Meteo.
CONDITIONS = {
    0: ('Clear sky', '☀'), 1: ('Mainly clear', '☀'),
    2: ('Partly cloudy', '⛅'), 3: ('Overcast', '☁'),
    45: ('Fog', '≋'), 48: ('Rime fog', '≋'),
    51: ('Light drizzle', '☂'), 53: ('Drizzle', '☂'), 55: ('Dense drizzle', '☂'),
    56: ('Light freezing drizzle', '☂'), 57: ('Freezing drizzle', '☂'),
    61: ('Light rain', '☂'), 63: ('Rain', '☂'), 65: ('Heavy rain', '☂'),
    66: ('Light freezing rain', '☂'), 67: ('Heavy freezing rain', '☂'),
    71: ('Light snow', '❄'), 73: ('Snow', '❄'), 75: ('Heavy snow', '❄'),
    77: ('Snow grains', '❄'), 80: ('Light showers', '☂'),
    81: ('Rain showers', '☂'), 82: ('Heavy showers', '☂'),
    85: ('Snow showers', '❄'), 86: ('Heavy snow showers', '❄'),
    95: ('Thunderstorm', 'ϟ'), 96: ('Thunderstorm with hail', 'ϟ'),
    99: ('Thunderstorm with heavy hail', 'ϟ'),
}


def _number(value, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not isfinite(value):
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _condition(code, is_day=True):
    label, icon = CONDITIONS.get(_number(code), ('Condition unavailable', '—'))
    if not is_day and code in (0, 1):
        icon = '☾'
    return {'label': label, 'icon': icon}


def _text(value):
    return value.strip()[:160] if isinstance(value, str) else ''


def _get_json(url, params):
    # URLs and forecast options are defined here, never supplied by the browser.
    with requests.get(
        url, params=params, headers={'Accept': 'application/json'},
        timeout=(3, 8), allow_redirects=False,
    ) as response:
        if response.status_code != 200:
            raise ValueError('Weather provider unavailable')
        payload = response.json()
    if not isinstance(payload, dict) or payload.get('error'):
        raise ValueError('Invalid weather response')
    return payload


def _cached(key, fetch):
    result = cache.get(key)
    if result is None:
        try:
            result = fetch()
        except (requests.RequestException, ValueError, TypeError):
            # Provider details never appear in the customer response.
            result = {'status': 'unavailable'}
        cache.set(
            key, result,
            WEATHER_ERROR_CACHE_SECONDS if result['status'] == 'unavailable'
            else WEATHER_CACHE_SECONDS,
        )
    return result


def _geocode(city):
    payload = _get_json(GEOCODING_URL, {
        'name': city, 'count': 1, 'language': 'en', 'format': 'json',
    })
    # Open-Meteo omits "results" when there are no matching locations.
    results = payload.get('results', [])
    if not isinstance(results, list):
        raise ValueError('Invalid locations')
    if not results:
        return {'status': 'not_found'}
    location = results[0]
    if not isinstance(location, dict):
        raise ValueError('Invalid location')
    latitude = _number(location.get('latitude'), -90, 90)
    longitude = _number(location.get('longitude'), -180, 180)
    name = _text(location.get('name'))
    if latitude is None or longitude is None or not name:
        raise ValueError('Invalid location')
    region = []
    for part in (location.get('admin1'), location.get('country')):
        part = _text(part)
        if part and part != name and part not in region:
            region.append(part)
    return {'status': 'ok', 'location': {
        'name': name, 'region': ', '.join(region),
        'latitude': latitude, 'longitude': longitude,
    }}


def _forecast(location):
    payload = _get_json(FORECAST_URL, {
        'latitude': location['latitude'], 'longitude': location['longitude'],
        'current': ','.join(CURRENT_FIELDS), 'daily': ','.join(DAILY_FIELDS),
        'forecast_days': 7, 'timezone': 'auto',
        'temperature_unit': 'celsius', 'wind_speed_unit': 'kmh',
        'precipitation_unit': 'mm',
    })
    current = payload.get('current')
    daily = payload.get('daily')
    if not isinstance(current, dict) or not isinstance(daily, dict):
        raise ValueError('Missing weather data')
    temperature = _number(current.get('temperature_2m'))
    if temperature is None:
        raise ValueError('Missing current temperature')
    for field in ('time', 'weather_code', 'temperature_2m_max', 'temperature_2m_min'):
        if not isinstance(daily.get(field), list) or len(daily[field]) < 7:
            raise ValueError('Incomplete forecast')
    probabilities = daily.get('precipitation_probability_max')
    if not isinstance(probabilities, list):
        probabilities = []
    days = []
    for index in range(7):
        day_date = date.fromisoformat(daily['time'][index])
        if days and (day_date - days[-1]['date']).days != 1:
            raise ValueError('Invalid forecast dates')
        days.append({
            'date': day_date,
            'condition': _condition(daily['weather_code'][index]),
            'maximum': _number(daily['temperature_2m_max'][index]),
            'minimum': _number(daily['temperature_2m_min'][index]),
            'precipitation_probability': _number(probabilities[index], 0, 100)
            if index < len(probabilities) else None,
        })
    try:
        # timezone=auto returns this location's local wall time.
        local_time = datetime.fromisoformat(current.get('time', ''))
    except (TypeError, ValueError):
        local_time = None
    interval = _number(current.get('interval'), 60, 86400)
    return {'status': 'ok', 'days': days, 'current': {
        'temperature': temperature,
        'condition': _condition(current.get('weather_code'), current.get('is_day') != 0),
        'feels_like': _number(current.get('apparent_temperature')),
        'wind_speed': _number(current.get('wind_speed_10m'), 0),
        'precipitation': _number(current.get('precipitation'), 0),
        'rain': _number(current.get('rain'), 0),
        'interval_minutes': interval / 60 if interval is not None else None,
        'local_time': local_time,
    }}


def get_customer_weather(city):
    """Accept a validated city; cache public data, never customer page HTML."""
    city_key = sha256(city.casefold().encode('utf-8')).hexdigest()
    match = _cached(f'customerpanel:weather:v1:city:{city_key}', lambda: _geocode(city))
    if match['status'] != 'ok':
        return match
    location = match['location']
    location_key = f"{location['latitude']}:{location['longitude']}"
    forecast = _cached(
        f'customerpanel:weather:v1:forecast:{location_key}', lambda: _forecast(location),
    )
    return {**forecast, 'location': location}
