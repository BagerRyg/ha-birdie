"""Behavioral tests for Birdie polling, with HA/BLE boundaries stubbed.

Run with Python 3.12+: python -m unittest discover -s tests -v
The real coordinator, BLE client, parsers, and models are loaded from custom_components/birdie/.
These tests require no Home Assistant installation or physical Bluetooth device.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def module(name, **attrs):
    result = types.ModuleType(name)
    result.__dict__.update(attrs)
    sys.modules[name] = result
    return result


class Subscriptable:
    @classmethod
    def __class_getitem__(cls, _):
        return cls


class ConfigEntry(Subscriptable):
    data = {"address": "02:00:00:00:00:01", "name": "Birdie Pro"}
    title = "Birdie Pro"
    options = {}


class UpdateFailed(Exception):
    pass


class HomeAssistantError(Exception):
    pass


class DataUpdateCoordinator(Subscriptable):
    """Minimal HA boundary, including HA's failed-update availability contract."""

    def __init__(self, hass, logger, **kwargs):
        self.hass = hass
        self.update_interval = kwargs["update_interval"]
        self.last_update_success = True
        self.publications = []
        self.listener_calls = 0
        self.shutdown_called = False

    def async_set_updated_data(self, data):
        self.data = data
        self.last_update_success = True
        self.publications.append(data)

    def async_update_listeners(self):
        self.listener_calls += 1

    async def async_shutdown(self):
        self.shutdown_called = True

    async def async_refresh(self):
        try:
            data = await self._async_update_data()
        except UpdateFailed:
            self.last_update_success = False
        else:
            self.async_set_updated_data(data)


class BleakError(Exception):
    pass


module("homeassistant")
module("homeassistant.config_entries", ConfigEntry=ConfigEntry)
module("homeassistant.const", CONF_ADDRESS="address",
       EVENT_HOMEASSISTANT_STOP="homeassistant_stop")
module("homeassistant.core", HomeAssistant=object, CALLBACK_TYPE=Callable[[], None],
       callback=lambda fn: fn)
module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)
module("homeassistant.helpers")
module("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=DataUpdateCoordinator,
       UpdateFailed=UpdateFailed)
CALL_LATER = Mock(side_effect=lambda *args: Mock())
module("homeassistant.helpers.event", async_call_later=CALL_LATER)
module("homeassistant.components")
BLUETOOTH = module("homeassistant.components.bluetooth", async_scanner_count=Mock(return_value=1),
                   async_ble_device_from_address=Mock(return_value=object()),
                   async_register_callback=Mock(), async_rediscover_address=Mock(),
                   BluetoothScanningMode=types.SimpleNamespace(ACTIVE="active"),
                   BluetoothCallbackMatcher=dict)
module("homeassistant.components.bluetooth.match", ADDRESS="address", BluetoothCallbackMatcher=dict)
module("bleak", BleakClient=object)
module("bleak.backends")
module("bleak.backends.client", BaseBleakClient=object)
module("bleak.backends.device", BLEDevice=object)
module("bleak.exc", BleakError=BleakError)
module("bleak_retry_connector", close_stale_connections_by_address=AsyncMock(),
       establish_connection=AsyncMock(), get_device=AsyncMock(return_value=None))
package = module("birdie_under_test")
package.__path__ = [str(ROOT / "custom_components" / "birdie")]
coordinator_module = importlib.import_module("birdie_under_test.coordinator")
client_module = importlib.import_module("birdie_under_test.client")
constants = importlib.import_module("birdie_under_test.const")
integration_module = module("birdie_under_test.integration")
integration_module.__package__ = "birdie_under_test"
integration_path = ROOT / "custom_components" / "birdie" / "__init__.py"
exec(compile(integration_path.read_text(encoding="utf-8"), str(integration_path), "exec"),
     integration_module.__dict__)
BirdieBleError = client_module.BirdieBleError
BirdieData = coordinator_module.BirdieData

SNAPSHOT = {
    "temperature_celsius": 22.3,
    "humidity_percent": 39.1,
    "co2_ppm": 700,
    "co2_threshold_ppm": 1000,
    "cool_down_minutes": 60,
}


