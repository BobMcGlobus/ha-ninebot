"""Writable multi-option controls (ride mode, KERS) for a Ninebot scooter."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NinebotCoordinator
from .entity import NinebotEntity
from .ninebot_ble import CtrlIdx
from .ninebot_ble.register import KersLevel, OperationMode


@dataclass(frozen=True, kw_only=True)
class NinebotSelectEntityDescription(SelectEntityDescription):
    """Describes a Ninebot select backed by a control register + enum."""

    register: CtrlIdx
    # option label -> raw register value, and the reverse for reading state.
    to_value: dict[str, int]


_MODE_MAP = {member.name: member.value for member in OperationMode}
_KERS_MAP = {member.name: member.value for member in KersLevel}


SELECTS: tuple[NinebotSelectEntityDescription, ...] = (
    NinebotSelectEntityDescription(
        key="operating_mode",
        name="Ride mode",
        icon="mdi:speedometer",
        register=CtrlIdx.NB_CTL_WORKMODE,
        options=list(_MODE_MAP),
        to_value=_MODE_MAP,
        entity_category=EntityCategory.CONFIG,
    ),
    NinebotSelectEntityDescription(
        key="kers_level",
        name="Recuperation (KERS)",
        icon="mdi:battery-charging",
        register=CtrlIdx.NB_CTL_KERS,
        options=list(_KERS_MAP),
        to_value=_KERS_MAP,
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Ninebot selects."""
    coordinator: NinebotCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(NinebotSelect(coordinator, desc) for desc in SELECTS)


class NinebotSelect(NinebotEntity, SelectEntity):
    """A control register exposed as a select."""

    entity_description: NinebotSelectEntityDescription

    def __init__(
        self, coordinator: NinebotCoordinator, description: NinebotSelectEntityDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"

    @property
    def current_option(self) -> str | None:
        """Return the current option.

        The coordinator stores enum registers as their member name (e.g. "SPORT"),
        which is exactly our option label.
        """
        value = (self.coordinator.data or {}).get(str(self.entity_description.register))
        if value in self.entity_description.to_value:
            return str(value)
        return None

    async def async_select_option(self, option: str) -> None:
        """Change the option."""
        raw = self.entity_description.to_value[option]
        readback = await self.coordinator.async_write_and_verify(
            self.entity_description.register, raw
        )
        # read_reg returns the enum member name for these registers.
        if str(readback) != option:
            raise HomeAssistantError(
                f"Scooter did not accept '{option}' (it now reports {readback})"
            )
