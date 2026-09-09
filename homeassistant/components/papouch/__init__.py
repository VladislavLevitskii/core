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
from pap_spinel import SerialTransport, SpinelClient, SpinelTransportError, TcpTransport

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


async def _async_setup_network_entry(
    hass: HomeAssistant, entry: PapouchConfigEntry
) -> PapouchNetworkDataUpdateCoordinator:
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
            translation_key="cannot_connect_http",
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

    return PapouchNetworkDataUpdateCoordinator(hass, api_client, entry, device)


def _async_cleanup_stale_devices(
    device_registry: dr.DeviceRegistry,
    entry: PapouchConfigEntry,
    port: str,
) -> None:

    devices_config = entry.options.get("devices", [])
    expected_serial_numbers = {dev_conf["serial_number"] for dev_conf in devices_config}

    existing_devices = dr.async_entries_for_config_entry(
        device_registry, entry.entry_id
    )

    for device_entry in existing_devices:
        for domain, device_id in device_entry.identifiers:
            if domain == DOMAIN:
                if device_id != port and device_id not in expected_serial_numbers:
                    device_registry.async_remove_device(device_entry.id)
                break


async def _async_setup_serial_entry(
    hass: HomeAssistant, entry: PapouchConfigEntry
) -> PapouchSerialDataUpdateCoordinator:
    port = entry.data["port"]
    baudrate = entry.data["baudrate"]
    transport = SerialTransport(port, baudrate)

    serial_client = PapouchSerialClient(SpinelClient(transport))

    try:
        await serial_client.open()
    except SpinelTransportError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="unable_open_port",
            translation_placeholders={"port": port},
        ) from err

    devices_config = entry.options.get("devices", [])

    device_registry = dr.async_get(hass)

    _async_cleanup_stale_devices(devices_config, entry, port)

    devices = []

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

    return PapouchSerialDataUpdateCoordinator(hass, serial_client, entry, devices)


async def _async_setup_tcp_entry(
    hass: HomeAssistant, entry: PapouchConfigEntry
) -> PapouchSerialDataUpdateCoordinator:
    host = entry.data["host"]
    port = entry.data["port"]

    serial_client = PapouchSerialClient(SpinelClient(TcpTransport(host, port)))

    try:
        await serial_client.open()
    except SpinelTransportError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect_tcp",
            translation_placeholders={"name": host, "location": port},
        ) from err

    try:
        device = await create_serial_device(serial_client, address=0xFE)
    except DeviceConnectionError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="unable_create_device",
            translation_placeholders={"serial_number": host},
        ) from err

    if not device:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="unsupported_device",
            translation_placeholders={"name": host, "location": ""},
        )

    session = async_get_clientsession(hass)
    network_client = PapouchHTTPClient(host, session)

    try:
        mac_address = await network_client.get_device_mac()
    except DeviceConnectionError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect_http",
            translation_placeholders={
                "name": device.name,
                "location": device.location,
            },
        ) from err

    device.conf.identifier = mac_address

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, mac_address)},
        identifiers={(DOMAIN, mac_address)},
        name=device.name,
        manufacturer=device.manufacturer,
        model=device.name,
        suggested_area=device.location or UNKNOWN_LOCATION,
    )

    return PapouchSerialDataUpdateCoordinator(hass, serial_client, entry, [device])


async def async_setup_entry(hass: HomeAssistant, entry: PapouchConfigEntry) -> bool:
    """Set up Papouch device from a config entry."""
    entry.async_on_unload(entry.add_update_listener(update_listener))

    coordinator: PapouchBaseCoordinator

    connection_type = entry.data.get("connection_type", "network")

    if connection_type == "network":
        coordinator = await _async_setup_network_entry(hass, entry)
    elif connection_type == "serial":
        coordinator = await _async_setup_serial_entry(hass, entry)
    elif connection_type == "tcp":
        coordinator = await _async_setup_tcp_entry(hass, entry)
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
