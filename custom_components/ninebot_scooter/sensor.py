"""Ninebot scooter sensors, driven by the register map."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NinebotCoordinator
from .entity import NinebotEntity
from .ninebot_ble import BmsIdx, CtrlIdx, get_register_desc, iter_register

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

        descriptions.append(
            NinebotSensorEntityDescription(
                key=key,
                reg_key=key,
                name=key,  # register enum value is a human-readable label
                device_class=device_class,
                native_unit_of_measurement=unit,
                state_class=state_class,
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
    async_add_entities(
        NinebotSensor(coordinator, description) for description in _build_descriptions()
    )


class NinebotSensor(NinebotEntity, SensorEntity):
    """A single register exposed as a sensor."""

    entity_description: NinebotSensorEntityDescription

    def __init__(
        self, coordinator: NinebotCoordinator, description: NinebotSensorEntityDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"

    @property
    def native_value(self) -> str | int | float | None:
        """Return the current register value."""
        return (self.coordinator.data or {}).get(self.entity_description.reg_key)
