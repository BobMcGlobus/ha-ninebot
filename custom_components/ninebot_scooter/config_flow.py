"""Config and options flow for the Ninebot Scooter integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_POLL_INTERVAL,
    CONF_V2_PASSWORD,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)

# Older Ninebot scooters (E/MAX/F series) advertise with this manufacturer id.
_NINEBOT_MANUFACTURER_ID = 16974

# Newer models (G2/G3/F2/E3 ...) may advertise differently, so also recognise the
# usual advertised name prefixes. Anything not matched can still be picked
# manually - see async_step_user.
_NAME_PREFIXES = ("nbscooter", "ninebot", "segway", "nb-", "nbs")


def _is_ninebot(discovery_info: BluetoothServiceInfoBleak) -> bool:
    """Known-good match: the manufacturer id used by supported models."""
    return _NINEBOT_MANUFACTURER_ID in discovery_info.manufacturer_data


def _looks_like_ninebot(discovery_info: BluetoothServiceInfoBleak) -> bool:
    """Looser match, including models we don't formally support yet."""
    if _is_ninebot(discovery_info):
        return True
    name = (discovery_info.name or "").lower()
    if name.startswith(_NAME_PREFIXES):
        return True
    # Newer models (e.g. Max G3) advertise their serial number as the BLE name,
    # such as "1CGBC2510C1691": 14 alphanumeric characters, no separators.
    raw = discovery_info.name or ""
    return len(raw) == 14 and raw.isalnum() and raw.isupper() and any(c.isdigit() for c in raw)


class NinebotConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ninebot scooters."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, str] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        if not _looks_like_ninebot(discovery_info):
            return self.async_abort(reason="not_supported")
        self._discovery_info = discovery_info
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm discovery."""
        assert self._discovery_info is not None
        title = self._discovery_info.name or self._discovery_info.address
        if user_input is not None:
            return self.async_create_entry(title=title, data={})

        self._set_confirm_only()
        self.context["title_placeholders"] = {"name": title}
        return self.async_show_form(
            step_id="bluetooth_confirm", description_placeholders={"name": title}
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step to pick a discovered device."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._discovered_devices[address], data={}
            )

        current_addresses = self._async_current_ids()
        candidates = [
            info
            for info in async_discovered_service_info(self.hass, False)
            if info.address not in current_addresses
        ]

        # Prefer devices we recognise; if none look like a scooter, offer every
        # nearby device rather than dead-ending. Models we don't formally support
        # yet (e.g. newer G-series) can then at least be attempted, and the
        # resulting logs/diagnostics tell us what they speak.
        recognised = [info for info in candidates if _looks_like_ninebot(info)]
        showing_all = not recognised
        for info in recognised or candidates:
            self._discovered_devices[info.address] = info.name or info.address

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(self._discovered_devices)}
            ),
            description_placeholders={
                "note": (
                    "No known Ninebot scooter was found nearby, so all Bluetooth "
                    "devices are listed. Picking an unsupported model may not work "
                    "- please share the diagnostics if it fails."
                )
                if showing_all
                else ""
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> NinebotOptionsFlow:
        """Get the options flow for this handler."""
        return NinebotOptionsFlow()


class NinebotOptionsFlow(OptionsFlow):
    """Handle options (poll interval)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            # A pairing password belongs with the device rather than the options,
            # and the protocol layer needs it as 16 raw bytes - so reject anything
            # that isn't, instead of breaking setup later.
            password = str(user_input.pop(CONF_V2_PASSWORD, "") or "").replace(" ", "")
            if password:
                try:
                    if len(bytes.fromhex(password)) != 16:
                        raise ValueError
                except ValueError:
                    errors[CONF_V2_PASSWORD] = "invalid_password"
            if not errors:
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={**self.config_entry.data, CONF_V2_PASSWORD: password}
                    if password
                    else {
                        k: v
                        for k, v in self.config_entry.data.items()
                        if k != CONF_V2_PASSWORD
                    },
                )
                return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(
            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_POLL_INTERVAL, default=current): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_POLL_INTERVAL,
                        max=MAX_POLL_INTERVAL,
                        step=5,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                # Newer vehicles that are already linked to the official app will
                # not accept a new pairing. If you can recover the password the
                # app uses, this is where it goes.
                vol.Optional(
                    CONF_V2_PASSWORD,
                    default=self.config_entry.data.get(CONF_V2_PASSWORD, ""),
                ): str,
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )
