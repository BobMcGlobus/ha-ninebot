"""Ninebot scooter sensors, driven by the register map."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, PROTOCOL_V2
from .coordinator import NinebotCoordinator
from .entity import NinebotEntity
from .ninebot_ble import BmsIdx, CtrlIdx, get_register_desc, iter_register
from .ninebot_ble.protocol_v2 import V2_REGISTERS

# Registers exposed as writable controls (switch/select/number) instead of sensors.
_CONTROL_KEYS: set[str] = {
    str(CtrlIdx.NB_CTL_WORKMODE),
    str(CtrlIdx.NB_CTL_KERS),
    str(CtrlIdx.NB_CTL_CRUISE),
    str(CtrlIdx.NB_CTL_TAIL_LIGHT),
}

# Monotonic totals -> total_increasing for long-term statistics.
_TOTAL_KEYS: set[str] = {
    str(CtrlIdx.NB_INF_RID_MIL),
    str(CtrlIdx.NB_INF_RUN_TIM),
    str(CtrlIdx.NB_INF_RID_TIM),
}

# --- Entity presentation ----------------------------------------------------
# A scooter exposes ~45 registers. Only a handful are interesting day to day, so
# keep those primary, put a few useful ones under Diagnostics, and ship the long
# tail disabled by default (users can enable any of them per entity).

# Primary sensors, shown at the top of the device page.
_PRIMARY_KEYS: tuple[str, ...] = (
    str(BmsIdx.BAT_REMAINING_CAP_PERCENT),  # battery %
    str(CtrlIdx.NB_INF_RID_MIL),  # total mileage / odometer
    str(CtrlIdx.NB_INF_ACTUAL_MIL),  # actual remaining mileage
)

# Diagnostic, but visible by default.
_DIAGNOSTIC_ENABLED_KEYS: frozenset[str] = frozenset(
    {
        str(BmsIdx.BAT_HEALTHY),  # battery health
        str(CtrlIdx.NB_INF_RUN_TIM),  # total operation time
        str(CtrlIdx.NB_INF_RID_TIM),  # total riding time
        str(CtrlIdx.NB_INF_SN),  # scooter serial number
        str(CtrlIdx.NB_FW_VER),  # controller firmware
        str(CtrlIdx.NB_INF_VER_BLE),  # BLE firmware
    }
)


@dataclass(frozen=True, kw_only=True)
class NinebotSensorEntityDescription(SensorEntityDescription):
    """Sensor description carrying the register key to read from coordinator data."""

    reg_key: str


def _build_descriptions() -> list[NinebotSensorEntityDescription]:
    descriptions: list[NinebotSensorEntityDescription] = []
    for idx in iter_register(CtrlIdx, BmsIdx):
        key = str(idx)
        if key in _CONTROL_KEYS:
            continue
        reg = get_register_desc(idx)

        device_class = (
            SensorDeviceClass(str(reg.device_class)) if reg.device_class is not None else None
        )
        unit = str(reg.unit) if reg.unit is not None else None

        if key in _TOTAL_KEYS:
            state_class: SensorStateClass | None = SensorStateClass.TOTAL_INCREASING
        elif unit is not None:
            state_class = SensorStateClass.MEASUREMENT
        else:
            state_class = None

        is_primary = key in _PRIMARY_KEYS
        descriptions.append(
            NinebotSensorEntityDescription(
                key=key,
                reg_key=key,
                name=key,  # register enum value is a human-readable label
                device_class=device_class,
                native_unit_of_measurement=unit,
                state_class=state_class,
                entity_category=None if is_primary else EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=(
                    is_primary or key in _DIAGNOSTIC_ENABLED_KEYS
                ),
            )
        )
    # Keep the primary sensors in a predictable order at the top.
    descriptions.sort(
        key=lambda d: _PRIMARY_KEYS.index(d.key) if d.key in _PRIMARY_KEYS else len(_PRIMARY_KEYS)
    )
    return descriptions


def _build_v2_descriptions() -> list[NinebotSensorEntityDescription]:
    """Descriptions for vehicles speaking the newer protocol."""
    descriptions: list[NinebotSensorEntityDescription] = []
    for reg in V2_REGISTERS:
        descriptions.append(
            NinebotSensorEntityDescription(
                key=reg.key,
                reg_key=reg.key,
                name=reg.key,
                device_class=(
                    SensorDeviceClass(reg.device_class) if reg.device_class else None
                ),
                native_unit_of_measurement=reg.unit,
                state_class=(
                    SensorStateClass.TOTAL_INCREASING
                    if reg.key == "Total mileage"
                    else SensorStateClass.MEASUREMENT
                    if reg.unit
                    else None
                ),
                entity_category=None if reg.primary else EntityCategory.DIAGNOSTIC,
            )
        )
    return descriptions


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Ninebot sensors."""
    coordinator: NinebotCoordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.protocol == PROTOCOL_V2:
        descriptions = _build_v2_descriptions()
    else:
        descriptions = _build_descriptions()

    entities: list[SensorEntity] = [
        NinebotSensor(coordinator, description) for description in descriptions
    ]
    entities.append(NinebotLastUpdateSensor(coordinator))
    async_add_entities(entities)


class NinebotSensor(NinebotEntity, RestoreSensor):
    """A single register exposed as a sensor.

    Uses RestoreSensor so the last value survives a Home Assistant restart until
    the scooter is next awake and polled.
    """

    entity_description: NinebotSensorEntityDescription

    def __init__(
        self, coordinator: NinebotCoordinator, description: NinebotSensorEntityDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._restored_value: str | int | float | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the previous value on startup."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._restored_value = last.native_value

    @property
    def native_value(self) -> str | int | float | None:
        """Return the current register value, or the last known one."""
        current = (self.coordinator.data or {}).get(self.entity_description.reg_key)
        return current if current is not None else self._restored_value

    @property
    def available(self) -> bool:
        """Available whenever we have any value to show (current or restored)."""
        return self.native_value is not None


class NinebotLastUpdateSensor(NinebotEntity, RestoreSensor):
    """Timestamp of the last successful poll (i.e. how fresh the values are)."""

    _attr_name = "Last updated"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: NinebotCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_last_updated"
        self._restored: datetime | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the previous timestamp on startup."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            value = last.native_value
            if isinstance(value, str):
                value = dt_util.parse_datetime(value)
            if isinstance(value, datetime):
                self._restored = value

    @property
    def native_value(self) -> datetime | None:
        """Return the time of the last successful poll."""
        return self.coordinator.last_update_time or self._restored

    @property
    def available(self) -> bool:
        """Available once we have ever polled successfully."""
        return self.native_value is not None
