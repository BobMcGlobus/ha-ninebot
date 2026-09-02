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
    Platform.BINARY_SENSOR,
]

# Speed override bounds, in km/h. The ceiling is what the Max hardware itself is
# built for (the unrestricted G30P variant tops out here); the floor keeps the
# scooter rideable.
MIN_SPEED_LIMIT = 6
MAX_SPEED_LIMIT = 30

# Entry data
CONF_APP_KEY = "app_key"  # hex-encoded 16-byte pairing key, persisted per entry
CONF_PROTOCOL = "protocol"
CONF_V2_PASSWORD = "v2_password"  # hex-encoded session password for newer models
CONF_V2_GENERATION = "v2_generation"  # which protocol generation the vehicle uses
CONF_V2_BOARD = "v2_board"  # which board answers register reads on this vehicle

# Which BLE dialect a vehicle speaks. Detected on the first connection and then
# remembered, because it decides which entities exist.
PROTOCOL_LEGACY = "legacy"  # E / MAX (G30) / F series - confirmed working
PROTOCOL_V2 = "v2"  # Max G3, G2, E-series mopeds - under active development

# Options
CONF_POLL_INTERVAL = "poll_interval"
DEFAULT_POLL_INTERVAL = 30  # seconds
MIN_POLL_INTERVAL = 10
MAX_POLL_INTERVAL = 3600
