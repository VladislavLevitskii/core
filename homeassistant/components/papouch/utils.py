"""File contains helper functions that are used in various places."""

from typing import Any

import aiohttp
from aiopapouch import PapouchHTTPClient
from aiopapouch.exceptions import DeviceAuthError, DeviceLogicError
import voluptuous as vol

from homeassistant.helpers.device_registry import format_mac

from .const import DEFAULT_SCAN_INTERVAL, DEFAULT_WEB_PORT


async def _get_device_name(
    session: aiohttp.ClientSession,
    ip_address: str,
    password: str = "",
    web_port: int = DEFAULT_WEB_PORT,
) -> str:
    """Fetch the real device name and location directly from the device. Doesn't raise."""

    client = PapouchHTTPClient(
        ip_address, session, password=password, web_port=web_port
    )
    try:
        name, location = await client.get_device_info()
        if name and location:
            return f"{name} ({location})"
        if name and not location:
            return f"{name} (NONAME)"
        if not name and location:
            return f"Papouch device ({location})"
    except aiohttp.ClientError:
        pass

    return "Papouch Device - (NONAME)"


async def _async_fetch_network_details(
    session: aiohttp.ClientSession,
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

    title_name = await _get_device_name(session, ip_address, password)

    return errors, title_name, formatted_mac


def _get_network_schema(
    default_ip: str = "",
    default_refresh: int = DEFAULT_SCAN_INTERVAL,
    default_web_port: int = DEFAULT_WEB_PORT,
    discovered_ips_options: dict[str, str] | None = None,
    key_name: str = "ip_address",
) -> vol.Schema:

    ip_selector: Any = vol.In(discovered_ips_options) if discovered_ips_options else str
    ip_key: Any = (
        vol.Required(key_name, default=default_ip)
        if default_ip
        else vol.Required(key_name)
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
