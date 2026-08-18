"""Diagnostics file for Papouch integration."""

from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.redact import async_redact_data

from .coordinator import PapouchDataUpdateCoordinator

type PapouchConfigEntry = ConfigEntry[PapouchDataUpdateCoordinator]

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


TO_REDACT = {"password"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: PapouchConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    return {
        "entry_data": async_redact_data(entry.data, TO_REDACT),
        "data": entry.runtime_data.data,
    }
