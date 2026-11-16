"""Settings of the Ninebot E2 family on the classic protocol.

An E2 Plus speaks the classic protocol, but only with the Segway app's key and
from the phone's address, and several settings sit somewhere other than on a
G30: 0x7D, the G30's tail light, is the units setting here, and there is no
cruise control. Mapped from captures of the app on an E2 Plus by cl3mca (#17).
The app writes every one with command 0x03 to the controller (0x20) and reads
it straight back.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# An E2 Plus advertises "E2 " and the last four characters of its serial, which
# starts "N2T" (#17).
E2_PREFIXES = ("E2 ", "N2T")


@dataclass(frozen=True, kw_only=True)
class LegacySelectControl:
    """A setting on the controller, written and read by raw address."""

    key: str
    index: int
    options: dict[str, int]
    # Values the scooter may hold that this control never writes. Shown as they
    # are rather than as "unknown".
    shown_only: dict[str, int] = field(default_factory=dict)


E2_SELECT_CONTROLS: tuple[LegacySelectControl, ...] = (
    LegacySelectControl(
        key="Energy recovery", index=0x76, options={"off": 0, "low": 1, "strong": 2}
    ),
    # km/h times ten. The app writes 30, 40 and 50.
    LegacySelectControl(
        key="Start speed",
        index=0x7F,
        options={"3_kmh": 30, "4_kmh": 40, "5_kmh": 50},
        shown_only={"0_kmh": 0, "1_kmh": 10, "2_kmh": 20},
    ),
)

# Ride mode, switched with the button only; the app cannot change it.
E2_RIDE_MODE_INDEX = 0x75
E2_RIDE_MODES: dict[int, str] = {0: "walk", 1: "drive", 2: "sport", 3: "eco"}
E2_RIDE_MODE_KEY = "e2_ride_mode"


def e2_data_key(index: int) -> str:
    """Where a controller setting read by raw address lives in the coordinator's data."""
    return f"e2_{index:02X}"
