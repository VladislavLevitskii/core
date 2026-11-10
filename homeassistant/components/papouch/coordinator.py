"""Data update coordinator for the Papouch integration."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any, override

from aiopapouch.exceptions import (
    DeviceAuthError,
    DeviceConnectionError,
    DeviceLogicError,
    DeviceParseError,
)
from pap_spinel import SpinelTransportError

from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

if TYPE_CHECKING:
    from aiopapouch import (
        PapouchDevice,
        PapouchHTTPClient,
        PapouchNetworkDevice,
        PapouchSerialClient,
        PapouchSerialDevice,
    )

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


type PapouchConfigEntry = ConfigEntry[PapouchBaseCoordinator]


class PapouchBaseCoordinator(DataUpdateCoordinator, ABC):
    """Base class for Papouch data update coordinators."""

    @abstractmethod
    def get_devices(self) -> Sequence[PapouchDevice]:
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
        entry: PapouchConfigEntry,
        device: PapouchNetworkDevice,
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
        self.translations = {
            "name": device.conf.name,
            "location": device.conf.location,
        }

    @override
    def get_devices(self) -> Sequence[PapouchNetworkDevice]:
        """Return the single network device as a list."""
        return [self.device]

    @override
    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the device."""
        try:
            parsed_data = await self.device.get_fresh_data()
        except DeviceAuthError as err:
            _LOGGER.debug(
                "Retrieving network fresh data is failed on auth error, err: %s", err
            )
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="invalid_auth_with_info",
                translation_placeholders=self.translations,
            ) from err
        except DeviceConnectionError as err:
            _LOGGER.debug(
                "Retrieving network fresh data is failed on connection error, err: %s",
                err,
            )
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="cannot_connect_http",
                translation_placeholders=self.translations,
            ) from err
        except DeviceParseError as err:
            _LOGGER.debug(
                "Retrieving network fresh data is failed on parsing error, err: %s", err
            )
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="parsing_error_with_info",
                translation_placeholders=self.translations,
            ) from err

        return {self.device.conf.identifier: parsed_data}


class PapouchSerialDataUpdateCoordinator(PapouchBaseCoordinator):
    """Class to manage fetching Papouch data for a group of serial devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_client: PapouchSerialClient,
        entry: PapouchConfigEntry,
        devices: list[PapouchSerialDevice],
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
    def get_devices(self) -> Sequence[PapouchSerialDevice]:
        """Return all managed serial devices."""
        return self.devices

    @override
    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from all devices in the group."""
        device_data = {}

        for device in self.devices:
            try:
                device_data[device.conf.identifier] = await device.get_fresh_data()
            except DeviceConnectionError as err:
                _LOGGER.debug(
                    "Retrieving serial fresh data is failed on connection error, err: %s",
                    err,
                )
                raise ConfigEntryNotReady(
                    translation_domain=DOMAIN,
                    translation_key="cannot_connect_with_info",
                    translation_placeholders={
                        "name": device.conf.name,
                        "location": device.conf.identifier,
                    },
                ) from err
            except DeviceParseError as err:
                _LOGGER.debug(
                    "Retrieving serial fresh data is failed on parsing error, err: %s",
                    err,
                )
                raise ConfigEntryNotReady(
                    translation_domain=DOMAIN,
                    translation_key="parsing_error_with_info",
                    translation_placeholders={
                        "name": device.conf.name,
                        "location": device.conf.identifier,
                    },
                ) from err
            except DeviceLogicError as err:
                _LOGGER.debug(
                    "Retrieving serial fresh data is failed on logic error, err: %s",
                    err,
                )
                raise ConfigEntryNotReady(
                    translation_domain=DOMAIN,
                    translation_key="logic_error_with_info",
                    translation_placeholders={
                        "name": device.conf.name,
                        "location": device.conf.identifier,
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
