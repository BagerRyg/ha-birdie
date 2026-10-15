"""The Birdie integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .const import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: Any, entry: Any) -> bool:
    """Set up Birdie from a config entry."""
    from homeassistant.components import bluetooth
    from homeassistant.components.bluetooth.match import ADDRESS, BluetoothCallbackMatcher
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP
    from homeassistant.core import callback

    from .coordinator import BirdieCoordinator

    coordinator = BirdieCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator
    entry.runtime_data = coordinator

    @callback
    def _async_bluetooth_callback(service_info: Any, change: Any) -> None:
        """Leave connections to scheduled polling."""
        return

    # Keep active scans enabled to rediscover Birdie between polling sessions.
    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_bluetooth_callback,
            BluetoothCallbackMatcher({ADDRESS: coordinator.address}),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
    )

    try:
        await coordinator.async_refresh()
        if not coordinator.last_update_success:
            _LOGGER.warning(
                "Unable to connect to Birdie device %s during setup; "
                "entities will remain unavailable until the next successful connection",
                coordinator.address,
            )

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except (Exception, asyncio.CancelledError):
        # No platform owns the coordinator after a failed or cancelled setup.
        # Stop its expiry/polling timers and release any pending connection.
        try:
            await coordinator.async_shutdown()
        except Exception:
            _LOGGER.exception("Unable to clean up Birdie after setup failed")
        finally:
            hass.data[DOMAIN].pop(entry.entry_id, None)
        raise

    async def _async_stop(event: Any) -> None:
        """Close the BLE connection when Home Assistant stops."""
        await coordinator.async_shutdown()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: Any, entry: Any) -> None:
    """Apply new timing settings by reloading this device only."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: Any, entry: Any) -> bool:
    """Unload a Birdie config entry."""
    from homeassistant.components import bluetooth

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = entry.runtime_data
        await coordinator.async_shutdown()
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        bluetooth.async_rediscover_address(hass, coordinator.address)
    return unload_ok
