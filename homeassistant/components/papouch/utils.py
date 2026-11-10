"""File contains helper functions that are used in various places."""

import logging
from typing import Any

import aiohttp
from aiopapouch import PapouchHTTPClient
from aiopapouch.exceptions import (
    DeviceAuthError,
    DeviceConnectionError,
    DeviceParseError,
)
import probatio as prob

from homeassistant.helpers.device_registry import format_mac

from .const import DEFAULT_SCAN_INTERVAL, DEFAULT_WEB_PORT

_LOGGER = logging.getLogger(__name__)


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
            _LOGGER.debug("_get_device_name wasn't able to fetch a device's location")
            return f"{name} (NONAME)"
        if not name and location:
            _LOGGER.debug("_get_device_name wasn't able to fetch a device's name")
            return f"Papouch device ({location})"
    except (DeviceConnectionError, DeviceAuthError, DeviceParseError) as err:
        _LOGGER.debug("_get_device_name wasn't able to fetch proper data, err: %s", err)

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
    except DeviceConnectionError:
        errors["base"] = "cannot_connect"
    except DeviceParseError:
        errors["base"] = "parse_error"

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
) -> prob.Schema:

    ip_selector: Any = (
        prob.In(discovered_ips_options) if discovered_ips_options else str
    )
    ip_key: Any = (
        prob.Required(key_name, default=default_ip)
        if default_ip
        else prob.Required(key_name)
    )

    return prob.Schema(
        {
            ip_key: ip_selector,
            prob.Required("refresh_rate", default=default_refresh): prob.All(
                int, prob.Range(min=1, max=3600)
            ),
            prob.Optional("web_port", default=default_web_port): prob.All(
                int, prob.Range(min=1, max=65535)
            ),
            prob.Optional("password"): str,
        }
    )
