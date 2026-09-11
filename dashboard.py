# dashboard.py — live numbers for the GUI: system stats and the weather
#
# Weather comes from Open-Meteo (open-meteo.com): free for personal use, no API key needed.

import os
import time

import psutil
import requests

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather codes -> (what to show, which icon the page draws)
WEATHER_CODES = {
    0: ("Clear sky", "clear"), 1: ("Mainly clear", "clear"), 2: ("Partly cloudy", "partly"), 3: ("Overcast", "cloudy"),
    45: ("Fog", "fog"), 48: ("Freezing fog", "fog"),
    51: ("Light drizzle", "rain"), 53: ("Drizzle", "rain"), 55: ("Heavy drizzle", "rain"),
    56: ("Freezing drizzle", "rain"), 57: ("Freezing drizzle", "rain"),
    61: ("Light rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"), 67: ("Heavy freezing rain", "rain"),
    71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"), 77: ("Snow grains", "snow"),
    80: ("Light showers", "rain"), 81: ("Showers", "rain"), 82: ("Heavy showers", "rain"),
    85: ("Snow showers", "snow"), 86: ("Heavy snow showers", "snow"),
    95: ("Thunderstorm", "storm"), 96: ("Thunderstorm with hail", "storm"), 99: ("Thunderstorm with heavy hail", "storm"),
}

# So "Raleigh, NC" finds the right Raleigh: the geocoder matches full state names, not abbreviations
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}

_places = {}   # city text -> place, so each city is only looked up once


def system_stats() -> dict:
    """CPU, memory and system-drive usage right now."""
    memory = psutil.virtual_memory()
    drive = os.environ.get("SystemDrive", "C:") + "\\" if os.name == "nt" else "/"
    disk = psutil.disk_usage(drive)
    return {
        "cpu": psutil.cpu_percent(interval=None),   # % since the last call, so call it regularly
        "ram_used": memory.total - memory.available,
        "ram_total": memory.total,
        "ram_percent": memory.percent,
        "disk_used": disk.used,
        "disk_total": disk.total,
        "disk_percent": disk.percent,
    }


def find_place(city: str) -> dict:
    """'Raleigh, NC' -> {"name": "Raleigh, NC", "lat": ..., "lon": ...}"""
    key = city.strip().lower()
    if key in _places:
        return _places[key]
    name, _, qualifier = (part.strip() for part in city.partition(","))
    response = requests.get(GEOCODE_URL, params={"name": name, "count": 10, "language": "en", "format": "json"},
                            timeout=10)
    response.raise_for_status()
    results = response.json().get("results") or []
    if not results:
        raise LookupError(f"couldn't find a place called '{city}'")
    place = results[0]   # the most prominent match
    if qualifier:
        wanted = {qualifier.lower(), US_STATES.get(qualifier.upper(), "").lower()} - {""}
        for result in results:
            if wanted & {str(result.get(field, "")).lower() for field in ("admin1", "country", "country_code")}:
                place = result
                break
    label = f"{place['name']}, {qualifier}" if qualifier else ", ".join(
        part for part in (place.get("name"), place.get("admin1") or place.get("country")) if part)
    _places[key] = {"name": label, "lat": place["latitude"], "lon": place["longitude"]}
    return _places[key]


def weather(city: str, units: str = "imperial") -> dict:
    """Current conditions for a city, ready for the weather card."""
    place = find_place(city)
    imperial = units != "metric"
    response = requests.get(FORECAST_URL, timeout=10, params={
        "latitude": place["lat"],
        "longitude": place["lon"],
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,is_day",
        "temperature_unit": "fahrenheit" if imperial else "celsius",
        "wind_speed_unit": "mph" if imperial else "kmh",
        "timezone": "auto",
    })
    response.raise_for_status()
    now = response.json()["current"]
    description, icon = WEATHER_CODES.get(now.get("weather_code"), ("Unknown", "cloudy"))
    if not now.get("is_day", 1) and icon in ("clear", "partly"):
        icon = "night"
    return {
        "city": place["name"],
        "temp": round(now["temperature_2m"]),
        "feels_like": round(now["apparent_temperature"]),
        "humidity": round(now["relative_humidity_2m"]),
        "wind": round(now["wind_speed_10m"]),
        "description": description,
        "icon": icon,
        "temp_unit": "°F" if imperial else "°C",
        "wind_unit": "mph" if imperial else "km/h",
        "updated": time.time(),
    }
