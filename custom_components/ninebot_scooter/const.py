"""Constants for the Ninebot Scooter integration."""
from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "ninebot_scooter"

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.LOCK,
]

# Speed override bounds, in km/h. The ceiling is what the Max hardware itself is
# built for (the unrestricted G30P variant tops out here); the floor keeps the
# scooter rideable.
MIN_SPEED_LIMIT = 6
MAX_SPEED_LIMIT = 30

# Entry data
CONF_APP_KEY = "app_key"  # hex-encoded 16-byte pairing key, persisted per entry

# Options
CONF_POLL_INTERVAL = "poll_interval"
DEFAULT_POLL_INTERVAL = 30  # seconds
MIN_POLL_INTERVAL = 10
MAX_POLL_INTERVAL = 3600
