"""Initialization file of the integration."""

import logging
from typing import TYPE_CHECKING

import aiohttp
from aiopapouch import (
    PapouchHTTPClient,
    PapouchSerialClient,
    create_network_device,
    create_serial_device,
)
from aiopapouch.exceptions import DeviceConnectionError
from pap_spinel import SerialTransport, SpinelClient, SpinelTransportError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEFAULT_WEB_PORT, DOMAIN, UNKNOWN_LOCATION, UNKNOWN_NAME
from .coordinator import (
    PapouchBaseCoordinator,
    PapouchNetworkDataUpdateCoordinator,
    PapouchSerialDataUpdateCoordinator,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

_LOGGER = logging.getLogger(__name__)


type PapouchConfigEntry = ConfigEntry[PapouchBaseCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: PapouchConfigEntry) -> bool:
    """Set up Papouch device from a config entry."""
    entry.async_on_unload(entry.add_update_listener(update_listener))

    coordinator: PapouchBaseCoordinator

    connection_type = entry.data.get("connection_type", "network")

    if connection_type == "network":
        session = async_get_clientsession(hass)
        password = entry.data.get("password", "")
        web_port = entry.data.get("web_port", DEFAULT_WEB_PORT)
        api_client = PapouchHTTPClient(
            entry.data["ip_address"], session, password=password, web_port=web_port
        )

        name, location = await api_client.get_device_info()
        safe_name = name or UNKNOWN_NAME
        safe_location = location or UNKNOWN_LOCATION

        try:
            device = await create_network_device(api_client)
        except aiohttp.ClientResponseError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="invalid_auth",
                translation_placeholders={
                    "name": safe_name,
                    "location": safe_location,
                },
            ) from err

        except aiohttp.ClientError as err:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"name": safe_name, "location": safe_location},
            ) from err

        if device is None:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="unsupported_device",
                translation_placeholders={"name": safe_name, "location": safe_location},
            )

        if entry.unique_id is None and device.identifier:
            hass.config_entries.async_update_entry(entry, unique_id=device.identifier)

        device_registry = dr.async_get(hass)
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            connections={(dr.CONNECTION_NETWORK_MAC, device.identifier)},
            identifiers={(DOMAIN, device.identifier)},
            name=device.name,
            manufacturer=device.manufacturer,
            model=device.name,
            suggested_area=device.location,
        )

        coordinator = PapouchNetworkDataUpdateCoordinator(
            hass, api_client, entry, device
        )

    elif connection_type == "serial":
        port = entry.data["port"]
        baudrate = entry.data["baudrate"]

        serial_client = PapouchSerialClient(
            SpinelClient(SerialTransport(port, baudrate))
        )

        try:
            await serial_client.open()
        except SpinelTransportError as err:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="unable_open_port",
                translation_placeholders={"port": port},
            ) from err

        devices_config = entry.options.get("devices", [])
        devices = []

        device_registry = dr.async_get(hass)

        expected_serial_numbers = {
            dev_conf["serial_number"] for dev_conf in devices_config
        }

        existing_devices = dr.async_entries_for_config_entry(
            device_registry, entry.entry_id
        )

        for device_entry in existing_devices:
            for domain, device_id in device_entry.identifiers:
                if domain == DOMAIN:
                    if device_id != port and device_id not in expected_serial_numbers:
                        device_registry.async_remove_device(device_entry.id)
                    break

        for dev_conf in devices_config:
            address = dev_conf["address"]
            serial_number = dev_conf["serial_number"]
            name = dev_conf["name"]

            try:
                device = await create_serial_device(serial_client, address)
            except DeviceConnectionError as err:
                raise ConfigEntryNotReady(
                    translation_domain=DOMAIN,
                    translation_key="unable_create_device",
                    translation_placeholders={"serial_number": serial_number},
                ) from err

            if device:
                devices.append(device)

                location_stripped = device.location.strip() if device.location else ""
                device_location = location_stripped or UNKNOWN_LOCATION

                device_registry.async_get_or_create(
                    config_entry_id=entry.entry_id,
                    identifiers={(DOMAIN, device.identifier)},
                    name=f"{device.name} (Address {address})",
                    manufacturer=device.manufacturer,
                    model=device.name,
                    serial_number=serial_number,
                    suggested_area=device_location,
                )

            else:
                raise ConfigEntryNotReady(
                    translation_domain=DOMAIN,
                    translation_key="unsupported_device",
                    translation_placeholders={"name": name, "location": serial_number},
                )

        coordinator = PapouchSerialDataUpdateCoordinator(
            hass, serial_client, entry, devices
        )

    else:
        return False

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: PapouchConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        await entry.runtime_data.async_close()

    return unload_ok


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)
