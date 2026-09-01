"""Button platform for the Papouch integration."""

from dataclasses import dataclass
import logging
from typing import cast, override

from aiopapouch import PapouchDevice
import aiopapouch.exceptions as aiopapouch_exceptions

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PapouchConfigEntry
from .coordinator import PapouchBaseCoordinator
from .entity import PapouchEntity
from .exceptions import PapouchAuthError, PapouchCommandError, PapouchConnectionError

PARALLEL_UPDATES = 0
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class PapouchButtonEntityDescription(ButtonEntityDescription):
    """Description class of the Papouch button entity."""

    cmd_type: str = ""
    translation_placeholders: dict[str, str] | None = None


BUTTON_TYPES = (
    PapouchButtonEntityDescription(
        key="connect_all_coils", translation_key="connect_all_coils"
    ),
    PapouchButtonEntityDescription(
        key="disconnect_all_coils", translation_key="disconnect_all_coils"
    ),
    PapouchButtonEntityDescription(
        key="reset_all_counters", translation_key="reset_all_counters"
    ),
    PapouchButtonEntityDescription(key="set_sensor", translation_key="set_sensor"),
)

BUTTON_MAP = {desc.key: desc for desc in BUTTON_TYPES}


def _get_translation_config(
    cmd: str, name_val: str | None
) -> tuple[str | None, dict[str, str] | None]:
    if cmd.startswith("set_sensor_"):
        if name_val is not None:
            return "set_sensor_custom", {"name": name_val}
        return "set_sensor", None

    base_desc = BUTTON_MAP.get(cmd)
    return base_desc.translation_key if base_desc else None, None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PapouchConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the button platform."""
    coordinator = entry.runtime_data
    entities = []

    for device in coordinator.get_devices():
        for btn_data in device.get_supported_buttons():
            cmd = cast(str, btn_data["cmd"])

            is_sensor_btn = cmd.startswith("set_sensor_")
            map_key = "set_sensor" if is_sensor_btn else cmd
            base_desc = BUTTON_MAP.get(map_key)

            if not base_desc:
                _LOGGER.error("Unknown button command '%s'. Skipping", cmd)
                continue

            name_val = btn_data.get("name")
            translation_key, placeholders = _get_translation_config(cmd, name_val)

            description = PapouchButtonEntityDescription(
                key=cmd,
                cmd_type=cmd,
                translation_key=translation_key,
                translation_placeholders=placeholders,
            )
            entities.append(PapouchCommandButton(coordinator, device, description))

    async_add_entities(entities)


class PapouchCommandButton(PapouchEntity, ButtonEntity):
    """Representation of a generic Papouch button entity."""

    entity_description: PapouchButtonEntityDescription

    def __init__(
        self,
        coordinator: PapouchBaseCoordinator,
        device: PapouchDevice,
        description: PapouchButtonEntityDescription,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator, device)
        self.entity_description = description
        self._attr_unique_id = f"{self.device.identifier}_btn_{description.cmd_type}"

        if description.translation_placeholders:
            self._attr_translation_placeholders = description.translation_placeholders

    @override
    async def async_press(self) -> None:
        """Execute the command associated with the button."""
        try:
            await self.device.execute_button_command(self.entity_description.cmd_type)
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
                    "cmd": self.entity_description.cmd_type,
                    "name": self.device.name,
                }
            ) from err
