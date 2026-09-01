"""Number platform for the Papouch integration."""

from dataclasses import dataclass
import logging
from typing import cast, override

from aiopapouch import PapouchDevice
import aiopapouch.exceptions as aiopapouch_exceptions

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PapouchConfigEntry
from .coordinator import PapouchBaseCoordinator
from .entity import PapouchEntity
from .exceptions import PapouchAuthError, PapouchCommandError, PapouchConnectionError

PARALLEL_UPDATES = 0
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class PapouchNumberEntityDescription(NumberEntityDescription):
    """Description class of the Papouch number entity."""

    category: str = ""
    item_id: str = ""
    translation_placeholders: dict[str, str] | None = None
    native_min_value: float = 0
    native_max_value: float = 100
    native_step: float = 1
    mode: NumberMode = NumberMode.BOX


NUMBER_TYPES = (
    PapouchNumberEntityDescription(
        key="decrease_counter", translation_key="decrease_counter"
    ),
    PapouchNumberEntityDescription(
        key="output_off_duration", translation_key="output_off_duration"
    ),
    PapouchNumberEntityDescription(
        key="output_on_duration", translation_key="output_on_duration"
    ),
    PapouchNumberEntityDescription(key="set_counter", translation_key="set_counter"),
)

NUMBER_MAP = {desc.key: desc for desc in NUMBER_TYPES}


def _get_translation_config(
    category: str, name_val: str | None
) -> tuple[str | None, dict[str, str] | None]:
    if name_val is not None:
        return f"{category}_custom", {"name": name_val}

    base_desc = NUMBER_MAP.get(category)
    return base_desc.translation_key if base_desc else None, None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PapouchConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the number platform."""
    coordinator = entry.runtime_data
    entities = []

    for device in coordinator.get_devices():
        for number_data in device.get_supported_numbers():
            category = cast(str, number_data["category"])
            base_desc = NUMBER_MAP.get(category)

            if not base_desc:
                _LOGGER.error("Unknown number category '%s'. Skipping", category)
                continue

            item_id = str(number_data["item_id"])
            name_val = number_data.get("name")
            translation_key, placeholders = _get_translation_config(category, name_val)

            description = PapouchNumberEntityDescription(
                key=f"{category}_{item_id}",
                category=category,
                item_id=item_id,
                native_min_value=number_data.get("min_value", 0),
                native_max_value=number_data.get("max_value", 100),
                native_step=number_data.get("step", 1),
                mode=NumberMode(number_data.get("mode", "box")),
                translation_key=translation_key,
                translation_placeholders=placeholders,
            )
            entities.append(PapouchNumber(coordinator, device, description))

    async_add_entities(entities)


class PapouchNumber(PapouchEntity, NumberEntity):
    """Representation of a generic Papouch number entity."""

    entity_description: PapouchNumberEntityDescription

    def __init__(
        self,
        coordinator: PapouchBaseCoordinator,
        device: PapouchDevice,
        description: PapouchNumberEntityDescription,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator, device)
        self.entity_description = description
        self._attr_unique_id = (
            f"{self.device.identifier}_{description.category}_{description.item_id}"
        )

        if description.translation_placeholders:
            self._attr_translation_placeholders = description.translation_placeholders

        self._attr_native_min_value = description.native_min_value
        self._attr_native_max_value = description.native_max_value
        self._attr_native_step = description.native_step
        self._attr_mode = description.mode
        self._current_value: float = 1

    @property
    @override
    def native_value(self) -> float:
        """Return the local value of the number entity."""
        return self._current_value

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Send the update command to the device."""
        try:
            await self.device.set_number_value(
                self.entity_description.category, self.entity_description.item_id, value
            )
            await self.coordinator.async_request_refresh()
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
                    "cmd": f"set_{self.entity_description.category}",
                    "name": self.device.name,
                }
            ) from err

        self._current_value = value
        self.async_write_ha_state()
