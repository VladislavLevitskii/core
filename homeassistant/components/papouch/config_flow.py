"""Config flow for the Papouch integration."""

import asyncio
import copy
import ipaddress
import logging
import re
from typing import TYPE_CHECKING, Any, override

import aiohttp
from aiopapouch import PapouchHTTPClient, create_network_device, is_device_supported
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
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

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
from .coordinator import PapouchSerialDataUpdateCoordinator
from .discovery import async_discover_papouch_devices
from .utils import (
    _async_fetch_network_details,
    _get_device_details,
    _get_device_name,
    _get_network_schema,
    _get_next_available_address,
)

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
            self.hass, client, ip_address, password, errors
        )

        if errors:
            return errors, None

        if mode_device is None or title_name is None or mac_address is None:
            # errors shouldn't be empty -> `if errors` should trigger and return
            # mypy fix
            return {}, self.async_abort(reason="unknown")

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
            return {}, self.async_abort(reason="unknown")

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
                    new_name = await _get_device_name(
                        self.hass,
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
            if connection_type == "network":
                return await self.async_step_network()
            if connection_type == "serial":
                return await self.async_step_serial()

        schema = vol.Schema(
            {
                vol.Required("connection_type", default="network"): vol.In(
                    {
                        "network": "Network device",
                        "serial": "Serial hub",
                    }
                )
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_network(
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
            results = await async_discover_papouch_devices(self.hass)

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
            step_id="network", data_schema=schema, errors=errors
        )

    async def async_step_serial(
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

        ports = await self.hass.async_add_executor_job(serial.tools.list_ports.comports)

        configured_ports = {
            entry.data.get("port")
            for entry in self._async_current_entries()
            if entry.data.get("port")
        }

        list_of_ports = {}
        for port_info in ports:
            if port_info.device not in configured_ports:
                description = port_info.description or "Unknown device"
                list_of_ports[port_info.device] = f"{port_info.device} - {description}"

        list_of_ports["manual"] = "Enter port manually"

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

        return self.async_show_form(step_id="serial", data_schema=schema, errors=errors)

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
        return self.async_show_menu(
            step_id="web_mode", menu_options=["execute_switch", "abort_switch"]
        )

    async def async_step_execute_switch(
        self,
        user_input: dict[str, Any],
    ) -> ConfigFlowResult:
        """Make action when user clicks the switch button."""
        if self._saved_input is None:
            return self.async_abort(reason="unsupported_device")

        session = async_get_clientsession(self.hass)
        password = self._saved_input.get("password", "")
        ip_address = self._saved_input["ip_address"]
        web_port = self._saved_input["web_port"]

        client = PapouchHTTPClient(
            ip_address, session, password=password, web_port=web_port
        )

        try:
            device = await create_network_device(client)

            if device is None:
                return self.async_abort(reason="unsupported_device")

            await device.switch_to_web_mode()

            title_name = await _get_device_name(
                self.hass, ip_address, password, web_port
            )

            try:
                mac_address = await client.get_device_mac()
            except aiohttp.ClientError, DeviceLogicError:
                return self.async_abort(reason="cannot_connect")

            if mac_address:
                formatted_mac = format_mac(mac_address)
                await self.async_set_unique_id(formatted_mac)
                self._abort_if_unique_id_configured()

            data = {
                "ip_address": ip_address,
                "password": password,
                "device_name": title_name,
                "web_port": web_port,
            }
            options = {
                "refresh_rate": self._saved_input.get(
                    "refresh_rate", DEFAULT_SCAN_INTERVAL
                )
            }

            return self.async_create_entry(
                title=f"{title_name} - {ip_address}",
                data=data,
                options=options,
                description="web_mode_success",
            )
        except aiohttp.ClientError:
            return self.async_abort(reason="cannot_connect")

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
            return self.async_abort(reason="unknown")

        entry = self.hass.config_entries.async_get_entry(entry_id)

        if entry is None:
            return self.async_abort(reason="unknown")

        connection_type = entry.data.get("connection_type", "network")

        if connection_type == "serial":
            return await self.async_step_reconfigure_serial(user_input)

        if user_input is not None:
            errors, _ = await self._test_connection(
                user_input["ip_address"],
                user_input.get("password", ""),
                user_input.get("web_port", DEFAULT_WEB_PORT),
            )

            if not errors:
                new_name = await _get_device_name(
                    self.hass,
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

    async def async_step_reconfigure_serial(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration for serial hub."""
        errors: dict[str, str] = {}

        entry_id = self.context.get("entry_id")

        if not entry_id:
            return self.async_abort(reason="unknown")

        entry = self.hass.config_entries.async_get_entry(entry_id)

        if entry is None:
            return self.async_abort(reason="unknown")

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

        schema = vol.Schema(
            {
                vol.Required(
                    "port", default=entry.data.get("port", "/dev/ttyUSB0")
                ): str,
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

    @override
    @staticmethod
    @callback
    def async_get_options_flow(config_entry: PapouchConfigEntry) -> OptionsFlow:
        """Create the options flow."""
        return PapouchOptionsFlowHandler()


class PapouchOptionsFlowHandler(OptionsFlow):
    """Handle Papouch options."""

    def __init__(self) -> None:
        """Initialize options flow."""
        self._devices: list[dict[str, Any]] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        connection_type = self.config_entry.data.get("connection_type", "network")

        if connection_type == "serial":
            # deep copy for HA comparison, otherwise options flow will be cancelled
            self._devices = copy.deepcopy(self.config_entry.options.get("devices", []))
            return await self.async_step_serial_menu()

        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_refresh = self.config_entry.options.get(
            "refresh_rate", DEFAULT_SCAN_INTERVAL
        )

        schema = vol.Schema(
            {
                vol.Required("refresh_rate", default=current_refresh): vol.All(
                    int, vol.Range(min=1, max=3600)
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_serial_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Menu for managing the serial hub."""
        menu_options = ["add_device_menu", "hub_settings"]

        if self._devices:
            menu_options.insert(1, "remove_device")

        return self.async_show_menu(
            step_id="serial_menu",
            menu_options=menu_options,
        )

    async def async_step_add_device_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Menu to choose how to add a serial device."""
        return self.async_show_menu(
            step_id="add_device_menu",
            menu_options=["add_device_by_address", "add_device_by_serial_number"],
        )

    async def async_step_hub_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure general hub settings like refresh rate."""
        if user_input is not None:
            new_options = {**self.config_entry.options, **user_input}
            return self.async_create_entry(title="", data=new_options)

        current_refresh = self.config_entry.options.get(
            "refresh_rate", DEFAULT_SCAN_INTERVAL
        )

        schema = vol.Schema(
            {
                vol.Required("refresh_rate", default=current_refresh): vol.All(
                    int, vol.Range(min=1, max=3600)
                ),
            }
        )

        return self.async_show_form(step_id="hub_settings", data_schema=schema)

    async def async_step_add_device_by_address(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a new serial device by specifying its address."""
        errors: dict[str, str] = {}
        serial_number = None
        device_name = None

        coordinator: PapouchSerialDataUpdateCoordinator = self.config_entry.runtime_data

        if user_input is not None:
            address = int(user_input["address"])

            for device in self._devices:
                if device["address"] == address:
                    errors["address"] = "address_already_used"

            if not errors:
                errors, device_name, serial_number = await _get_device_details(
                    coordinator, address
                )

                if not is_device_supported(device_name, "serial"):
                    errors["base"] = "unsupported_device"

            if not errors and serial_number and device_name:
                for device in self._devices:
                    if device["serial_number"] == serial_number:
                        errors["base"] = "serial_already_used"

                if not errors:
                    self._devices.append(
                        {
                            "address": address,
                            "serial_number": serial_number,
                            "name": device_name,
                        }
                    )

                    new_options = {
                        **self.config_entry.options,
                        "devices": self._devices,
                    }
                    return self.async_create_entry(
                        title="",
                        data=new_options,
                        description_placeholders={"device_name": device_name or ""},
                    )

        schema = vol.Schema(
            {
                vol.Required("address", default=1): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=253,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="add_device_by_address",
            data_schema=schema,
            errors=errors,
            description_placeholders={"device_name": device_name or ""},
        )

    async def async_step_add_device_by_serial_number(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a new serial device by specifying its serial number."""
        errors: dict[str, str] = {}
        device_name = None

        if user_input is not None:
            serial_number = user_input["serial_number"]

            if "/" not in serial_number:
                errors["serial_number"] = "invalid_serial_format"

            if not errors:
                for device in self._devices:
                    if device["serial_number"] == serial_number:
                        errors["serial_number"] = "serial_already_used"
                        break

            coordinator: PapouchSerialDataUpdateCoordinator = (
                self.config_entry.runtime_data
            )

            new_address = await _get_next_available_address(coordinator, self._devices)
            if new_address is None:
                errors["base"] = "no_free_addresses"

            if not errors and new_address is not None:
                try:
                    await coordinator.api_client.set_address(
                        new_address,
                        serial_number,
                        f"device with {new_address} for SN {serial_number}",
                    )

                    # some devices restart after settings a new address
                    await asyncio.sleep(2)

                    errors, device_name, _ = await _get_device_details(
                        coordinator, new_address
                    )

                    if not is_device_supported(device_name, "serial"):
                        errors["base"] = "unsupported_device"

                except DeviceConnectionError:
                    errors["base"] = "cannot_connect_broadcast"

            if not errors:
                self._devices.append(
                    {
                        "address": new_address,
                        "serial_number": serial_number,
                        "name": device_name,
                    }
                )

                new_options = {
                    **self.config_entry.options,
                    "devices": self._devices,
                }
                return self.async_create_entry(
                    title="",
                    data=new_options,
                    description_placeholders={"device_name": device_name or ""},
                )

        schema = vol.Schema(
            {
                vol.Required("serial_number"): str,
            }
        )

        return self.async_show_form(
            step_id="add_device_by_serial_number",
            data_schema=schema,
            errors=errors,
            description_placeholders={"device_name": device_name or ""},
        )

    async def async_step_remove_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove a device from the hub."""
        if not self._devices:
            return await self.async_step_serial_menu()

        if user_input is not None:
            device_to_remove = user_input["device"]

            self._devices = [
                d for d in self._devices if d["serial_number"] != device_to_remove
            ]

            new_options = {
                **self.config_entry.options,
                "devices": self._devices,
            }

            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self.config_entry.entry_id)
            )

            return self.async_create_entry(title="", data=new_options)

        options = {
            dev[
                "serial_number"
            ]: f"{dev['name']}, address: {dev['address']}, SN: {dev['serial_number']}"
            for dev in self._devices
        }

        schema = vol.Schema(
            {
                vol.Required("device"): vol.In(options),
            }
        )

        return self.async_show_form(step_id="remove_device", data_schema=schema)
