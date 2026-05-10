"""
routes/conditions.py — Environmental conditions endpoint.

PURPOSE:
    GET /api/conditions — Return current time period (via astral) and
    optional weather data (via OpenWeatherMap API).
    Used by conditions.js in the frontend.
"""

import os
import time
from datetime import datetime, timedelta

from fastapi import APIRouter
import httpx

from time_rules import _get_sun_times, get_time_period, LOCATION, TIMEZONE, TWILIGHT_MINUTES

import routes as ctx

router = APIRouter(prefix="/api", tags=["conditions"])

# Optional OpenWeatherMap integration
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
_weather_cache = {"data": None, "fetched_at": 0}


@router.get("/conditions")
async def get_conditions():
    """
    Return current environmental conditions:
    - Time periods (sunrise, sunset, current period) via astral
    - Weather data via OpenWeatherMap (if API key is set)
    """
    now = datetime.now(TIMEZONE)
    sun_times = _get_sun_times(now)
    sunrise = sun_times["sunrise"]
    sunset = sun_times["sunset"]
    buffer = timedelta(minutes=TWILIGHT_MINUTES)

    # Day length
    day_length = sunset - sunrise
    day_h = int(day_length.total_seconds() // 3600)
    day_m = int((day_length.total_seconds() % 3600) // 60)

    # Build time period windows for today
    periods = [
        {"name": "Late Night", "icon": "\U0001f311", "start": now.replace(hour=0, minute=0).strftime("%I:%M %p"),
         "end": (sunrise - buffer).strftime("%I:%M %p")},
        {"name": "Twilight", "icon": "\U0001f305", "start": (sunrise - buffer).strftime("%I:%M %p"),
         "end": (sunrise + buffer).strftime("%I:%M %p")},
        {"name": "Daytime", "icon": "\u2600\ufe0f", "start": (sunrise + buffer).strftime("%I:%M %p"),
         "end": (sunset - buffer).strftime("%I:%M %p")},
        {"name": "Twilight", "icon": "\U0001f307", "start": (sunset - buffer).strftime("%I:%M %p"),
         "end": (sunset + buffer).strftime("%I:%M %p")},
        {"name": "Night", "icon": "\U0001f319", "start": (sunset + buffer).strftime("%I:%M %p"),
         "end": "12:00 AM"},
    ]

    current_period = get_time_period(now)

    result = {
        "location": LOCATION["name"] + ", " + LOCATION["region"],
        "date": now.strftime("%A, %B %d, %Y"),
        "time": now.strftime("%I:%M %p"),
        "current_period": current_period,
        "sunrise": sunrise.strftime("%I:%M %p"),
        "sunset": sunset.strftime("%I:%M %p"),
        "day_length": f"{day_h}h {day_m}m",
        "periods": periods,
        "weather": None,
    }

    # Fetch weather from OpenWeatherMap (cached 15 min)
    if OPENWEATHER_API_KEY:
        cache_age = time.time() - _weather_cache["fetched_at"]
        if _weather_cache["data"] and cache_age < 900:
            result["weather"] = _weather_cache["data"]
        else:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        "https://api.openweathermap.org/data/2.5/weather",
                        params={
                            "lat": LOCATION["latitude"],
                            "lon": LOCATION["longitude"],
                            "appid": OPENWEATHER_API_KEY,
                            "units": "metric",
                        },
                        timeout=5,
                    )
                    if resp.status_code == 200:
                        w = resp.json()
                        weather_data = {
                            "temp_c": round(w["main"]["temp"]),
                            "feels_like_c": round(w["main"]["feels_like"]),
                            "humidity": w["main"]["humidity"],
                            "description": w["weather"][0]["description"].title(),
                            "icon": w["weather"][0]["icon"],
                            "wind_speed_kmh": round(w["wind"]["speed"] * 3.6),
                            "visibility_m": w.get("visibility", 10000),
                            "clouds_pct": w["clouds"]["all"],
                        }
                        _weather_cache["data"] = weather_data
                        _weather_cache["fetched_at"] = time.time()
                        result["weather"] = weather_data
            except Exception as e:
                ctx.logger.warning(f"Weather API failed: {e}")

    return result
