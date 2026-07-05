#!/usr/bin/env python3
"""Weather context sensor (Open-Meteo, no auth).

Writes one daily Context line so triage can factor weather into surfacing
(e.g. an outdoor calendar event on a rainy day). Not an alert source.
Idempotent: only writes once per local day.

Configure your location via env vars (no personal defaults are shipped):
  SIGNAL_TRIAGE_WEATHER_LAT   latitude, decimal degrees (default: 40.7128)
  SIGNAL_TRIAGE_WEATHER_LON   longitude, decimal degrees (default: -74.0060)
  SIGNAL_TRIAGE_WEATHER_CITY  display name (default: "your location")
"""

from __future__ import annotations

import json
import os
import urllib.request

from _sensorlib import append_signal, load_delta, parse_detected_at, save_delta, utc_iso

LAT = float(os.environ.get("SIGNAL_TRIAGE_WEATHER_LAT", "40.7128"))
LNG = float(os.environ.get("SIGNAL_TRIAGE_WEATHER_LON", "-74.0060"))
CITY = os.environ.get("SIGNAL_TRIAGE_WEATHER_CITY", "your location")

WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "snow showers", 95: "thunderstorm", 96: "thunderstorm w/ hail",
    99: "thunderstorm w/ hail",
}


def main() -> None:
    today = parse_detected_at(utc_iso()).date().isoformat()
    state = load_delta("weather", default={})
    if state.get("last_day") == today:
        return  # already logged today

    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LNG}"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"
        "&timezone=auto&forecast_days=1"
    )
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.load(resp)

    d = data.get("daily", {})
    tmax = d.get("temperature_2m_max", [None])[0]
    tmin = d.get("temperature_2m_min", [None])[0]
    pop = d.get("precipitation_probability_max", [None])[0]
    code = d.get("weather_code", [None])[0]
    desc = WMO.get(code, f"code {code}")

    line = f"{CITY}: {desc}, {tmin:.0f}–{tmax:.0f}°C, precip {pop}%"
    append_signal("weather", [line], category="Context", verbs=["forecast"])
    save_delta("weather", {"last_day": today})


if __name__ == "__main__":
    main()
