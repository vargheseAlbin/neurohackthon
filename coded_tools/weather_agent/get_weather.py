"""CodedTool that fetches a weather forecast from the keyless Open-Meteo API."""

import logging
import ssl
from typing import Any

import httpx
from neuro_san.interfaces.coded_tool import CodedTool

try:
    # On a machine behind a TLS-inspecting corporate proxy, certifi's bundle does not
    # contain the proxy's root CA and every HTTPS call fails verification. truststore
    # defers to the OS trust store, which does have it. Harmless elsewhere.
    import truststore

    SSL_CONTEXT: ssl.SSLContext | None = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
except ImportError:  # pragma: no cover - truststore ships with the studio deps
    SSL_CONTEXT = None

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_SECONDS = 15.0
MAX_FORECAST_DAYS = 7

# Open-Meteo WMO weather codes, collapsed to the descriptions an LLM can narrate.
WEATHER_CODES: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snowfall",
    73: "moderate snowfall",
    75: "heavy snowfall",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}

logger = logging.getLogger(__name__)


class GetWeather(CodedTool):
    """
    Looks up a place name and returns its current conditions plus a daily forecast.

    Uses Open-Meteo, which needs no API key, so this tool is a self-contained
    example of an API-calling CodedTool.
    """

    async def async_invoke(self, args: dict[str, Any], sly_data: dict[str, Any]) -> Any:
        """
        :param args: Expects:
                "location": required place name, e.g. "Chennai" or "Austin, Texas".
                "days": optional number of forecast days, 1-7. Defaults to 3.
        :param sly_data: Unused by this tool.
        :return: A dict of weather data on success, or an error string that the
                calling agent can relay to the user.
        """
        location: str = str(args.get("location") or "").strip()
        if not location:
            return "Error: no location was provided."

        days: int = self._clamp_days(args.get("days"))

        try:
            verify: Any = SSL_CONTEXT if SSL_CONTEXT is not None else True
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, verify=verify) as client:
                place = await self._geocode(client, location)
                if place is None:
                    return f"Error: could not find a place called '{location}'."
                return await self._forecast(client, place, days)
        except httpx.HTTPError as error:
            logger.warning("Weather lookup for %s failed: %s", location, error)
            return f"Error: the weather service could not be reached ({error})."

    @staticmethod
    def _clamp_days(raw: Any) -> int:
        """Coerce the caller's "days" argument into the range Open-Meteo accepts."""
        try:
            days = int(raw)
        except (TypeError, ValueError):
            return 3
        return max(1, min(MAX_FORECAST_DAYS, days))

    @staticmethod
    async def _geocode(client: httpx.AsyncClient, location: str) -> dict[str, Any] | None:
        """Resolve a place name to the first matching geocoding result, or None."""
        response = await client.get(GEOCODE_URL, params={"name": location, "count": 1, "language": "en"})
        response.raise_for_status()
        results: list[dict[str, Any]] = response.json().get("results") or []
        return results[0] if results else None

    @staticmethod
    async def _forecast(client: httpx.AsyncClient, place: dict[str, Any], days: int) -> dict[str, Any]:
        """Fetch current conditions and a daily forecast for an already-geocoded place."""
        params = {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_days": days,
            "timezone": "auto",
        }
        response = await client.get(FORECAST_URL, params=params)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()

        current: dict[str, Any] = payload.get("current") or {}
        daily: dict[str, Any] = payload.get("daily") or {}
        dates: list[str] = daily.get("time") or []

        return {
            "location": ", ".join(
                part for part in (place.get("name"), place.get("admin1"), place.get("country")) if part
            ),
            "timezone": payload.get("timezone"),
            "units": {"temperature": "°C", "wind_speed": "km/h", "humidity": "%"},
            "current": {
                "temperature": current.get("temperature_2m"),
                "humidity": current.get("relative_humidity_2m"),
                "wind_speed": current.get("wind_speed_10m"),
                "conditions": WEATHER_CODES.get(current.get("weather_code"), "unknown"),
            },
            "forecast": [
                {
                    "date": date,
                    "conditions": WEATHER_CODES.get(GetWeather._at(daily, "weather_code", index), "unknown"),
                    "high": GetWeather._at(daily, "temperature_2m_max", index),
                    "low": GetWeather._at(daily, "temperature_2m_min", index),
                    "precipitation_chance": GetWeather._at(daily, "precipitation_probability_max", index),
                }
                for index, date in enumerate(dates)
            ],
        }

    @staticmethod
    def _at(daily: dict[str, Any], key: str, index: int) -> Any:
        """Read one day out of a parallel array, tolerating short or missing series."""
        series: list[Any] = daily.get(key) or []
        return series[index] if index < len(series) else None
