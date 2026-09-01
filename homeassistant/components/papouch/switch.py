"""Switch platform for the Papouch integration."""

from dataclasses import dataclass
import logging
from typing import Any, override

from aiopapouch import PapouchDevice
import aiopapouch.exceptions as aiopapouch_exceptions

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PapouchConfigEntry
from .coordinator import PapouchBaseCoordinator
from .entity import PapouchEntity
from .exceptions import PapouchAuthError, PapouchCommandError, PapouchConnectionError

PARALLEL_UPDATES = 0
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class PapouchSwitchEntityDescription(SwitchEntityDescription):
    """Description class of the Papouch switch."""

    item_id: str = ""
    translation_placeholders: dict[str, str] | None = None


SWITCH_TYPES = (PapouchSwitchEntityDescription(key="output", translation_key="output"),)

SWITCH_MAP = {desc.key: desc for desc in SWITCH_TYPES}


def _get_translation_config(
    data_type: str, name_val: str | None
) -> tuple[str | None, dict[str, str] | None]:
    if name_val is not None:
        return f"{data_type}_custom", {"name": name_val}

    base_desc = SWITCH_MAP.get(data_type)
    return base_desc.translation_key if base_desc else None, None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PapouchConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switch platform."""
    coordinator = entry.runtime_data
    entities = []

    for device in coordinator.get_devices():
        for switch_data in device.get_supported_switches():
            data_type = str(switch_data.get("type", "output"))
            base_desc = SWITCH_MAP.get(data_type)

            if not base_desc:
                _LOGGER.error("Unknown switch type '%s'. Skipping", data_type)
                continue

            item_id = str(switch_data["item_id"])
            name_val = switch_data.get("name")
            translation_key, placeholders = _get_translation_config(data_type, name_val)

            description = PapouchSwitchEntityDescription(
                key=item_id,
                item_id=item_id,
                translation_key=translation_key,
                translation_placeholders=placeholders,
            )
            entities.append(PapouchSwitch(coordinator, device, description))

    async_add_entities(entities)


class PapouchSwitch(PapouchEntity, SwitchEntity):
    """Representation of a unified Papouch switch entity."""

    entity_description: PapouchSwitchEntityDescription

    def __init__(
        self,
        coordinator: PapouchBaseCoordinator,
        device: PapouchDevice,
        description: PapouchSwitchEntityDescription,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, device)
        self.entity_description = description
        self._attr_unique_id = f"{self.device.identifier}_{description.item_id}"

        if description.translation_placeholders:
            self._attr_translation_placeholders = description.translation_placeholders

    @property
    @override
    def is_on(self) -> bool | None:
        """Return True if the switch is on."""
        val = self.coordinator.data.get("switch", {}).get(
            self.entity_description.item_id
        )
        return val == 1 if val is not None else None

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        try:
            await self.device.turn_on_switch(self.entity_description.item_id)
        except aiopapouch_exceptions.DeviceAuthError as err:
            raise PapouchAuthError(
                translation_placeholders={
                    "name": self.device.name,
                    "location": self.device.location,
                }
            ) from err
        except aiopapouch_exceptions.DeviceConnectionError as err:
            raise PapouchConnectionError(
                translation_placeholders={
                    "name": self.device.name,
                    "location": self.device.location,
                }
            ) from err
        except aiopapouch_exceptions.DeviceError as err:
            raise PapouchCommandError(
                translation_placeholders={
                    "cmd": f"turn_on_switch_{self.entity_description.item_id}",
                    "name": self.device.name,
                }
            ) from err

        if self.coordinator.data and "switch" in self.coordinator.data:
            self.coordinator.data["switch"][self.entity_description.item_id] = 1

        self.async_write_ha_state()

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        try:
            await self.device.turn_off_switch(self.entity_description.item_id)
        except aiopapouch_exceptions.DeviceAuthError as err:
            raise PapouchAuthError(
                translation_placeholders={
                    "name": self.device.name,
                    "location": self.device.location,
                }
            ) from err
        except aiopapouch_exceptions.DeviceConnectionError as err:
            raise PapouchConnectionError(
                translation_placeholders={
                    "name": self.device.name,
                    "location": self.device.location,
                }
            ) from err
        except aiopapouch_exceptions.DeviceError as err:
            raise PapouchCommandError(
                translation_placeholders={
                    "cmd": f"turn_off_switch_{self.entity_description.item_id}",
                    "name": self.device.name,
                }
            ) from err

        if self.coordinator.data and "switch" in self.coordinator.data:
            self.coordinator.data["switch"][self.entity_description.item_id] = 0

        self.async_write_ha_state()