class FakeClient:
    def __init__(self):
        self.events = []
        self.connected = False
        self.connect_result = True
        self.values = dict(SNAPSHOT)
        self.read_error = None
        self.write_error = None
        self.disconnect_error = None
        self.read_started = asyncio.Event()
        self.read_gate = None
        self.active = 0
        self.max_active = 0

    async def async_connect(self):
        self.events.append("connect")
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.connected = self.connect_result
        return self.connect_result

    async def async_read_initial(self):
        self.events.append("read")
        self.read_started.set()
        if self.read_gate is not None:
            await self.read_gate.wait()
        if self.read_error:
            raise self.read_error
        return dict(self.values)

    async def async_disconnect(self):
        self.events.append("disconnect")
        if self.disconnect_error:
            raise self.disconnect_error
        self.connected = False
        self.active = max(0, self.active - 1)

    async def async_write_co2_threshold(self, value):
        self.events.append(("write_threshold", value))
        if self.write_error:
            raise self.write_error
        self.values["co2_threshold_ppm"] = value
        return value

    async def async_write_cool_down(self, value):
        self.events.append(("write_cool_down", value))
        if self.write_error:
            raise self.write_error
        self.values["cool_down_minutes"] = value
        return value

    async def async_start_notify(self):
        raise AssertionError("Polling must never subscribe to notifications")


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        BLUETOOTH.async_register_callback.reset_mock()

    def make_coordinator(self, options=None):
        loop = asyncio.get_running_loop()
        clock = types.SimpleNamespace(time=Mock(return_value=1000.0),
                                      call_soon_threadsafe=loop.call_soon_threadsafe)
        hass = types.SimpleNamespace(loop=clock)
        entry = ConfigEntry()
        entry.options = options or {}
        coordinator = coordinator_module.BirdieCoordinator(hass, entry)
        coordinator.client = FakeClient()
        return coordinator

    async def test_success_disconnects_before_publishing_and_uses_three_minutes(self):
        coordinator = self.make_coordinator()
        await coordinator.async_refresh()
        self.assertEqual(coordinator.client.events, ["connect", "read", "disconnect"])
        self.assertFalse(coordinator.client.connected)
        self.assertTrue(coordinator.data.available)
        self.assertEqual(coordinator.data.temperature_celsius, 22.3)
        self.assertEqual(coordinator.update_interval.total_seconds(), 180)
        BLUETOOTH.async_register_callback.assert_not_called()

    async def test_busy_retains_recent_snapshot_without_refreshing_its_age(self):
        coordinator = self.make_coordinator()
        await coordinator.async_refresh()
        old_data = coordinator.data
        coordinator.client.connect_result = False
        coordinator.client.events.clear()
        coordinator.hass.loop.time.return_value = 1180
        await coordinator.async_refresh()
        self.assertTrue(coordinator.last_update_success)
        self.assertIs(coordinator.data, old_data)
        self.assertEqual(coordinator._last_success, 1000)
        self.assertEqual(coordinator.client.events, ["connect", "disconnect"])

    async def test_read_errors_disconnect_and_do_not_publish(self):
        for error in (BirdieBleError("failed"), OSError("adapter failed"), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                coordinator = self.make_coordinator()
                coordinator.client.read_error = error
                await coordinator.async_refresh()
                self.assertFalse(coordinator.last_update_success)
                self.assertFalse(coordinator.client.connected)
                self.assertEqual(coordinator.publications, [])

    async def test_missing_and_nonfinite_environmental_values_never_refresh_stale_temperature(self):
        for key in ("temperature_celsius", "humidity_percent", "co2_ppm"):
            for value in (None, "unknown", float("nan"), float("inf"), float("-inf")):
                with self.subTest(key=key, value=value):
                    coordinator = self.make_coordinator()
                    coordinator.data = coordinator.data.updated(temperature_celsius=15, available=True)
                    coordinator.client.values[key] = value
                    await coordinator.async_refresh()
                    self.assertFalse(coordinator.last_update_success)
                    self.assertEqual(coordinator.data.temperature_celsius, 15)
                    self.assertEqual(coordinator.publications, [])
                    self.assertFalse(coordinator.client.connected)

    async def test_missing_temperature_key_rejected(self):
        coordinator = self.make_coordinator()
        del coordinator.client.values["temperature_celsius"]
        with self.assertRaises(UpdateFailed):
            await coordinator._async_update_data()
        self.assertFalse(coordinator.client.connected)

    async def test_total_session_timeout_cleans_up(self):
        coordinator = self.make_coordinator()
        coordinator.client.read_gate = asyncio.Event()
        coordinator.session_timeout = 0.01
        with self.assertRaises(UpdateFailed):
            await asyncio.wait_for(coordinator._async_update_data(), timeout=0.5)
        self.assertFalse(coordinator.client.connected)
        self.assertEqual(coordinator.client.events[-1], "disconnect")

    async def test_cancelled_read_cleans_up_and_propagates_cancellation(self):
        coordinator = self.make_coordinator()
        coordinator.client.read_gate = asyncio.Event()
        task = asyncio.create_task(coordinator._async_update_data())
        await coordinator.client.read_started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(coordinator.client.connected)
        self.assertEqual(coordinator.client.events[-1], "disconnect")

    async def test_successful_writes_take_fresh_snapshot_and_release_connection(self):
        for method, value, field, event in (
            ("async_set_co2_threshold", 1200, "co2_threshold_ppm", "write_threshold"),
            ("async_set_cool_down", 90, "cool_down_minutes", "write_cool_down"),
        ):
            with self.subTest(method=method):
                coordinator = self.make_coordinator()
                await getattr(coordinator, method)(value)
                self.assertEqual(coordinator.client.events, ["connect", (event, value), "read", "disconnect"])
                self.assertFalse(coordinator.client.connected)
                self.assertTrue(coordinator.data.available)
                self.assertEqual(getattr(coordinator.data, field), value)
                self.assertEqual(coordinator.data.temperature_celsius, 22.3)

    async def test_failed_write_disconnects_and_marks_previous_data_unavailable(self):
        for method in ("async_set_co2_threshold", "async_set_cool_down"):
            with self.subTest(method=method):
                coordinator = self.make_coordinator()
                coordinator.data = coordinator.data.updated(temperature_celsius=15, available=True)
                coordinator.client.write_error = BirdieBleError("write failed")
                value = 1200 if "co2" in method else 90
                with self.assertRaises(HomeAssistantError):
                    await getattr(coordinator, method)(value)
                self.assertFalse(coordinator.client.connected)
                self.assertFalse(coordinator.data.available)
                self.assertEqual(coordinator.data.temperature_celsius, 15)
                self.assertNotIn("read", coordinator.client.events)

    async def test_successful_write_with_bad_snapshot_is_not_published(self):
        coordinator = self.make_coordinator()
        coordinator.client.values.pop("temperature_celsius")
        with self.assertRaises(HomeAssistantError):
            await coordinator.async_set_co2_threshold(1500)
        self.assertFalse(coordinator.client.connected)
        self.assertFalse(coordinator.data.available)
        self.assertEqual(coordinator.publications, [])

    async def test_verified_write_readback_used_when_optional_setting_missing_from_snapshot(self):
        for method, client_method, requested, read_back, field in (
            ("async_set_co2_threshold", "async_write_co2_threshold", 1200, 1201, "co2_threshold_ppm"),
            ("async_set_cool_down", "async_write_cool_down", 90, 89, "cool_down_minutes"),
        ):
            with self.subTest(method=method):
                coordinator = self.make_coordinator()
                coordinator.client.values.pop(field)
                write = AsyncMock(return_value=read_back)
                setattr(coordinator.client, client_method, write)
                await getattr(coordinator, method)(requested)
                write.assert_awaited_once_with(requested)
                self.assertEqual(getattr(coordinator.data, field), read_back)
                self.assertEqual(coordinator.data.temperature_celsius, 22.3)
                self.assertTrue(coordinator.data.available)
                self.assertFalse(coordinator.client.connected)

    async def test_verified_write_readback_does_not_publish_when_full_snapshot_fails(self):
        for method, client_method, value, field in (
            ("async_set_co2_threshold", "async_write_co2_threshold", 1200, "co2_threshold_ppm"),
            ("async_set_cool_down", "async_write_cool_down", 90, "cool_down_minutes"),
        ):
            with self.subTest(method=method):
                coordinator = self.make_coordinator()
                coordinator.client.read_error = BirdieBleError("snapshot failed")
                write = AsyncMock(return_value=value)
                setattr(coordinator.client, client_method, write)
                with self.assertRaises(HomeAssistantError):
                    await getattr(coordinator, method)(value)
                write.assert_awaited_once_with(value)
                self.assertIsNone(getattr(coordinator.data, field))
                self.assertEqual(coordinator.publications, [])
                self.assertFalse(coordinator.data.available)
                self.assertFalse(coordinator.client.connected)

    async def test_invalid_write_does_not_connect(self):
        coordinator = self.make_coordinator()
        for method, values in ((coordinator.async_set_co2_threshold, (399, 5001)),
                               (coordinator.async_set_cool_down, (0, 241))):
            for value in values:
                with self.assertRaises(HomeAssistantError):
                    await method(value)
        self.assertEqual(coordinator.client.events, [])

    async def test_concurrent_poll_and_write_are_serialized_through_disconnect(self):
        coordinator = self.make_coordinator()
        # Disable the separate cooldown policy to isolate mutual exclusion.
        coordinator.connection_cooldown = 0
        gate = coordinator.client.read_gate = asyncio.Event()
        poll = asyncio.create_task(coordinator._async_update_data())
        await coordinator.client.read_started.wait()
        write = asyncio.create_task(coordinator.async_set_co2_threshold(1300))
        await asyncio.sleep(0)
        self.assertEqual(coordinator.client.events, ["connect", "read"])
        gate.set()
        await asyncio.gather(poll, write)
        self.assertEqual(coordinator.client.max_active, 1)
        self.assertEqual(coordinator.client.events, ["connect", "read", "disconnect", "connect",
                                                    ("write_threshold", 1300), "read", "disconnect"])

    async def test_shutdown_during_poll_prevents_queued_write_from_reconnecting(self):
        coordinator = self.make_coordinator()
        gate = coordinator.client.read_gate = asyncio.Event()
        poll = asyncio.create_task(coordinator._async_update_data())
        await coordinator.client.read_started.wait()
        write = asyncio.create_task(coordinator.async_set_co2_threshold(1300))
        stop = asyncio.create_task(coordinator.async_shutdown())
        await asyncio.sleep(0)
        self.assertTrue(coordinator._stopping)
        gate.set()
        await poll
        with self.assertRaises(HomeAssistantError):
            await write
        await stop
        self.assertEqual(coordinator.client.events.count("connect"), 1)
        self.assertFalse(coordinator.client.connected)
        self.assertFalse(coordinator.data.available)
        self.assertTrue(coordinator.shutdown_called)
        self.assertIsNone(coordinator._cancel_expiry)

    async def test_poll_after_shutdown_never_connects(self):
        coordinator = self.make_coordinator()
        await coordinator.async_shutdown()
        with self.assertRaises(UpdateFailed):
            await coordinator._async_update_data()
        self.assertNotIn("connect", coordinator.client.events)

    async def test_disconnect_failure_does_not_publish_available_snapshot(self):
        coordinator = self.make_coordinator()
        coordinator.client.disconnect_error = BirdieBleError("cannot release")
        await coordinator.async_refresh()
        self.assertFalse(coordinator.last_update_success)
        self.assertFalse(coordinator.data.available)
        self.assertEqual(coordinator.publications, [])

    async def test_configuration_applies_all_five_options(self):
        coordinator = self.make_coordinator({
            constants.CONF_POLL_INTERVAL: 240,
            constants.CONF_CONNECTION_COOLDOWN: 150,
            constants.CONF_RETRY_INTERVAL: 300,
            constants.CONF_SESSION_TIMEOUT: 30,
            constants.CONF_MAX_SAMPLE_AGE: 3600,
        })
        self.assertEqual(coordinator.poll_interval, 240)
        self.assertEqual(coordinator.connection_cooldown, 150)
        self.assertEqual(coordinator.retry_interval, 300)
        self.assertEqual(coordinator.session_timeout, 30)
        self.assertEqual(coordinator.max_sample_age, 3600)
        self.assertEqual(coordinator.update_interval.total_seconds(), 240)

    async def test_cooldown_sets_lower_bound_for_regular_and_failed_poll_intervals(self):
        coordinator = self.make_coordinator({constants.CONF_POLL_INTERVAL: 60,
                                             constants.CONF_CONNECTION_COOLDOWN: 120,
                                             constants.CONF_RETRY_INTERVAL: 90})
        self.assertEqual(coordinator.update_interval.total_seconds(), 120)
        coordinator.client.connect_result = False
        await coordinator.async_refresh()
        self.assertEqual(coordinator.update_interval.total_seconds(), 120)
        self.assertEqual(coordinator._next_connection_at, 1120)

    async def test_cooldown_prevents_manual_poll_and_write_reconnecting(self):
        coordinator = self.make_coordinator()
        await coordinator.async_refresh()
        previous = coordinator.data
        coordinator.client.events.clear()
        coordinator.hass.loop.time.return_value = 1050
        await coordinator.async_refresh()
        self.assertEqual(coordinator.data, previous)
        with self.assertRaisesRegex(HomeAssistantError, "cooldown"):
            await coordinator.async_set_co2_threshold(1300)
        self.assertEqual(coordinator.client.events, [])
        self.assertEqual(coordinator._last_success, 1000)

    async def test_queued_write_does_not_bypass_cooldown(self):
        coordinator = self.make_coordinator()
        gate = coordinator.client.read_gate = asyncio.Event()
        poll = asyncio.create_task(coordinator._async_update_data())
        await coordinator.client.read_started.wait()
        write = asyncio.create_task(coordinator.async_set_co2_threshold(1300))
        await asyncio.sleep(0)
        gate.set()
        await poll
        with self.assertRaisesRegex(HomeAssistantError, "cooldown"):
            await write
        self.assertEqual(coordinator.client.events, ["connect", "read", "disconnect"])

    async def test_timer_expires_at_exact_ttl_independently_of_polling(self):
        coordinator = self.make_coordinator({constants.CONF_MAX_SAMPLE_AGE: 3600})
        await coordinator.async_refresh()
        self.assertEqual(CALL_LATER.call_args.args[1], 3600)
        coordinator.hass.loop.time.return_value = 4599.999
        coordinator._async_expire_sample(None)
        self.assertTrue(coordinator.data.available)
        coordinator.hass.loop.time.return_value = 4600
        coordinator._async_expire_sample(None)
        self.assertFalse(coordinator.data.available)
        self.assertEqual(coordinator.data.temperature_celsius, 22.3)
        self.assertEqual(coordinator.client.events.count("connect"), 1)

    async def test_failure_does_not_reset_age_or_replace_expiry_and_recovers_on_new_snapshot(self):
        coordinator = self.make_coordinator({constants.CONF_MAX_SAMPLE_AGE: 3600,
                                             constants.CONF_RETRY_INTERVAL: 240})
        await coordinator.async_refresh()
        expiry = coordinator._cancel_expiry
        calls = CALL_LATER.call_count
        coordinator.hass.loop.time.return_value = 1180
        coordinator.client.connect_result = False
        await coordinator.async_refresh()
        self.assertEqual(coordinator._last_success, 1000)
        self.assertIs(coordinator._cancel_expiry, expiry)
        self.assertEqual(CALL_LATER.call_count, calls)
        self.assertEqual(coordinator.update_interval.total_seconds(), 240)
        coordinator.hass.loop.time.return_value = 4600
        coordinator._async_expire_sample(None)
        await coordinator.async_refresh()
        self.assertFalse(coordinator.last_update_success)
        self.assertFalse(coordinator.data.available)
        coordinator.hass.loop.time.return_value = 4840
        coordinator.client.connect_result = True
        coordinator.client.values["temperature_celsius"] = 21.1
        await coordinator.async_refresh()
        self.assertTrue(coordinator.last_update_success)
        self.assertTrue(coordinator.data.available)
        self.assertEqual(coordinator.data.temperature_celsius, 21.1)
        self.assertEqual(coordinator._last_success, 4840)
        self.assertEqual(coordinator.update_interval.total_seconds(), 180)

    async def test_new_complete_sample_replaces_previous_expiry(self):
        coordinator = self.make_coordinator()
        await coordinator.async_refresh()
        expiry = coordinator._cancel_expiry
        coordinator.hass.loop.time.return_value = 1180
        await coordinator.async_refresh()
        expiry.assert_called_once_with()
        self.assertIsNot(coordinator._cancel_expiry, expiry)
        self.assertEqual(coordinator._last_success, 1180)

    async def test_failed_write_preserves_fresh_sample_without_refreshing_age(self):
        coordinator = self.make_coordinator()
        await coordinator.async_refresh()
        coordinator.hass.loop.time.return_value = 1180
        coordinator.client.write_error = BirdieBleError("phone connected")
        with self.assertRaises(HomeAssistantError):
            await coordinator.async_set_co2_threshold(1300)
        self.assertTrue(coordinator.data.available)
        self.assertEqual(coordinator._last_success, 1000)
        self.assertFalse(coordinator.client.connected)

    async def test_expiry_still_runs_during_slow_poll(self):
        coordinator = self.make_coordinator({constants.CONF_MAX_SAMPLE_AGE: 3600})
        await coordinator.async_refresh()
        coordinator.hass.loop.time.return_value = 4590
        coordinator.client.read_started.clear()
        gate = coordinator.client.read_gate = asyncio.Event()
        poll = asyncio.create_task(coordinator.async_refresh())
        await coordinator.client.read_started.wait()
        coordinator.hass.loop.time.return_value = 4600
        coordinator._async_expire_sample(None)
        self.assertFalse(coordinator.data.available)
        gate.set()
        await poll
        self.assertTrue(coordinator.data.available)
        self.assertEqual(coordinator._last_success, 4600)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self):
        self.on_values = Mock()
        self.on_disconnect = Mock()
        hass = types.SimpleNamespace(loop=asyncio.get_running_loop())
        return client_module.BirdieBleClient(hass, "02:00:00:00:00:01", self.on_values, self.on_disconnect)

    def attach_ble(self, client):
        bleak = types.SimpleNamespace(is_connected=True, stop_notify=AsyncMock())

        async def disconnect():
            bleak.is_connected = False
            client._handle_disconnect(bleak)

        bleak.disconnect = AsyncMock(side_effect=disconnect)
        client._client = bleak
        client._services = object()
        return bleak

    async def test_expected_disconnect_does_not_invalidate_cached_snapshot(self):
        client = self.make_client()
        bleak = self.attach_ble(client)
        await client.async_disconnect()
        await asyncio.sleep(0)
        bleak.disconnect.assert_awaited_once()
        self.on_disconnect.assert_not_called()
        self.assertIsNone(client._client)
        self.assertIsNone(client._services)

    async def test_failed_disconnect_retains_handle_for_retry(self):
        client = self.make_client()
        bleak = self.attach_ble(client)
        bleak.disconnect.side_effect = BleakError("adapter failure")
        with self.assertRaises(BirdieBleError):
            await client.async_disconnect()
        self.assertIs(client._client, bleak)
        self.assertTrue(client.is_connected)

    async def test_disconnect_returning_still_connected_is_failure_and_retains_handle(self):
        client = self.make_client()
        bleak = self.attach_ble(client)
        bleak.disconnect.side_effect = None
        with self.assertRaises(BirdieBleError):
            await client.async_disconnect()
        self.assertIs(client._client, bleak)

    async def test_cleanup_failure_prevents_reconnect_and_retains_old_client(self):
        client = self.make_client()
        bleak = self.attach_ble(client)
        bleak.disconnect.side_effect = BleakError("adapter failure")
        with patch.object(client_module, "establish_connection", new_callable=AsyncMock) as connect:
            with self.assertRaises(BirdieBleError):
                await client.async_connect()
            connect.assert_not_awaited()
        self.assertIs(client._client, bleak)

    async def test_late_old_disconnect_callback_cannot_clear_new_connection(self):
        client = self.make_client()
        old = self.attach_ble(client)
        await client.async_disconnect()
        current = self.attach_ble(client)
        client._expected_disconnect = False
        services = client._services
        client._handle_disconnect(old)
        await asyncio.sleep(0)
        self.assertIs(client._client, current)
        self.assertIs(client._services, services)
        self.on_disconnect.assert_not_called()

    async def test_unexpected_current_disconnect_reports_unavailable(self):
        client = self.make_client()
        bleak = self.attach_ble(client)
        client._handle_disconnect(bleak)
        await asyncio.sleep(0)
        self.on_disconnect.assert_called_once()
        self.assertIsNone(client._client)

    async def test_read_snapshot_does_not_stream_partial_values_to_coordinator(self):
        client = self.make_client()
        raw = {
            constants.CO2_CHARACTERISTIC_UUID: (700).to_bytes(2, "little"),
            constants.TEMPERATURE_CHARACTERISTIC_UUID: (22300).to_bytes(4, "little", signed=True),
            constants.HUMIDITY_CHARACTERISTIC_UUID: (39100).to_bytes(4, "little"),
        }

        async def read(uuid):
            if uuid in raw:
                return raw[uuid]
            raise BirdieBleError("not available")

        with patch.object(client, "_async_read_uuid", side_effect=read):
            result = await client.async_read_initial()
        await asyncio.sleep(0)
        self.assertEqual(result["temperature_celsius"], 22.3)
        self.assertEqual(result["humidity_percent"], 39.1)
        self.assertEqual(result["co2_ppm"], 700)
        self.on_values.assert_not_called()

    async def test_connect_uses_one_attempt_and_does_not_start_notifications(self):
        client = self.make_client()
        services = types.SimpleNamespace(get_characteristic=Mock(return_value=object()))
        bleak = types.SimpleNamespace(is_connected=True, services=services)
        with patch.object(client_module, "establish_connection", new_callable=AsyncMock,
                          return_value=bleak) as connect, \
             patch.object(client, "async_start_notify", new_callable=AsyncMock) as notify:
            self.assertTrue(await client.async_connect())
            self.assertEqual(connect.call_args.kwargs["max_attempts"], 1)
            notify.assert_not_awaited()


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_address_subscription_keeps_discovery_but_never_reconnects(self):
        entry = ConfigEntry()
        entry.entry_id = "birdie-test-entry"
        cleanup_callbacks = []
        entry.async_on_unload = cleanup_callbacks.append
        entry.add_update_listener = Mock(return_value=Mock())
        loop = asyncio.get_running_loop()
        hass = types.SimpleNamespace(
            loop=types.SimpleNamespace(time=Mock(return_value=1000.0),
                                       call_soon_threadsafe=loop.call_soon_threadsafe),
            data={},
            config_entries=types.SimpleNamespace(
                async_forward_entry_setups=AsyncMock(),
                async_unload_platforms=AsyncMock(return_value=True),
            ),
            bus=types.SimpleNamespace(async_listen_once=Mock(return_value=Mock())),
        )
        ble_client = FakeClient()
        unsubscribe = Mock()
        with patch.object(coordinator_module, "BirdieBleClient", return_value=ble_client), \
             patch.object(BLUETOOTH, "async_register_callback", return_value=unsubscribe) as register, \
             patch.object(BLUETOOTH, "async_rediscover_address") as rediscover:
            original_connect = ble_client.async_connect

            async def connect_after_scan_registration():
                register.assert_called_once()
                return await original_connect()

            ble_client.async_connect = connect_after_scan_registration
            self.assertTrue(await integration_module.async_setup_entry(hass, entry))
            register.assert_called_once()
            registered_hass, on_advertisement, matcher, mode = register.call_args.args
            self.assertIs(registered_hass, hass)
            self.assertEqual(matcher, {"address": entry.data["address"]})
            self.assertEqual(mode, BLUETOOTH.BluetoothScanningMode.ACTIVE)
            self.assertIn(unsubscribe, cleanup_callbacks)
            self.assertTrue(entry.runtime_data.data.available)
            self.assertEqual(ble_client.events, ["connect", "read", "disconnect"])

            # Discovery must not become a second connection/polling scheduler,
            # including when the normal minimum cooldown has already elapsed.
            ble_client.events.clear()
            hass.loop.time.return_value = 2000.0
            for _ in range(3):
                self.assertIsNone(on_advertisement(types.SimpleNamespace(address=entry.data["address"]),
                                                  "advertisement"))
            await asyncio.sleep(0)
            self.assertEqual(ble_client.events, [])
            self.assertFalse(ble_client.connected)

            self.assertTrue(await integration_module.async_unload_entry(hass, entry))
            self.assertTrue(entry.runtime_data.shutdown_called)
            self.assertNotIn(entry.entry_id, hass.data[constants.DOMAIN])
            rediscover.assert_called_once_with(hass, entry.data["address"])
            # HA invokes registered callbacks when the config entry unloads.
            for cleanup in cleanup_callbacks:
                cleanup()
            unsubscribe.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
