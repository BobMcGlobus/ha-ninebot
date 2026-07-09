"""Constants for the Ninebot Scooter integration."""
from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "ninebot_scooter"

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
]

# Options
CONF_POLL_INTERVAL = "poll_interval"
DEFAULT_POLL_INTERVAL = 30  # seconds
MIN_POLL_INTERVAL = 10
MAX_POLL_INTERVAL = 3600
