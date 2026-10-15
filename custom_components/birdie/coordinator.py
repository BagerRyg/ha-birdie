"""Coordinator for the Birdie integration."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import math
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ble import normalize_address
from .client import BirdieBleClient, BirdieBleError
from .const import (
    BIRDIE_STATE_VALUES,
    CONF_CONNECTION_COOLDOWN,
    CONF_MAX_SAMPLE_AGE,
    CONF_NAME,
    CONF_POLL_INTERVAL,
    CONF_RETRY_INTERVAL,
    CONF_SESSION_TIMEOUT,
    DEFAULT_CONNECTION_COOLDOWN,
    DEFAULT_MAX_SAMPLE_AGE,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_RETRY_INTERVAL,
    DEFAULT_SESSION_TIMEOUT,
    DOMAIN,
    UNKNOWN_STATE,
)
from .models import BirdieData

_LOGGER = logging.getLogger(__name__)

ENVIRONMENTAL_FIELDS = ("temperature_celsius", "humidity_percent", "co2_ppm")


class BirdieCooldownError(BirdieBleError):
    """Bluetooth is deliberately being left available for the phone."""


class _ManualUpdateFilter(logging.Filter):
    """Suppress noisy DataUpdateCoordinator debug updates."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter only the automatic coordinator data update debug line."""
        return not (
            record.msg == "Manually updated %s data" and record.args == (DOMAIN,)
        )


_LOGGER.addFilter(_ManualUpdateFilter())


BirdieConfigEntry = ConfigEntry["BirdieCoordinator"]


