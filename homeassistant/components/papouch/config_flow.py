"""Config flow for the Papouch integration."""

import asyncio
import ipaddress
import logging
import re
from typing import TYPE_CHECKING, Any, override

import aiohttp
from aiopapouch import (
    PapouchHTTPClient,
    async_discover_papouch_devices,
    create_converter,
    create_network_device,
    is_device_supported,
)
from aiopapouch.exceptions import (
    DeviceAuthError,
    DeviceConnectionError,
    DeviceLogicError,
)
import serial.tools.list_ports
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac

from .const import (
    DEFAULT_BAUDRATE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WEB_PORT,
    DHCP_TIMEOUT,
    DOMAIN,
    TCP_CLIENT_MODE_INDEX,
    TCP_SERVER_MODE_INDEX,
    UDP_MODE_INDEX,
    WEB_MODE_INDEX,
)
from .options_flow import PapouchOptionsFlowHandler
from .utils import _async_fetch_network_details, _get_device_name, _get_network_schema

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

    from . import PapouchConfigEntry

_LOGGER = logging.getLogger(__name__)


class PapouchConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Papouch."""

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._reauth_entry: PapouchConfigEntry | None = None
        self.discovered_ip: str | None = None
        self.discovered_name: str | None = None
        self._saved_input: dict | None = None
        self._discovered_ips: dict[str, str] | None = None
        self._is_network_hub: bool = False
        self._switch_task: asyncio.Task | None = None

    async def _test_connection(
        self, ip_address: str, password: str = "", web_port: int = DEFAULT_WEB_PORT
    ) -> tuple[dict[str, str], int | None]:
        """Test the connection and return any errors and the device mode."""
        if not re.match(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", ip_address):
            return {"ip_address": "invalid_ip_format"}, None

        session = async_get_clientsession(self.hass)
        client = PapouchHTTPClient(
            ip_address, session, password=password, web_port=web_port
        )

        try:
            await client.fetch_info()
            mode_device = await client.get_device_mode()
        except DeviceAuthError:
            return {"base": "invalid_auth"}, None
        except (
            aiohttp.ClientError,
            DeviceConnectionError,
            TimeoutError,
        ):
            _LOGGER.exception("Failed to connect to the device")
            return {"base": "cannot_connect"}, None

        return {}, mode_device

    async def _async_validate_network_hub(
        self, host: str, password: str, web_port: int
    ) -> tuple[dict[str, str], str | None, str | None, int | None, int | None]:
        """Test the connection to network hub and return (errors, title, unique_id, device_mode)."""
        session = async_get_clientsession(self.hass)
        client = PapouchHTTPClient(host, session, password=password, web_port=web_port)

        try:
            converter = await create_converter(client)
            if converter is None:
                return {"base": "unsupported_converter"}, None, None, None, None

            device_mode = await converter.get_mode()
            title = f"{converter.conf.context} - {(client.ip_address)}"

        except aiohttp.ClientError, DeviceConnectionError, TimeoutError:
            return {"base": "cannot_connect"}, None, None, None, None

        return (
            {},
            title,
            converter.conf.identifier,
            device_mode,
            converter.conf.tcp_port,
        )

    async def _async_get_available_serial_ports(self) -> dict[str, str]:
        """Fetch available serial ports excluding already configured ones."""
        ports = await self.hass.async_add_executor_job(serial.tools.list_ports.comports)
        configured_ports = {
            entry.data.get("port")
            for entry in self._async_current_entries()
            if entry.data.get("port")
        }

        list_of_ports = {
            p.device: f"{p.device} - {p.description or 'Unknown device'}"
            for p in ports
            if p.device not in configured_ports
        }
        list_of_ports["manual"] = "Enter port manually"
        return list_of_ports

    async def _async_process_user_input(
        self, user_input: dict[str, Any]
    ) -> tuple[dict[str, str], ConfigFlowResult | None]:
        """Process user input, test connection, and determine the next routing step."""
        for entry in self._async_current_entries():
            if entry.data.get("ip_address") == user_input["ip_address"]:
                return {}, self.async_abort(reason="already_configured")

        ip_address = user_input["ip_address"]
        password = str(user_input.get("password", ""))
        web_port = int(user_input["web_port"])

        errors, mode_device = await self._test_connection(
            user_input["ip_address"], password, web_port
        )

        if errors:
            return errors, None

        self._saved_input = user_input

        session = async_get_clientsession(self.hass)

        client = PapouchHTTPClient(
            ip_address, session, password=password, web_port=web_port
        )

        errors, title_name, mac_address = await _async_fetch_network_details(
            session, client, ip_address, password, errors
        )

        if errors:
            return errors, None

        if mode_device is None or title_name is None or mac_address is None:
            # errors shouldn't be empty -> `if errors` should trigger and return
            # mypy fix
            return {}, self.async_abort(reason="unreachable")

        if mode_device == TCP_SERVER_MODE_INDEX:
            tcp_port = await client.get_device_tcp_port()
            await self.async_set_unique_id(mac_address)
            self._abort_if_unique_id_configured()

            data = {
                "connection_type": "tcp",
                "host": user_input["ip_address"],
                "port": tcp_port,
            }
            options = {
                "refresh_rate": user_input.get("refresh_rate", DEFAULT_SCAN_INTERVAL)
            }
            return {}, self.async_create_entry(
                title=f"{title_name} - {user_input['ip_address']}",
                data=data,
                options=options,
            )

        if mode_device in (TCP_CLIENT_MODE_INDEX, UDP_MODE_INDEX):
            return {}, await self.async_step_web_mode()

        if mode_device == WEB_MODE_INDEX:
            pass
        else:
            errors["base"] = "device_empty_mode"
            return errors, None

        await self.async_set_unique_id(mac_address)
        self._abort_if_unique_id_configured()

        data = {
            "connection_type": "network",
            "ip_address": user_input["ip_address"],
            "password": password,
            "device_name": title_name,
            "web_port": web_port,
        }
        options = {
            "refresh_rate": user_input.get("refresh_rate", DEFAULT_SCAN_INTERVAL)
        }

        return {}, self.async_create_entry(
            title=f"{title_name} - {user_input['ip_address']}",
            data=data,
            options=options,
        )

    @override
    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Discover the device from a DHCP request."""
        self.discovered_ip = discovery_info.ip
        discovered_mac = format_mac(discovery_info.macaddress)

        await self.async_set_unique_id(discovered_mac)

        for entry in self._async_current_entries():
            if entry.unique_id == discovered_mac:
                if entry.data.get("ip_address") != self.discovered_ip:
                    session = async_get_clientsession(self.hass)

                    new_name = await _get_device_name(
                        session,
                        self.discovered_ip,
                        entry.data.get("password", ""),
                        entry.data.get("web_port", DEFAULT_WEB_PORT),
                    )
                    new_title = f"{new_name} - {self.discovered_ip}"

                    updated_data = {
                        **entry.data,
                        "ip_address": self.discovered_ip,
                    }

                    self.hass.config_entries.async_update_entry(
                        entry, data=updated_data, title=new_title
                    )

                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(entry.entry_id)
                    )

                return self.async_abort(reason="already_configured")

            if (
                entry.unique_id is None
                and entry.data.get("ip_address") == self.discovered_ip
            ):
                self.hass.config_entries.async_update_entry(
                    entry, unique_id=discovered_mac
                )
                return self.async_abort(reason="already_configured")

        session = async_get_clientsession(self.hass)
        client = PapouchHTTPClient(self.discovered_ip, session)

        try:
            await asyncio.sleep(DHCP_TIMEOUT)
            device_name, device_location = await client.get_device_info()
        except aiohttp.ClientError:
            _LOGGER.exception("Failed to fetch device info after DHCP")
            return self.async_abort(reason="cannot_connect")

        if not is_device_supported(device_name, "network"):
            return self.async_abort(reason="unsupported_device")

        title_name = f"{device_name} ({device_location})"
        self.discovered_name = f"{title_name} - {self.discovered_ip}"

        self.context.update({"title_placeholders": {"name": self.discovered_name}})

        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step after adding the device via DHCP."""
        errors: dict[str, str] = {}

        if self.discovered_name is None:
            return self.async_abort(reason="unsupported_device")

        if user_input is not None:
            user_input["ip_address"] = self.discovered_ip
            errors, result = await self._async_process_user_input(user_input)
            if result:
                return result

        schema = vol.Schema(
            {
                vol.Required("refresh_rate", default=DEFAULT_SCAN_INTERVAL): vol.All(
                    int, vol.Range(min=1, max=3600)
                ),
                vol.Optional("web_port", default=DEFAULT_WEB_PORT): vol.All(
                    int, vol.Range(min=1, max=65536)
                ),
                vol.Optional("password"): str,
            }
        )

        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={"name": self.discovered_name},
        )

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step to choose between network or serial hub."""
        if user_input is not None:
            connection_type = user_input.get("connection_type")
            if connection_type == "network_device":
                return await self.async_step_network_device()
            if connection_type == "network_hub":
                return await self.async_step_network_hub()
            if connection_type == "serial_hub":
                return await self.async_step_serial_hub()

        schema = vol.Schema(
            {
                vol.Required("connection_type", default="network_device"): vol.In(
                    {
                        "network_device": "Network device",
                        "network_hub": "Network hub",
                        "serial_hub": "Serial hub",
                    }
                )
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_network_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the network device setup featuring active UDP discovery."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input["ip_address"] == "manual":
                self._saved_input = user_input
                return await self.async_step_manual()

            errors, result = await self._async_process_user_input(user_input)
            if result:
                return result

        if self._discovered_ips is None:
            session = async_get_clientsession(self.hass)
            results = await async_discover_papouch_devices(session, "network")

            configured_ips = {
                entry.data.get("ip_address")
                for entry in self._async_current_entries()
                if entry.data.get("ip_address")
            }

            filtered_results = {
                ip: data for ip, data in results.items() if ip not in configured_ips
            }

            sorted_ips = sorted(filtered_results.keys(), key=ipaddress.ip_address)
            self._discovered_ips = {}

            for ip in sorted_ips:
                location, name = filtered_results[ip]
                self._discovered_ips[ip] = f"{ip} - {name} ({location})"

        if not self._discovered_ips and not self.discovered_ip and not errors:
            return await self.async_step_manual()

        options = self._discovered_ips.copy()

        if self.discovered_ip and self.discovered_ip not in options:
            options[self.discovered_ip] = f"Unknown device - {self.discovered_ip}"

        options["manual"] = "Enter IP manually"

        default_interval = (
            user_input.get("refresh_rate", DEFAULT_SCAN_INTERVAL)
            if user_input
            else DEFAULT_SCAN_INTERVAL
        )

        default_web_port = DEFAULT_WEB_PORT

        if user_input and "web_port" in user_input:
            default_web_port = user_input["web_port"]

        schema = _get_network_schema(
            default_refresh=default_interval,
            default_web_port=default_web_port,
            discovered_ips_options=options,
        )

        return self.async_show_form(
            step_id="network_device", data_schema=schema, errors=errors
        )

    async def async_step_serial_hub(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the serial hub configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            port = user_input["port"]

            if port == "manual":
                self._saved_input = {
                    "baudrate": user_input["baudrate"],
                    "refresh_rate": user_input["refresh_rate"],
                }
                return await self.async_step_serial_manual()

            baudrate = user_input["baudrate"]

            await self.async_set_unique_id(port)
            self._abort_if_unique_id_configured()

            data = {
                "connection_type": "serial",
                "port": port,
                "baudrate": baudrate,
            }
            options = {
                "refresh_rate": user_input.get("refresh_rate", DEFAULT_SCAN_INTERVAL)
            }

            return self.async_create_entry(
                title=f"Papouch - {port}",
                data=data,
                options=options,
            )

        list_of_ports = await self._async_get_available_serial_ports()

        if len(list_of_ports) == 1:
            return await self.async_step_serial_manual()

        schema = vol.Schema(
            {
                vol.Required("port"): vol.In(list_of_ports),
                vol.Required("baudrate", default=DEFAULT_BAUDRATE): int,
                vol.Required("refresh_rate", default=DEFAULT_SCAN_INTERVAL): vol.All(
                    int, vol.Range(min=1, max=3600)
                ),
            }
        )

        return self.async_show_form(
            step_id="serial_hub", data_schema=schema, errors=errors
        )

    async def async_step_network_hub(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the network hub configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input["host"]
            if host == "manual":
                return await self.async_step_network_hub_manual()
            baudrate = user_input["baudrate"]
            web_port = user_input["web_port"]
            password = user_input.get("password", "")

            (
                errors,
                title,
                unique_id,
                device_mode,
                tcp_port,
            ) = await self._async_validate_network_hub(host, password, web_port)

            if not errors:
                if device_mode != TCP_SERVER_MODE_INDEX:
                    self._saved_input = user_input
                    self._is_network_hub = True
                    return await self.async_step_web_mode()

                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                data = {
                    "connection_type": "network_hub",
                    "host": host,
                    "baudrate": baudrate,
                    "web_port": web_port,
                    "password": password,
                    "tcp_port": tcp_port,
                }
                options = {
                    "refresh_rate": user_input.get(
                        "refresh_rate", DEFAULT_SCAN_INTERVAL
                    )
                }

                return self.async_create_entry(
                    title=f"{title}", data=data, options=options
                )

        if self._discovered_ips is None:
            session = async_get_clientsession(self.hass)
            results = await async_discover_papouch_devices(
                session, connection_type="network_hub"
            )

            configured_hosts = {
                entry.data.get("host")
                for entry in self._async_current_entries()
                if entry.data.get("host")
            }
            filtered_results = {
                ip: data for ip, data in results.items() if ip not in configured_hosts
            }
            self._discovered_ips = {
                ip: f"{ip} - {name} ({location})"
                for ip, (location, name) in filtered_results.items()
            }

        options_dict = self._discovered_ips.copy()
        options_dict["manual"] = "Enter IP manually"

        if len(options_dict) == 1:
            return await self.async_step_network_hub_manual()

        default_host = list(options_dict.keys())[0] if options_dict else "manual"

        schema = vol.Schema(
            {
                vol.Required("host", default=default_host): vol.In(options_dict),
                vol.Required("baudrate", default=DEFAULT_BAUDRATE): int,
                vol.Required("refresh_rate", default=DEFAULT_SCAN_INTERVAL): vol.All(
                    int, vol.Range(min=1, max=3600)
                ),
                vol.Required("web_port", default=DEFAULT_WEB_PORT): int,
                vol.Optional("password"): str,
            }
        )

        return self.async_show_form(
            step_id="network_hub", data_schema=schema, errors=errors
        )

    async def async_step_network_hub_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual IP entry for network hub."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input["host"]
            web_port = user_input["web_port"]
            password = user_input.get("password", "")

            (
                errors,
                title,
                unique_id,
                device_mode,
                tcp_port,
            ) = await self._async_validate_network_hub(host, password, web_port)

            if not errors:
                if device_mode != TCP_SERVER_MODE_INDEX:
                    self._saved_input = user_input
                    self._is_network_hub = True
                    return await self.async_step_web_mode()

                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                data = {
                    "connection_type": "network_hub",
                    "host": host,
                    "baudrate": user_input["baudrate"],
                    "web_port": web_port,
                    "password": password,
                    "tcp_port": tcp_port,
                }
                options = {"refresh_rate": user_input["refresh_rate"]}

                return self.async_create_entry(
                    title=f"{title} - {host}", data=data, options=options
                )

        schema = vol.Schema(
            {
                vol.Required("host"): str,
                vol.Required("baudrate", default=DEFAULT_BAUDRATE): int,
                vol.Required("refresh_rate", default=DEFAULT_SCAN_INTERVAL): vol.All(
                    int, vol.Range(min=1, max=3600)
                ),
                vol.Required("web_port", default=DEFAULT_WEB_PORT): int,
                vol.Optional("password"): str,
            }
        )

        return self.async_show_form(
            step_id="network_hub_manual", data_schema=schema, errors=errors
        )

    async def async_step_serial_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual serial port."""
        errors: dict[str, str] = {}

        if user_input is not None:
            port = user_input["port"]
            baudrate = user_input["baudrate"]

            await self.async_set_unique_id(port)
            self._abort_if_unique_id_configured()

            data = {
                "connection_type": "serial",
                "port": port,
                "baudrate": baudrate,
            }
            options = {
                "refresh_rate": user_input.get("refresh_rate", DEFAULT_SCAN_INTERVAL)
            }

            return self.async_create_entry(
                title=f"Papouch - {port}",
                data=data,
                options=options,
            )

        default_baudrate = DEFAULT_BAUDRATE
        default_refresh = DEFAULT_SCAN_INTERVAL

        if self._saved_input:
            default_baudrate = self._saved_input.get("baudrate", DEFAULT_BAUDRATE)
            default_refresh = self._saved_input.get(
                "refresh_rate", DEFAULT_SCAN_INTERVAL
            )

        schema = vol.Schema(
            {
                vol.Required("port", default="/dev/ttyUSB0"): str,
                vol.Required("baudrate", default=default_baudrate): int,
                vol.Required("refresh_rate", default=default_refresh): vol.All(
                    int, vol.Range(min=1, max=3600)
                ),
            }
        )

        return self.async_show_form(
            step_id="serial_manual", data_schema=schema, errors=errors
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual IP entry when discovery fails or is bypassed."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors, result = await self._async_process_user_input(user_input)
            if result:
                return result

        default_ip = self.discovered_ip or ""
        default_interval = DEFAULT_SCAN_INTERVAL
        default_web_port = DEFAULT_WEB_PORT

        if self._saved_input and "refresh_rate" in self._saved_input:
            default_interval = self._saved_input["refresh_rate"]
        if user_input and "refresh_rate" in user_input:
            default_interval = user_input["refresh_rate"]
        if user_input and "ip_address" in user_input:
            default_ip = user_input["ip_address"]
        if user_input and "web_port" in user_input:
            default_web_port = user_input["web_port"]

        schema = _get_network_schema(default_ip, default_interval, default_web_port)

        return self.async_show_form(step_id="manual", data_schema=schema, errors=errors)

    async def async_step_web_mode(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Step where the user can switch the device into WEB mode via buttons."""

        mode_name = "TCP server" if self._is_network_hub else "WEB"

        return self.async_show_menu(
            step_id="web_mode",
            menu_options=["execute_switch", "abort_switch"],
            description_placeholders={"mode": mode_name},
        )

    async def async_step_execute_switch(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Make action when user clicks the switch button with progress bar."""
        if self._saved_input is None:
            return self.async_abort(reason="unsupported_device")

        if not hasattr(self, "_switch_task") or self._switch_task is None:
            self._switch_task = self.hass.async_create_task(
                self._async_perform_switch()
            )

        if not self._switch_task.done():
            return self.async_show_progress(
                step_id="execute_switch",
                progress_action="restarting_device",
                progress_task=self._switch_task,
            )

        return self.async_show_progress_done(next_step_id="finish_switch")

    async def async_step_finish_switch(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the result after the switch task finishes."""

        if self._switch_task is None or self._saved_input is None:
            # unreachable
            return self.async_abort(reason="unreachable")

        try:
            title_name, data, unique_id = self._switch_task.result()
        except (
            aiohttp.ClientError,
            DeviceConnectionError,
            TimeoutError,
        ) as err:
            _LOGGER.error("Connection error during switch: %s", err)
            return self.async_abort(reason="cannot_connect")
        except DeviceLogicError as err:
            _LOGGER.error("Logic error during switch: %s", err)
            return self.async_abort(reason="invalid_response")
        finally:
            self._switch_task = None

        await self.async_set_unique_id(unique_id, raise_on_progress=False)
        self._abort_if_unique_id_configured()

        options = {
            "refresh_rate": self._saved_input.get("refresh_rate", DEFAULT_SCAN_INTERVAL)
        }

        return self.async_create_entry(
            title=title_name,
            data=data,
            options=options,
            description="web_mode_success",
            description_placeholders={
                "mode": "TCP server" if self._is_network_hub else "WEB"
            },
        )

    async def _async_perform_switch(self) -> tuple[str, dict[str, Any], str]:
        """Async task on background, switch to proper mode and return proper data."""
        session = async_get_clientsession(self.hass)

        if self._saved_input is None:
            raise DeviceLogicError("Unreachable")

        password = self._saved_input.get("password", "")
        address = (
            self._saved_input["host"]
            if self._is_network_hub
            else self._saved_input["ip_address"]
        )
        web_port = self._saved_input["web_port"]

        client = PapouchHTTPClient(
            address, session, password=password, web_port=web_port
        )

        if self._is_network_hub:
            converter = await create_converter(client)
            if converter is None:
                raise DeviceConnectionError("Unsupported device")

            await converter.switch_to_tcp_server()

            (
                _,
                title_name,
                unique_id,
                _,
                tcp_port,
            ) = await self._async_validate_network_hub(address, password, web_port)
            if not unique_id or not title_name:
                raise DeviceConnectionError("Cannot validate network hub")

            data = {
                "connection_type": "network_hub",
                "host": address,
                "baudrate": self._saved_input["baudrate"],
                "web_port": web_port,
                "password": password,
                "tcp_port": tcp_port,
            }
        else:
            device = await create_network_device(client)
            if device is None:
                raise DeviceConnectionError("Unsupported device")

            await device.switch_to_web_mode()

            title_name = await _get_device_name(session, address, password, web_port)

            try:
                mac_address = await client.get_device_mac()
            except aiohttp.ClientError as err:
                raise DeviceConnectionError(err) from err

            formatted_mac = format_mac(mac_address)
            unique_id = formatted_mac

            data = {
                "ip_address": address,
                "password": password,
                "device_name": device.conf.context,
                "web_port": web_port,
            }

        return f"{title_name} - {address}", data, unique_id

    async def async_step_abort_switch(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Make action when user clicks cancel."""
        return self.async_abort(reason="web_mode_required")

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],
    ) -> ConfigFlowResult:
        """Handle initiation of re-authentication."""
        self._reauth_entry = self._get_reauth_entry()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dialog that informs the user that reauth is required."""
        errors: dict[str, str] = {}

        if user_input is not None and self._reauth_entry:
            password = user_input.get("password", "")
            ip_address = self._reauth_entry.data["ip_address"]
            web_port = self._reauth_entry.data.get("web_port", DEFAULT_WEB_PORT)

            errors, _ = await self._test_connection(ip_address, password, web_port)

            if not errors:
                new_data = {
                    **self._reauth_entry.data,
                    "password": password,
                    "web_port": web_port,
                }
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry, data=new_data
                )

                await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)

                return self.async_abort(reason="reauth_successful")

        device_name = self._reauth_entry.title if self._reauth_entry else "Papouch"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional("password"): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "ip_address": self._reauth_entry.data["ip_address"]
                if self._reauth_entry
                else "",
                "name": device_name,
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle integration reconfiguration (e.g. IP address and password change)."""
        errors: dict[str, str] = {}

        entry_id = self.context.get("entry_id")
        if not entry_id:
            return self.async_abort(reason="unreachable")

        entry = self.hass.config_entries.async_get_entry(entry_id)

        if entry is None:
            return self.async_abort(reason="unreachable")

        connection_type = entry.data.get("connection_type", "network")

        if connection_type == "serial":
            return await self.async_step_reconfigure_serial(user_input)

        if connection_type == "network_hub":
            return await self.async_step_reconfigure_network_hub(user_input)

        if user_input is not None:
            errors, _ = await self._test_connection(
                user_input["ip_address"],
                user_input.get("password", ""),
                user_input.get("web_port", DEFAULT_WEB_PORT),
            )

            if not errors:
                session = async_get_clientsession(self.hass)

                new_name = await _get_device_name(
                    session,
                    user_input["ip_address"],
                    user_input.get("password", ""),
                    user_input.get("web_port", DEFAULT_WEB_PORT),
                )
                new_title = f"{new_name} - {user_input['ip_address']}"

                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        "ip_address": user_input["ip_address"],
                        "password": user_input.get("password", ""),
                        "web_port": user_input.get("web_port", DEFAULT_WEB_PORT),
                    },
                    title=new_title,
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

        default_web_port = entry.data.get("web_port", DEFAULT_WEB_PORT)
        if user_input and "web_port" in user_input:
            default_web_port = user_input["web_port"]

        schema = vol.Schema(
            {
                vol.Required("ip_address", default=entry.data["ip_address"]): str,
                vol.Optional("password", default=entry.data.get("password", "")): str,
                vol.Optional("web_port", default=default_web_port): vol.All(
                    int, vol.Range(min=1, max=65536)
                ),
            }
        )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "name": entry.title,
            },
        )

    async def async_step_reconfigure_network_hub(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration for network hub."""
        errors: dict[str, str] = {}

        entry_id = self.context.get("entry_id")
        if not entry_id:
            return self.async_abort(reason="unreachable")

        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return self.async_abort(reason="unreachable")

        if user_input is not None:
            host = user_input["host"]
            baudrate = user_input["baudrate"]
            web_port = user_input["web_port"]
            password = user_input.get("password", "")

            (
                errors,
                title,
                _,
                device_mode,
                tcp_port,
            ) = await self._async_validate_network_hub(host, password, web_port)

            if not errors:
                if device_mode != TCP_SERVER_MODE_INDEX:
                    self._saved_input = user_input
                    self._is_network_hub = True
                    return await self.async_step_web_mode()

                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        "host": host,
                        "baudrate": baudrate,
                        "web_port": web_port,
                        "password": password,
                        "tcp_port": tcp_port,
                    },
                    title=f"{title} - {host}",
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

        schema = vol.Schema(
            {
                vol.Required("host", default=entry.data.get("host")): str,
                vol.Required(
                    "baudrate", default=entry.data.get("baudrate", DEFAULT_BAUDRATE)
                ): int,
                vol.Required(
                    "web_port", default=entry.data.get("web_port", DEFAULT_WEB_PORT)
                ): int,
                vol.Optional("password", default=entry.data.get("password", "")): str,
            }
        )

        return self.async_show_form(
            step_id="reconfigure_network_hub",
            data_schema=schema,
            errors=errors,
            description_placeholders={"name": entry.title},
        )

    async def async_step_reconfigure_serial(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration for serial hub."""
        errors: dict[str, str] = {}

        entry_id = self.context.get("entry_id")

        if not entry_id:
            return self.async_abort(reason="unreachable")

        entry = self.hass.config_entries.async_get_entry(entry_id)

        if entry is None:
            return self.async_abort(reason="unreachable")

        if user_input is not None:
            port = user_input["port"]

            if port == "manual":
                self._saved_input = {
                    "baudrate": user_input["baudrate"],
                }
                return await self.async_step_reconfigure_serial_manual()

            baudrate = user_input["baudrate"]

            self.hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    "port": port,
                    "baudrate": baudrate,
                },
                title=f"Papouch - {port}",
            )
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="reconfigure_successful")

        list_of_ports = await self._async_get_available_serial_ports()

        current_port = entry.data.get("port", "/dev/ttyUSB0")
        if current_port not in list_of_ports:
            list_of_ports[current_port] = f"{current_port} - Current port"

        schema = vol.Schema(
            {
                vol.Required("port", default=current_port): vol.In(list_of_ports),
                vol.Required(
                    "baudrate", default=entry.data.get("baudrate", DEFAULT_BAUDRATE)
                ): int,
            }
        )

        return self.async_show_form(
            step_id="reconfigure_serial",
            data_schema=schema,
            errors=errors,
            description_placeholders={"name": entry.title},
        )

    async def async_step_reconfigure_serial_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual serial port for reconfiguration."""
        errors: dict[str, str] = {}

        entry_id = self.context.get("entry_id")
        if not entry_id:
            return self.async_abort(reason="unreachable")

        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return self.async_abort(reason="unreachable")

        if user_input is not None:
            port = user_input["port"]
            baudrate = user_input["baudrate"]

            self.hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    "port": port,
                    "baudrate": baudrate,
                },
                title=f"Papouch - {port}",
            )
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="reconfigure_successful")

        default_baudrate = entry.data.get("baudrate", DEFAULT_BAUDRATE)
        if self._saved_input:
            default_baudrate = self._saved_input.get("baudrate", default_baudrate)

        schema = vol.Schema(
            {
                vol.Required(
                    "port", default=entry.data.get("port", "/dev/ttyUSB0")
                ): str,
                vol.Required("baudrate", default=default_baudrate): int,
            }
        )

        return self.async_show_form(
            step_id="reconfigure_serial_manual",
            data_schema=schema,
            errors=errors,
            description_placeholders={"name": entry.title},
        )

    @override
    @staticmethod
    @callback
    def async_get_options_flow(config_entry: PapouchConfigEntry) -> OptionsFlow:
        """Create the options flow."""
        return PapouchOptionsFlowHandler()
