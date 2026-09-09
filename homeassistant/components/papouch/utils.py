"""File contains helper functions that are used in various places."""

from typing import TYPE_CHECKING, Any

import aiohttp
from aiopapouch import PapouchHTTPClient, parse_device_name, parse_device_serial_number
from aiopapouch.exceptions import (
    DeviceAuthError,
    DeviceConnectionError,
    DeviceLogicError,
)
from pap_spinel import INST_INFO
import voluptuous as vol

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac

from .const import DEFAULT_SCAN_INTERVAL, DEFAULT_WEB_PORT
from .coordinator import PapouchSerialDataUpdateCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def _get_device_name(
    hass: HomeAssistant,
    ip_address: str,
    password: str = "",
    web_port: int = DEFAULT_WEB_PORT,
) -> str:
    """Fetch the real device name and location directly from the device. Doesn't raise."""
    session = async_get_clientsession(hass)
    client = PapouchHTTPClient(
        ip_address, session, password=password, web_port=web_port
    )
    try:
        name, location = await client.get_device_info()
        if name and location:
            return f"{name} ({location})"
    except aiohttp.ClientError:
        pass

    return "Papouch Device"


async def _get_device_details(
    coordinator: PapouchSerialDataUpdateCoordinator, address: int
) -> tuple[dict[str, str], str | None, str | None]:
    """Test device connection and return errors, name, and serial number. Doesn't raise."""
    try:
        pkt_man_data = await coordinator.api_client.get_man_data(
            address, f"Unknown device with {address} address"
        )

        serial_number = parse_device_serial_number(pkt_man_data.data)

        pkt_info = await coordinator.api_client.get_info(
            address, f"Device at address {address}"
        )
        device_name = parse_device_name(pkt_info.data)

    except DeviceConnectionError:
        return {"base": "cannot_connect"}, None, None

    return {}, device_name, serial_number


async def _get_next_available_address(
    coordinator: PapouchSerialDataUpdateCoordinator, devices: list[dict[str, Any]]
) -> int | None:
    """Find the next available address from 0 to 253. Doesn't raise."""

    used_addresses = {device["address"] for device in devices}

    for addr in range(250, -1, -1):
        if addr in used_addresses:
            continue

        try:
            # we don't want any other device to have a new address
            await coordinator.api_client.write_command(
                addr, INST_INFO, context="", timeout=0.3
            )
        except DeviceConnectionError:
            return addr

    return None


async def _async_fetch_network_details(
    hass: HomeAssistant,
    client: PapouchHTTPClient,
    ip_address: str,
    password: str,
    errors: dict,
) -> tuple[dict, str | None, str | None]:
    try:
        mac_address = await client.get_device_mac()
    except DeviceAuthError:
        errors["base"] = "invalid_auth"
    except aiohttp.ClientError, DeviceLogicError:
        errors["base"] = "cannot_connect"

    if errors:
        return errors, None, None

    formatted_mac = format_mac(mac_address)

    title_name = await _get_device_name(hass, ip_address, password)

    return errors, title_name, formatted_mac


def _get_network_schema(
    default_ip: str = "",
    default_refresh: int = DEFAULT_SCAN_INTERVAL,
    default_web_port: int = DEFAULT_WEB_PORT,
    discovered_ips_options: dict[str, str] | None = None,
) -> vol.Schema:

    ip_selector: Any = vol.In(discovered_ips_options) if discovered_ips_options else str
    ip_key: Any = (
        vol.Required("ip_address", default=default_ip)
        if default_ip
        else vol.Required("ip_address")
    )

    return vol.Schema(
        {
            ip_key: ip_selector,
            vol.Required("refresh_rate", default=default_refresh): vol.All(
                int, vol.Range(min=1, max=3600)
            ),
            vol.Optional("web_port", default=default_web_port): vol.All(
                int, vol.Range(min=1, max=65536)
            ),
            vol.Optional("password"): str,
        }
    )