class BirdieCoordinator(DataUpdateCoordinator[BirdieData]):
    """Coordinate Birdie BLE data for Home Assistant entities."""

    config_entry: BirdieConfigEntry

    def __init__(self, hass: HomeAssistant, entry: BirdieConfigEntry) -> None:
        """Initialize the coordinator."""
        address = normalize_address(entry.data[CONF_ADDRESS])
        name = entry.data.get(CONF_NAME) or entry.title or address
        self.poll_interval = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        self.connection_cooldown = entry.options.get(
            CONF_CONNECTION_COOLDOWN, DEFAULT_CONNECTION_COOLDOWN
        )
        self.retry_interval = entry.options.get(CONF_RETRY_INTERVAL, DEFAULT_RETRY_INTERVAL)
        self.session_timeout = entry.options.get(
            CONF_SESSION_TIMEOUT, DEFAULT_SESSION_TIMEOUT
        )
        self.max_sample_age = entry.options.get(CONF_MAX_SAMPLE_AGE, DEFAULT_MAX_SAMPLE_AGE)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=max(self.poll_interval, self.connection_cooldown)
            ),
        )
        self.address = address
        self.device_name = name
        self.data = BirdieData(address=address, name=name)
        self.client = BirdieBleClient(
            hass,
            address,
            self._async_handle_values,
            self._async_handle_disconnect,
        )
        self._connect_lock = asyncio.Lock()
        self._stopping = False
        self._last_success: float | None = None
        self._next_connection_at = 0.0
        self._cancel_expiry: CALLBACK_TYPE | None = None

    @property
    def model(self) -> str:
        """Return the best known model name."""
        if self.device_name.startswith("Birdie Pro"):
            return "Birdie Pro"
        return "Birdie"

    async def _async_update_data(self) -> BirdieData:
        """Read one snapshot, releasing Bluetooth before the next interval."""
        try:
            data = await self._async_transaction()
        except (BirdieBleError, TimeoutError, OSError) as err:
            if self._stopping:
                raise UpdateFailed("Birdie integration is stopping") from err
            self.update_interval = timedelta(
                seconds=max(self.retry_interval, self.connection_cooldown)
            )
            if self._sample_is_fresh():
                _LOGGER.debug("Birdie read failed; retaining recent sample: %s", err)
                return self.data
            raise UpdateFailed(f"Birdie reading unavailable or too old: {err}") from err
        if self._stopping:
            return data.updated(available=False)
        self.update_interval = timedelta(
            seconds=max(self.poll_interval, self.connection_cooldown)
        )
        return data

    async def _async_transaction(
        self, setting: str | None = None, value: int | None = None
    ) -> BirdieData:
        """Serialize polls and writes, including their guaranteed disconnect."""
        async with self._connect_lock:
            if self._stopping:
                raise BirdieBleError("Integration is stopping")
            remaining = self._next_connection_at - self.hass.loop.time()
            if remaining > 0:
                if setting is not None:
                    raise BirdieCooldownError(
                        f"Bluetooth cooldown active; try again in {math.ceil(remaining)} seconds"
                    )
                return self.data.updated(available=self._sample_is_fresh())
            try:
                async with asyncio.timeout(self.session_timeout):
                    if not await self.client.async_connect():
                        raise BirdieBleError("Device busy or unreachable")
                    written_values = {}
                    if setting == "co2_threshold":
                        assert value is not None
                        written_values["co2_threshold_ppm"] = (
                            await self.client.async_write_co2_threshold(value)
                        )
                    elif setting == "cool_down":
                        assert value is not None
                        written_values["cool_down_minutes"] = (
                            await self.client.async_write_cool_down(value)
                        )
                    values = await self.client.async_read_initial()
                    for key, read_back in written_values.items():
                        values.setdefault(key, read_back)
                    # A partial read must not refresh an old heating input.
                    if any(
                        not isinstance(values.get(key), (int, float))
                        or not math.isfinite(values[key])
                        for key in ENVIRONMENTAL_FIELDS
                    ):
                        raise BirdieBleError("Incomplete environmental reading")
                    data = self.data.updated(**values, available=True)
                    sample_time = self.hass.loop.time()
            finally:
                try:
                    await self.client.async_disconnect()
                finally:
                    self._next_connection_at = (
                        self.hass.loop.time() + self.connection_cooldown
                    )
            self._last_success = sample_time
            self._schedule_expiry()
            _LOGGER.debug(
                "Birdie snapshot read; Bluetooth released, next poll in %s seconds",
                max(self.poll_interval, self.connection_cooldown),
            )
            return data

    def _sample_is_fresh(self) -> bool:
        """Only successful complete reads reset the sample's age."""
        return (
            self._last_success is not None
            and self.hass.loop.time() - self._last_success < self.max_sample_age
        )

    @callback
    def _schedule_expiry(self) -> None:
        """Expire cached readings even if a retry is delayed or still running."""
        if self._stopping:
            return
        assert self._last_success is not None
        if self._cancel_expiry is not None:
            self._cancel_expiry()
        self._cancel_expiry = async_call_later(
            self.hass,
            max(0, self.max_sample_age - (self.hass.loop.time() - self._last_success)),
            self._async_expire_sample,
        )

    @callback
    def _async_expire_sample(self, _now: Any) -> None:
        """Stop exposing an old sample without extending the polling interval."""
        self._cancel_expiry = None
        if not self._stopping and not self._sample_is_fresh():
            self._set_available(False)

    async def async_shutdown(self) -> None:
        """Cancel polling and prevent queued operations from reconnecting."""
        self._stopping = True
        self.update_interval = None
        if self._cancel_expiry is not None:
            self._cancel_expiry()
            self._cancel_expiry = None
        await super().async_shutdown()
        async with self._connect_lock:
            await self.client.async_disconnect()
            self._set_available(False)

    async def async_set_co2_threshold(self, value: int) -> None:
        """Write CO2 threshold, read it back, and update coordinator data."""
        if not 400 <= value <= 5000:
            raise HomeAssistantError("CO2 threshold must be between 400 and 5000 ppm")
        try:
            data = await self._async_transaction("co2_threshold", value)
        except (BirdieBleError, TimeoutError, OSError) as err:
            self._set_available(self._sample_is_fresh())
            raise HomeAssistantError(f"Unable to write CO2 threshold: {err}") from err
        if self._stopping:
            return
        self.update_interval = timedelta(
            seconds=max(self.poll_interval, self.connection_cooldown)
        )
        self.async_set_updated_data(data)

    async def async_set_cool_down(self, value: int) -> None:
        """Write cool down minutes, read it back, and update coordinator data."""
        if not 1 <= value <= 240:
            raise HomeAssistantError("Cool down period must be between 1 and 240 minutes")
        try:
            data = await self._async_transaction("cool_down", value)
        except (BirdieBleError, TimeoutError, OSError) as err:
            self._set_available(self._sample_is_fresh())
            raise HomeAssistantError(f"Unable to write cool down period: {err}") from err
        if self._stopping:
            return
        self.update_interval = timedelta(
            seconds=max(self.poll_interval, self.connection_cooldown)
        )
        self.async_set_updated_data(data)

    @callback
    def _async_handle_values(self, values: dict[str, Any]) -> None:
        """Merge parsed BLE values into coordinator data."""
        previous_data = self.data
        updated_data = previous_data.updated(**values)
        if "birdie_state" in values:
            _LOGGER.debug(
                (
                    "Birdie state coordinator update for %s: %s -> %s; "
                    "co2=%s ppm threshold=%s ppm alarm=%s available=%s"
                ),
                self.address,
                _state_value(previous_data.birdie_state),
                _state_value(updated_data.birdie_state),
                updated_data.co2_ppm,
                updated_data.co2_threshold_ppm,
                updated_data.co2_alarm,
                updated_data.available,
            )
        self.async_set_updated_data(updated_data)

    @callback
    def _async_handle_disconnect(self) -> None:
        """Keep a recent snapshot through a lost or busy Bluetooth connection."""
        self._set_available(self._sample_is_fresh())

    @callback
    def _set_available(self, available: bool) -> None:
        """Update availability while preserving the latest values."""
        if self.data.available == available:
            return
        self.data = self.data.updated(available=available)
        self.async_update_listeners()


def _state_value(state: int | None) -> str:
    """Return a readable Birdie state for debug logging."""
    if state is None:
        return UNKNOWN_STATE
    return BIRDIE_STATE_VALUES.get(state, UNKNOWN_STATE)
