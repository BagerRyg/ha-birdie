"""Entry lifecycle and two-device isolation with mocked Home Assistant boundaries."""

import asyncio
import importlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_polling import (
    BLUETOOTH,
    ConfigEntry,
    FakeClient,
    Subscriptable,
    constants,
    coordinator_module,
    integration_module,
)


class LifecycleRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def make_hass(self):
        loop = asyncio.get_running_loop()
        return types.SimpleNamespace(
            loop=types.SimpleNamespace(time=Mock(return_value=1000.0),
                                       call_soon_threadsafe=loop.call_soon_threadsafe),
            data={},
            config_entries=types.SimpleNamespace(
                async_forward_entry_setups=AsyncMock(),
                async_unload_platforms=AsyncMock(return_value=True),
                async_reload=AsyncMock(),
            ),
            bus=types.SimpleNamespace(async_listen_once=Mock(return_value=Mock())),
        )

    def make_entry(self, suffix):
        entry = ConfigEntry()
        entry.entry_id = f"birdie-test-{suffix}"
        entry.data = {"address": f"02:00:00:00:00:{suffix:02d}", "name": "Birdie Pro"}
        entry.options = {}
        entry.cleanups = []
        entry.async_on_unload = entry.cleanups.append
        entry.add_update_listener = Mock(return_value=Mock())
        return entry

    async def test_platform_setup_failure_cancels_expiry_and_removes_runtime(self):
        hass, entry, client = self.make_hass(), self.make_entry(1), FakeClient()
        hass.config_entries.async_forward_entry_setups.side_effect = RuntimeError("platform failed")
        with patch.object(coordinator_module, "BirdieBleClient", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "platform failed"):
                await integration_module.async_setup_entry(hass, entry)
        self.assertTrue(entry.runtime_data.shutdown_called)
        self.assertTrue(entry.runtime_data._stopping)
        self.assertIsNone(entry.runtime_data._cancel_expiry)
        self.assertFalse(client.connected)
        self.assertNotIn(entry.entry_id, hass.data[constants.DOMAIN])

    async def test_setup_cancellation_still_releases_and_removes_runtime(self):
        hass, entry, client = self.make_hass(), self.make_entry(1), FakeClient()
        hass.config_entries.async_forward_entry_setups.side_effect = asyncio.CancelledError()
        with patch.object(coordinator_module, "BirdieBleClient", return_value=client):
            with self.assertRaises(asyncio.CancelledError):
                await integration_module.async_setup_entry(hass, entry)
        self.assertTrue(entry.runtime_data.shutdown_called)
        self.assertFalse(client.connected)
        self.assertNotIn(entry.entry_id, hass.data[constants.DOMAIN])

    async def test_unreachable_device_sets_up_unavailable_for_later_recovery(self):
        hass, entry, client = self.make_hass(), self.make_entry(1), FakeClient()
        client.connect_result = False
        with patch.object(coordinator_module, "BirdieBleClient", return_value=client):
            self.assertTrue(await integration_module.async_setup_entry(hass, entry))
        self.assertFalse(entry.runtime_data.data.available)
        self.assertFalse(client.connected)
        self.assertEqual(entry.runtime_data.update_interval.total_seconds(), 180)
        hass.config_entries.async_forward_entry_setups.assert_awaited_once()

    async def test_two_entries_unload_and_options_reload_only_target_device(self):
        hass = self.make_hass()
        first, second = self.make_entry(1), self.make_entry(2)
        client_a, client_b = FakeClient(), FakeClient()
        client_b.values["temperature_celsius"] = 18.5
        with patch.object(coordinator_module, "BirdieBleClient", side_effect=[client_a, client_b]):
            await integration_module.async_setup_entry(hass, first)
            await integration_module.async_setup_entry(hass, second)
        data_b = second.runtime_data.data
        expiry_b = second.runtime_data._cancel_expiry
        self.assertEqual(len(hass.data[constants.DOMAIN]), 2)
        await integration_module._async_options_updated(hass, first)
        hass.config_entries.async_reload.assert_awaited_once_with(first.entry_id)
        with patch.object(BLUETOOTH, "async_rediscover_address") as rediscover:
            self.assertTrue(await integration_module.async_unload_entry(hass, first))
            rediscover.assert_called_once_with(hass, first.data["address"])
        self.assertNotIn(first.entry_id, hass.data[constants.DOMAIN])
        self.assertIs(hass.data[constants.DOMAIN][second.entry_id], second.runtime_data)
        self.assertIs(second.runtime_data.data, data_b)
        self.assertIs(second.runtime_data._cancel_expiry, expiry_b)
        self.assertFalse(second.runtime_data._stopping)
        self.assertTrue(second.runtime_data.data.available)
        self.assertEqual(second.runtime_data.data.temperature_celsius, 18.5)

    async def test_failed_platform_unload_keeps_coordinator_running(self):
        hass, entry = self.make_hass(), self.make_entry(1)
        with patch.object(coordinator_module, "BirdieBleClient", return_value=FakeClient()):
            await integration_module.async_setup_entry(hass, entry)
        hass.config_entries.async_unload_platforms.return_value = False
        self.assertFalse(await integration_module.async_unload_entry(hass, entry))
        self.assertFalse(entry.runtime_data._stopping)
        self.assertIn(entry.entry_id, hass.data[constants.DOMAIN])

    async def test_identical_names_have_distinct_device_and_entity_identifiers(self):
        class CoordinatorEntity(Subscriptable):
            def __init__(self, coordinator):
                self.coordinator = coordinator

        registry = types.ModuleType("homeassistant.helpers.device_registry")
        registry.CONNECTION_BLUETOOTH = "bluetooth"
        registry.DeviceInfo = dict
        boundary = sys.modules["homeassistant.helpers.update_coordinator"]
        with patch.dict(sys.modules, {registry.__name__: registry}), \
             patch.object(boundary, "CoordinatorEntity", CoordinatorEntity, create=True):
            entity_module = importlib.import_module("birdie_under_test.entity")
            hass = self.make_hass()
            a = coordinator_module.BirdieCoordinator(hass, self.make_entry(1))
            b = coordinator_module.BirdieCoordinator(hass, self.make_entry(2))
            entity_a = entity_module.BirdieEntity(a, "temperature")
            entity_b = entity_module.BirdieEntity(b, "temperature")
        self.assertNotEqual(entity_a._attr_unique_id, entity_b._attr_unique_id)
        self.assertNotEqual(entity_a.device_info["identifiers"], entity_b.device_info["identifiers"])
        self.assertEqual(entity_a.device_info["name"], entity_b.device_info["name"])


if __name__ == "__main__":
    unittest.main()
