"""Select platform for the Papouch integration."""

from dataclasses import dataclass
import logging
from typing import cast, override

from aiopapouch import PapouchDevice
import aiopapouch.exceptions as aiopapouch_exceptions

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PapouchConfigEntry
from .coordinator import PapouchBaseCoordinator
from .entity import PapouchEntity
from .exceptions import PapouchAuthError, PapouchCommandError, PapouchConnectionError

PARALLEL_UPDATES = 0
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class PapouchSelectEntityDescription(SelectEntityDescription):
    """Description class of the Papouch select entity."""

    category: str = ""
    item_id: str = ""
    translation_placeholders: dict[str, str] | None = None


SELECT_TYPES = (
    PapouchSelectEntityDescription(key="counter_mode", translation_key="counter_mode"),
    PapouchSelectEntityDescription(key="sensor_type", translation_key="sensor_type"),
    PapouchSelectEntityDescription(
        key="sensor_type_meteo_ab", translation_key="sensor_type_meteo_ab"
    ),
    PapouchSelectEntityDescription(
        key="sensor_type_meteo_c", translation_key="sensor_type_meteo_c"
    ),
)

SELECT_MAP = {desc.key: desc for desc in SELECT_TYPES}


def _get_translation_config(
    category: str, name_val: str | None
) -> tuple[str | None, dict[str, str] | None]:
    if name_val is not None:
        return f"{category}_custom", {"name": name_val}

    base_desc = SELECT_MAP.get(category)
    return base_desc.translation_key if base_desc else None, None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PapouchConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the select platform."""
    coordinator = entry.runtime_data
    entities = []

    for device in coordinator.get_devices():
        for select_data in device.get_supported_selects():
            category = cast(str, select_data["category"])
            base_desc = SELECT_MAP.get(category)

            if not base_desc:
                _LOGGER.error("Unknown select category '%s'. Skipping", category)
                continue

            item_id = str(select_data["item_id"])
            name_val = select_data.get("name")
            translation_key, placeholders = _get_translation_config(category, name_val)

            description = PapouchSelectEntityDescription(
                key=f"{category}_{item_id}",
                category=category,
                item_id=item_id,
                options=select_data.get("options", []),
                translation_key=translation_key,
                translation_placeholders=placeholders,
            )
            entities.append(PapouchSelectEntity(coordinator, device, description))

    async_add_entities(entities)


class PapouchSelectEntity(PapouchEntity, SelectEntity):
    """Representation of a unified Papouch select entity."""

    entity_description: PapouchSelectEntityDescription

    def __init__(
        self,
        coordinator: PapouchBaseCoordinator,
        device: PapouchDevice,
        description: PapouchSelectEntityDescription,
    ) -> None:
        """Initialize the select entity."""
        super().__init__(coordinator, device)
        self.entity_description = description
        self._attr_unique_id = (
            f"{self.device.identifier}_{description.category}_{description.item_id}"
        )

        if description.translation_placeholders:
            self._attr_translation_placeholders = description.translation_placeholders

    @property
    @override
    def current_option(self) -> str | None:
        """Return the currently selected option."""
        return self.device.get_select_option(
            self.entity_description.category, self.entity_description.item_id
        )

    @override
    async def async_select_option(self, option: str) -> None:
        """Change the selected option on the device."""

        try:
            await self.device.set_select_option(
                self.entity_description.category,
                self.entity_description.item_id,
                option,
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
                    "cmd": f"select_{self.entity_description.category}",
                    "name": self.device.name,
                }
            ) from err
