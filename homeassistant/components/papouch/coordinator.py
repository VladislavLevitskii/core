"""Data update coordinator for the Papouch integration."""

from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any, override

from aiopapouch.exceptions import DeviceAuthError, DeviceConnectionError
from pap_spinel import SpinelTransportError

from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

if TYPE_CHECKING:
    from aiopapouch import PapouchDevice, PapouchHTTPClient, PapouchSerialClient

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class PapouchBaseCoordinator(DataUpdateCoordinator):
    """Base class for Papouch data update coordinators."""

    def get_devices(self) -> list[PapouchDevice]:
        """Return a list of all managed devices."""
        raise NotImplementedError

    async def async_close(self) -> None:
        """Close resources used by the coordinator."""


class PapouchNetworkDataUpdateCoordinator(PapouchBaseCoordinator):
    """Class to manage fetching Papouch network data."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_client: PapouchHTTPClient,
        entry: ConfigEntry,
        device: PapouchDevice,
    ) -> None:
        """Initialize the coordinator."""
        interval = entry.options.get("refresh_rate", DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=interval),
        )
        self.api_client = api_client
        self.device = device

    @override
    def get_devices(self) -> list[PapouchDevice]:
        """Return the single network device as a list."""
        return [self.device]

    @override
    async def _async_update_data(self) -> dict:
        """Fetch data from the device."""
        try:
            fresh_data = await self.api_client.fetch_data()
            return await self.device.parse_fresh_data(fresh_data)
        except DeviceAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="invalid_auth",
                translation_placeholders={
                    "name": self.device.name,
                    "location": self.device.location,
                },
            ) from err
        except DeviceConnectionError as err:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={
                    "name": self.device.name,
                    "location": self.device.location,
                },
            ) from err


class PapouchSerialDataUpdateCoordinator(PapouchBaseCoordinator):
    """Class to manage fetching Papouch data for a group of serial devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_client: PapouchSerialClient,
        entry: ConfigEntry,
        devices: list[PapouchDevice],
    ) -> None:
        """Initialize the serial coordinator."""
        interval = entry.options.get("refresh_rate", DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=interval),
        )
        self.api_client = api_client
        self.devices = devices

    @override
    def get_devices(self) -> list[PapouchDevice]:
        """Return all managed serial devices."""
        return self.devices

    @override
    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from all devices in the group."""
        device_data = {}
        if self.config_entry is None or self.config_entry.data is None:
            port = "Unknown"
        else:
            port = self.config_entry.data.get("port", "Unknown")

        for device in self.devices:
            try:
                device_data[device.identifier] = await device.parse_fresh_data("")
            except DeviceConnectionError as err:
                raise ConfigEntryNotReady(
                    translation_domain=DOMAIN,
                    translation_key="cannot_connect",
                    translation_placeholders={
                        "name": device.name,
                        "location": port,
                    },
                ) from err

        return device_data

    @override
    async def async_close(self) -> None:
        """Close the serial port."""
        try:
            await self.api_client.close()
        except SpinelTransportError as err:
            _LOGGER.warning("Could not close serial port: %s", err)
