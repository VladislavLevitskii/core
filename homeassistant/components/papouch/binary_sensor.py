"""Binary sensor platform for the Papouch integration."""

from dataclasses import dataclass
import logging
from typing import cast, override

from aiopapouch import PapouchDevice

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PapouchConfigEntry
from .coordinator import PapouchBaseCoordinator
from .entity import PapouchEntity

PARALLEL_UPDATES = 0
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class PapouchBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Description class of the Papouch binary sensor."""

    data_key: str = ""
    item_id: str = ""
    translation_placeholders: dict[str, str] | None = None


BINARY_SENSOR_TYPES = (
    PapouchBinarySensorEntityDescription(key="input", translation_key="input"),
)

BINARY_SENSOR_MAP = {desc.key: desc for desc in BINARY_SENSOR_TYPES}


def _get_translation_config(
    data_type: str, name_val: str | None
) -> tuple[str | None, dict[str, str] | None]:
    if name_val is not None:
        return f"{data_type}_custom", {"name": name_val}

    base_desc = BINARY_SENSOR_MAP.get(data_type)
    return base_desc.translation_key if base_desc else None, None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PapouchConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensor platform."""
    coordinator = entry.runtime_data
    entities = []

    for device in coordinator.get_devices():
        for sensor_data in device.get_supported_binary_sensors():
            data_type = cast(str, sensor_data.get("type", "input"))
            base_desc = BINARY_SENSOR_MAP.get(data_type)

            if not base_desc:
                _LOGGER.error("Unknown binary sensor type '%s'. Skipping", data_type)
                continue

            item_id = str(sensor_data["item_id"])
            name_val = sensor_data.get("name")
            translation_key, placeholders = _get_translation_config(data_type, name_val)

            description = PapouchBinarySensorEntityDescription(
                key=f"{data_type}_{item_id}",
                data_key=data_type,
                item_id=item_id,
                device_class=base_desc.device_class,
                translation_key=translation_key,
                translation_placeholders=placeholders,
            )
            entities.append(PapouchBinarySensor(coordinator, device, description))

    async_add_entities(entities)


class PapouchBinarySensor(PapouchEntity, BinarySensorEntity):
    """Representation of a generic Papouch binary sensor."""

    entity_description: PapouchBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: PapouchBaseCoordinator,
        device: PapouchDevice,
        description: PapouchBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, device)
        self.entity_description = description
        self._attr_unique_id = (
            f"{self.device.identifier}_{description.data_key}_{description.item_id}"
        )

        if description.translation_placeholders:
            self._attr_translation_placeholders = description.translation_placeholders

    @property
    @override
    def is_on(self) -> bool:
        """Return True if the binary sensor is on."""
        return bool(
            self.coordinator.data.get(self.entity_description.data_key, {}).get(
                self.entity_description.item_id
            )
            == 1
        )
