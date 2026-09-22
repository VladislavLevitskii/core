"""Options flow for the Papouch integration."""

import copy
import logging
from typing import Any

from aiopapouch import is_device_supported
from aiopapouch.exceptions import DeviceConnectionError
from aiopapouch.utils import _get_device_details, assign_next_available_address
import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import DEFAULT_SCAN_INTERVAL, SERIAL_BROADCAST_ADDRESS
from .coordinator import PapouchSerialDataUpdateCoordinator

_LOGGER = logging.getLogger()


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

        if connection_type in ("serial", "network_hub"):
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

    async def async_step_add_device_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Menu to choose how to add a serial device."""
        return self.async_show_menu(
            step_id="add_device_menu",
            menu_options=[
                "add_device_by_address",
                "add_device_by_serial_number",
                "add_device_via_broadcast",
            ],
        )

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
                try:
                    device_name, serial_number, _ = await _get_device_details(
                        coordinator.api_client, address
                    )
                except DeviceConnectionError:
                    errors["base"] = "cannot_connect"

                if not errors and not is_device_supported(device_name, "serial"):
                    errors["base"] = "unsupported_device"

            if not errors:
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

            used_addresses: list[int] = [d["address"] for d in self._devices]

            new_address, device_name = await assign_next_available_address(
                coordinator.api_client, used_addresses, serial_number
            )

            if new_address is None:
                errors["base"] = "no_free_addresses"
            elif device_name is None:
                errors["base"] = "assign_failed"
            elif not is_device_supported(device_name, "serial"):
                errors["base"] = "unsupported_device"

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

    async def async_step_add_device_via_broadcast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a new serial device using broadcast."""

        errors: dict[str, str] = {}

        coordinator: PapouchSerialDataUpdateCoordinator = self.config_entry.runtime_data

        try:
            device_name, serial_number, new_address = await _get_device_details(
                coordinator.api_client, SERIAL_BROADCAST_ADDRESS
            )
        except DeviceConnectionError:
            errors["base"] = "cannot_connect"

        for device in self._devices:
            if new_address == device["address"]:
                return self.async_abort(reason="broadcast_already_configured_device")

        if errors:
            return self.async_abort(reason="bus_multiple_devices")

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
