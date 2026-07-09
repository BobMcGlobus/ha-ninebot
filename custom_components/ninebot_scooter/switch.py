"""Writable on/off controls for a Ninebot scooter."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NinebotCoordinator
from .entity import NinebotEntity
from .ninebot_ble import CtrlIdx


@dataclass(frozen=True, kw_only=True)
class NinebotSwitchEntityDescription(SwitchEntityDescription):
    """Describes a Ninebot switch backed by a control register."""

    register: CtrlIdx
    on_value: int = 1
    off_value: int = 0


SWITCHES: tuple[NinebotSwitchEntityDescription, ...] = (
    NinebotSwitchEntityDescription(
        key="cruise_control",
        name="Cruise control",
        icon="mdi:cruise-control",
        register=CtrlIdx.NB_CTL_CRUISE,
        entity_category=EntityCategory.CONFIG,
    ),
    NinebotSwitchEntityDescription(
        key="tail_light",
        name="Tail light",
        icon="mdi:car-light-high",
        register=CtrlIdx.NB_CTL_TAIL_LIGHT,
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Ninebot switches."""
    coordinator: NinebotCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(NinebotSwitch(coordinator, desc) for desc in SWITCHES)


class NinebotSwitch(NinebotEntity, SwitchEntity):
    """A control register exposed as a switch."""

    entity_description: NinebotSwitchEntityDescription

    def __init__(
        self, coordinator: NinebotCoordinator, description: NinebotSwitchEntityDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return True if the control is on."""
        value = (self.coordinator.data or {}).get(str(self.entity_description.register))
        if value is None:
            return None
        return int(value) != self.entity_description.off_value

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the control on."""
        await self.coordinator.async_write_register(
            self.entity_description.register, self.entity_description.on_value
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the control off."""
        await self.coordinator.async_write_register(
            self.entity_description.register, self.entity_description.off_value
        )
