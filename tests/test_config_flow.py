"""Config flow tests using real voluptuous and stubbed HA framework boundaries.

Install requirements-test.txt first. Selector stubs mirror the validation used
by Home Assistant 2026.8.1; these tests do not run a complete HA flow manager.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import voluptuous as vol

# Reuse the same module objects so discovery order cannot replace polling stubs.
import test_polling as boundary


class AbortFlow(Exception):
    pass


class FlowBase:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    async def async_set_unique_id(self, value, **kwargs):
        self.unique_id = value

    def _abort_if_unique_id_configured(self):
        if self.unique_id in self.configured_ids:
            raise AbortFlow("already_configured")

    def _async_current_ids(self, **kwargs):
        return set(self.configured_ids)

    def _set_confirm_only(self):
        self.confirm_only = True

    def async_abort(self, **kwargs):
        return {"type": "abort", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_show_progress(self, **kwargs):
        return {"type": "progress", **kwargs}

    def async_show_progress_done(self, **kwargs):
        return {"type": "progress_done", **kwargs}


class NumberSelector:
    def __init__(self, config):
        self.config = config

    def __call__(self, value):
        return vol.All(
            vol.Coerce(float), vol.Range(min=self.config["min"], max=self.config["max"])
        )(value)


class SelectSelector:
    def __init__(self, config):
        self.config = config

    def __call__(self, value):
        options = [option["value"] for option in self.config["options"]]
        if self.config.get("custom_value"):
            return vol.Any(vol.In(options), str)(value)
        return vol.In(options)(value)


config_entries = sys.modules["homeassistant.config_entries"]
config_entries.ConfigFlow = FlowBase
config_entries.OptionsFlow = FlowBase
config_entries.ConfigFlowResult = dict
boundary.BLUETOOTH.BluetoothServiceInfoBleak = types.SimpleNamespace
boundary.BLUETOOTH.async_discovered_service_info = Mock(return_value=[])
boundary.module(
    "homeassistant.helpers.selector",
    NumberSelector=NumberSelector,
    NumberSelectorConfig=dict,
    NumberSelectorMode=types.SimpleNamespace(BOX="box"),
    SelectSelector=SelectSelector,
    SelectSelectorConfig=dict,
    SelectSelectorMode=types.SimpleNamespace(DROPDOWN="dropdown"),
)
flow_module = importlib.import_module("birdie_under_test.config_flow")
constants = boundary.constants
ADDRESS_A = "02:00:00:00:00:01"
ADDRESS_B = "02:00:00:00:00:02"
ADDRESS_C = "02:00:00:00:00:03"
ADDRESS_HEX = "02:00:00:00:00:AB"


def discovery(address=ADDRESS_A, name="Birdie Pro", service_uuids=()):
    return types.SimpleNamespace(address=address, name=name, service_uuids=service_uuids)


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cached = patch.object(flow_module, "async_discovered_service_info", return_value=[])
        self.cached = cached.start()
        self.addCleanup(cached.stop)

    def make_flow(self, configured_ids=None):
        flow = flow_module.BirdieConfigFlow()
        flow.context = {}
        flow.configured_ids = configured_ids if configured_ids is not None else set()
        flow.hass = types.SimpleNamespace(
            loop=asyncio.get_running_loop(), async_create_task=asyncio.create_task
        )
        return flow

    async def test_manual_address_rejects_blank_and_malformed_input_before_unique_id(self):
        for address in ("", "   ", "not an address", "02:00:00:00:00", "GG:00:00:00:00:01"):
            with self.subTest(address=address):
                flow = self.make_flow()
                with patch.object(flow, "async_set_unique_id", new_callable=AsyncMock) as unique:
                    result = await flow.async_step_user({"address": address})
                self.assertEqual(result["type"], "form")
                self.assertEqual(result["errors"], {"address": "invalid_address"})
                unique.assert_not_awaited()

    async def test_manual_address_normalizes_and_keeps_explicit_friendly_name(self):
        flow = self.make_flow()
        result = await flow.async_step_user(
            {"address": f" {ADDRESS_HEX.lower()} ", "name": "Office air monitor"}
        )
        self.assertEqual(flow.unique_id, ADDRESS_HEX)
        self.assertEqual(result["data"],
                         {"address": ADDRESS_HEX, "name": "Office air monitor"})
        self.assertEqual(result["title"], "Office air monitor")

    async def test_manual_existing_device_is_rejected(self):
        flow = self.make_flow({ADDRESS_A})
        with self.assertRaisesRegex(AbortFlow, "already_configured"):
            await flow.async_step_user({"address": ADDRESS_A})

    async def test_discovery_rejects_unsupported_device_without_creating_unique_id(self):
        flow = self.make_flow()
        with patch.object(flow, "async_set_unique_id", new_callable=AsyncMock) as unique:
            result = await flow.async_step_bluetooth(discovery(name="Unrelated monitor"))
        self.assertEqual(result, {"type": "abort", "reason": "not_supported"})
        unique.assert_not_awaited()

    async def test_supported_discovery_requires_confirmation_and_normalizes_id(self):
        flow = self.make_flow()
        result = await flow.async_step_bluetooth(discovery(f" {ADDRESS_HEX.lower()} "))
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "bluetooth_confirm")
        self.assertEqual(flow.unique_id, ADDRESS_HEX)
        self.assertTrue(flow.confirm_only)
        result = await flow.async_step_bluetooth_confirm({})
        self.assertEqual(result["data"], {"address": flow.unique_id, "name": "Birdie Pro"})

    async def test_service_uuid_identifies_candidate_without_name(self):
        flow = self.make_flow()
        result = await flow.async_step_bluetooth(discovery(
            name=None, service_uuids=[constants.ENVIRONMENTAL_SERVICE_UUID.upper()]
        ))
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["description_placeholders"]["name"], ADDRESS_A)

    async def test_discovery_confirmation_rechecks_device_added_manually(self):
        configured = set()
        discovered_flow = self.make_flow(configured)
        await discovered_flow.async_step_bluetooth(discovery())
        manual_flow = self.make_flow(configured)
        result = await manual_flow.async_step_user({"address": ADDRESS_A})
        self.assertEqual(result["type"], "create_entry")
        # Model HA storing the completed manual flow before discovery confirmation.
        configured.add(manual_flow.unique_id)
        with self.assertRaisesRegex(AbortFlow, "already_configured"):
            await discovered_flow.async_step_bluetooth_confirm({})

    async def test_dropdown_distinguishes_names_and_accepts_manual_address(self):
        flow = self.make_flow()
        flow._scan_complete = True
        self.cached.return_value = [discovery(), discovery(ADDRESS_B)]
        form = await flow.async_step_user()
        schema = form["data_schema"]
        selector = next(value for key, value in schema.schema.items() if key.schema == "address")
        self.assertEqual(selector.config["options"], [
            {"value": ADDRESS_A, "label": f"Birdie Pro ({ADDRESS_A})"},
            {"value": ADDRESS_B, "label": f"Birdie Pro ({ADDRESS_B})"},
        ])
        manual_input = schema({"address": ADDRESS_C, "name": "Other monitor"})
        result = await flow.async_step_user(manual_input)
        self.assertEqual(result["data"], {"address": ADDRESS_C, "name": "Other monitor"})

    async def test_selected_discovery_retains_name_without_address_label(self):
        flow = self.make_flow()
        flow._scan_complete = True
        self.cached.return_value = [discovery()]
        form = await flow.async_step_user()
        result = await flow.async_step_user(form["data_schema"]({"address": ADDRESS_A}))
        self.assertEqual(result["title"], "Birdie Pro")
        self.assertEqual(result["data"]["name"], "Birdie Pro")

    async def test_cached_discovery_filters_configured_duplicates_and_unsupported(self):
        flow = self.make_flow({ADDRESS_A})
        flow._scan_complete = True
        # A previously offered candidate can become configured while the form is open.
        flow._discovered_devices[ADDRESS_A] = "Birdie Pro"
        self.cached.return_value = [
            discovery(), discovery(ADDRESS_B), discovery(ADDRESS_C, name="Unrelated monitor")
        ]
        await flow.async_step_user()
        self.assertEqual(flow._discovered_devices, {ADDRESS_B: "Birdie Pro"})
        self.assertEqual(self.cached.call_count, 2)

    async def test_cached_candidate_skips_waiting_and_callback_registration(self):
        flow = self.make_flow()
        self.cached.return_value = [discovery()]
        with patch.object(flow_module, "async_register_callback") as register:
            await flow._async_scan_for_devices()
        register.assert_not_called()
        self.assertEqual(flow._discovered_devices, {ADDRESS_A: "Birdie Pro"})

    async def test_scan_timeout_and_cancellation_unregister_all_callbacks(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                flow = self.make_flow()
                unloads = []
                ready = asyncio.Event()

                def register(*args):
                    unload = Mock()
                    unloads.append(unload)
                    ready.set()
                    return unload

                with patch.object(flow_module, "async_register_callback", side_effect=register), \
                     patch.object(flow_module, "DISCOVERY_TIMEOUT", 0.01 if not cancel else 10):
                    task = asyncio.create_task(flow._async_scan_for_devices())
                    await ready.wait()
                    if cancel:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        await task
                self.assertTrue(unloads)
                for unload in unloads:
                    unload.assert_called_once_with()

    async def test_scan_discovery_finishes_and_unregisters_callbacks(self):
        flow = self.make_flow()
        callbacks = []
        unloads = []
        ready = asyncio.Event()

        def register(hass, callback, matcher, mode):
            callbacks.append(callback)
            unload = Mock()
            unloads.append(unload)
            ready.set()
            return unload

        with patch.object(flow_module, "async_register_callback", side_effect=register):
            task = asyncio.create_task(flow._async_scan_for_devices())
            await ready.wait()
            callbacks[0](discovery(), "advertisement")
            await task
        self.assertEqual(flow._discovered_devices, {ADDRESS_A: "Birdie Pro"})
        for unload in unloads:
            unload.assert_called_once_with()


class OptionsFlowTests(unittest.IsolatedAsyncioTestCase):
    def make_flow(self, options=None):
        flow = flow_module.BirdieOptionsFlow()
        flow.config_entry = types.SimpleNamespace(options=options or {})
        return flow

    def defaults(self):
        return {key: default for key, default, *_ in flow_module.POLLING_OPTIONS}

    async def test_default_form_and_saved_values_are_integer_seconds(self):
        flow = self.make_flow()
        form = await flow.async_step_init()
        validated = form["data_schema"]({})
        self.assertEqual(validated, self.defaults())
        result = await flow.async_step_init(validated)
        self.assertEqual(result["type"], "create_entry")
        self.assertTrue(all(type(value) is int for value in result["data"].values()))

    async def test_existing_per_device_options_are_form_defaults(self):
        selected = self.defaults() | {constants.CONF_POLL_INTERVAL: 240}
        flow = self.make_flow(selected)
        form = await flow.async_step_init()
        self.assertEqual(form["data_schema"]({}), selected)

    async def test_all_selector_ranges_reject_outside_values_and_accept_boundaries(self):
        flow = self.make_flow()
        form = await flow.async_step_init()
        for key, default, minimum, maximum in flow_module.POLLING_OPTIONS:
            for value in (minimum, maximum):
                with self.subTest(key=key, value=value):
                    self.assertEqual(form["data_schema"]({key: value})[key], value)
            for value in (minimum - 1, maximum + 1, "invalid", float("inf")):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(vol.Invalid):
                        form["data_schema"]({key: value})

    async def test_fractional_or_nan_seconds_are_rejected_for_every_option(self):
        for key, default, *_ in flow_module.POLLING_OPTIONS:
            for value in (default - 0.5, float("nan")):
                with self.subTest(key=key, value=value):
                    flow = self.make_flow()
                    result = await flow.async_step_init(self.defaults() | {key: value})
                    self.assertEqual(result["type"], "form")
                    self.assertEqual(result["errors"], {key: "whole_seconds"})

    async def test_maximum_sample_age_includes_larger_interval_and_session_timeout(self):
        for poll, cooldown in ((300, 120), (120, 300)):
            for delta, expected_type in ((-1, "form"), (0, "create_entry")):
                with self.subTest(poll=poll, cooldown=cooldown, delta=delta):
                    options = self.defaults() | {
                        constants.CONF_POLL_INTERVAL: poll,
                        constants.CONF_CONNECTION_COOLDOWN: cooldown,
                        constants.CONF_SESSION_TIMEOUT: 45,
                        constants.CONF_MAX_SAMPLE_AGE: max(poll, cooldown) + 45 + delta,
                    }
                    result = await self.make_flow().async_step_init(options)
                    self.assertEqual(result["type"], expected_type)
                    if expected_type == "form":
                        self.assertEqual(result["errors"], {
                            constants.CONF_MAX_SAMPLE_AGE: "sample_age_too_short"
                        })

    async def test_options_listener_reloads_only_requested_device(self):
        hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(async_reload=AsyncMock()))
        entry = types.SimpleNamespace(entry_id="birdie-test-entry-b")
        await boundary.integration_module._async_options_updated(hass, entry)
        hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)


if __name__ == "__main__":
    unittest.main()
