"""Support for Ninebot scooter sensors."""
from __future__ import annotations

from homeassistant import config_entries
from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataProcessor,
    PassiveBluetoothDataUpdate,
    PassiveBluetoothProcessorCoordinator,
    PassiveBluetoothProcessorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.sensor import sensor_device_info_to_hass_device_info

from .const import DOMAIN
from .device import device_key_to_bluetooth_entity_key
from .ninebot_ble import SensorUpdate


def sensor_update_to_bluetooth_data_update(
    sensor_update: SensorUpdate,
) -> PassiveBluetoothDataUpdate:
    """Convert a sensor update to a bluetooth data update."""
    return PassiveBluetoothDataUpdate(
        devices={
            device_id: sensor_device_info_to_hass_device_info(device_info)
            for device_id, device_info in sensor_update.devices.items()
        },
        entity_descriptions={
            device_key_to_bluetooth_entity_key(device_key): SensorEntityDescription(
                key=str(device_key),
                device_class=SensorDeviceClass(desc.device_class)
                if desc.device_class is not None
                else None,
                native_unit_of_measurement=desc.native_unit_of_measurement,
            )
            for device_key, desc in sensor_update.entity_descriptions.items()
        },
        entity_data={
            device_key_to_bluetooth_entity_key(device_key): sensor_values.native_value
            for device_key, sensor_values in sensor_update.entity_values.items()
        },
        entity_names={
            device_key_to_bluetooth_entity_key(device_key): sensor_values.name
            for device_key, sensor_values in sensor_update.entity_values.items()
        },
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: config_entries.ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Ninebot sensors."""
    coordinator: PassiveBluetoothProcessorCoordinator = hass.data[DOMAIN][
        entry.entry_id
    ]
    processor = PassiveBluetoothDataProcessor(sensor_update_to_bluetooth_data_update)
    entry.async_on_unload(
        processor.async_add_entities_listener(
            NinebotBluetoothSensorEntity, async_add_entities
        )
    )
    entry.async_on_unload(
        coordinator.async_register_processor(processor, SensorEntityDescription)
    )


class NinebotBluetoothSensorEntity(
    PassiveBluetoothProcessorEntity,
    SensorEntity,
):
    """Representation of a Ninebot sensor.

    The generic base class is left unsubscripted on purpose: the number of type
    parameters on ``PassiveBluetoothDataProcessor`` has changed across Home
    Assistant versions (one arg on older cores, ``[_T, _DataT]`` on newer ones),
    and subscripting it in the class bases raises a ``TypeError`` at import time
    on the mismatched version. The subscription is purely for static typing and
    has no runtime effect, so we omit it to stay compatible with all cores.
    """

    @property
    def native_value(self) -> str | int | None:
        """Return the native value."""
        return self.processor.entity_data.get(self.entity_key)

    @property
    def available(self) -> bool:
        """Return True if entity is available.

        These are sleepy devices that stop broadcasting when not in use, so once
        we have seen the device we always report available and rely on
        ``assumed_state`` to signal that the values may be stale.
        """
        return True

    @property
    def assumed_state(self) -> bool:
        """Return True if the device is no longer broadcasting."""
        return not self.processor.available
