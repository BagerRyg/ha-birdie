<a href="https://birdie.design/products/birdie-pro"><img src="custom_components/birdie/brand/icon@2x.png" alt="Birdie mascot" width="96"></a>

# Birdie for Home Assistant

[![Version 0.1.7](https://img.shields.io/badge/version-0.1.7-blue)](custom_components/birdie/manifest.json)
[![MIT License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Home Assistant 2026.8.1+](https://img.shields.io/badge/Home_Assistant-2026.8.1%2B-18BCF2)](https://www.home-assistant.io/)

**Bring Birdie readings into Home Assistant while keeping direct Bluetooth access
for the Birdie phone app.**

A Home Assistant custom integration for Birdie BLE air quality monitors, including
[Birdie Pro](https://birdie.design/products/birdie-pro). This fork of
[dellzhui/ha-birdie](https://github.com/dellzhui/ha-birdie)
adds scheduled Bluetooth polling so Home Assistant and the Birdie phone app can
take turns connecting to the monitor.

Fork version: **`v0.1.7`**, based on upstream `v0.1.3`.

This fork requires **Home Assistant 2026.8.1 or newer**.

![Birdie sensor readings in Home Assistant](docs/images/ha-sensors.jpg)

*CO₂, temperature, humidity, and device readings in Home Assistant.*

[Prerequisites](#prerequisites) · [Install](#installation) · [Polling settings](#polling-settings) ·
[Thermostat fallback](#cached-readings-and-thermostat-fallback) ·
[Validation](docs/validation.md) · [Changelog](CHANGELOG.md)

## Why this fork exists

The original integration keeps a Bluetooth connection open to receive live GATT
notifications. On the Birdie Pro tested here, that prevented the Birdie iOS app
from connecting directly while Home Assistant was connected.

This fork uses **connect → read → disconnect → wait** instead. By default, Home
Assistant reads a snapshot, releases Bluetooth, and waits three minutes before
trying again. It does not subscribe to continuous notifications or reconnect
whenever an advertisement arrives. Active Bluetooth scanning remains enabled so
Home Assistant can rediscover the monitor between sessions.

This shares access over time; it does not provide simultaneous Bluetooth
connections or relay the phone app's traffic through Home Assistant. If the phone
is using Birdie when a poll is due, HA keeps its recent reading and retries later.
Close the Birdie app when finished if it continues holding the connection. A
phone connection attempt can still overlap a brief HA session; retry after that
session finishes.

## Polling settings

Open **Settings → Devices & services → Birdie → Configure**. Saving options
reloads that Birdie device. All values are whole seconds.

![Birdie polling options in Home Assistant](docs/images/ha-polling-options.jpg)

*Poll interval, Bluetooth cooldown, and retry controls. All five settings are explained below.*

| Setting | Default | Allowed range | Purpose |
| --- | ---: | ---: | --- |
| Poll interval | 180 (3 min) | 120–300 | Wait after a successful session before the next poll |
| Minimum Bluetooth cooldown | 120 (2 min) | 30–300 | Minimum gap after disconnect before another HA session |
| Retry delay | 180 (3 min) | 120–600 | Wait after a failed read before trying again |
| Session timeout | 45 | 10–60 | Limit time spent connecting, reading, or changing settings |
| Maximum sample age | 3600 (1 hour) | 180–3600 | Expire cached readings after this long without a successful read |

The longer of the cooldown and poll interval applies after success; the longer
of the cooldown and retry delay applies after failure. Disconnect cleanup runs
after the session timeout and has its own timeout of up to ten seconds. Manual
refreshes respect the cooldown, and setting writes during cooldown return a
message telling you when to retry. Polls and writes cannot overlap.

Maximum sample age must be at least
`max(poll interval, Bluetooth cooldown) + session timeout`.

The **Minimum Bluetooth cooldown** option is separate from Birdie's **Cool down
period** number entity, which controls the monitor's own behavior.

## Cached readings and thermostat fallback

A failed or busy connection does not immediately make a recent reading
unavailable. A complete, valid temperature/humidity/CO₂ snapshot resets its age;
failed or partial reads do not. At the maximum age, Birdie entities become
unavailable even if the next retry is delayed. A successful read restores them.
The cache is held in memory: after a restart or integration reload, HA needs a
new successful read.

This allows a 5–10-minute-old Birdie temperature to remain useful in a room
average, while excluding it completely after one hour with the default settings.
There is no gradual age-based weighting.

**Tado and RoomMind fallback require separate Home Assistant configuration.**
This integration does not choose a thermostat, create averages, or change heating
targets. To combine Birdie with a thermostat's own readings, create two
[Min/Max helpers](https://www.home-assistant.io/integrations/min_max/) with type
**Mean**: one for temperature and one for humidity. Each helper uses the matching
Birdie and thermostat sensor. Use matching units within each pair.

For example, replace these placeholder entity IDs with your own:

```yaml
sensor:
  - platform: min_max
    name: Room temperature
    type: mean
    round_digits: 1
    entity_ids:
      - sensor.birdie_temperature
      - sensor.tado_current_temperature

  - platform: min_max
    name: Room humidity
    type: mean
    round_digits: 1
    entity_ids:
      - sensor.birdie_humidity
      - sensor.tado_current_humidity
```

Use the resulting helpers as RoomMind's temperature and humidity inputs. While
both sources are available, they contribute equally. When Birdie expires, the
helpers use only Tado; when Birdie recovers, averaging resumes. If neither source
is available, the helper cannot provide a valid reading. Do not replace missing
temperature or humidity readings with zero.

## Entities

| Entity | Platform | Unit | Notes |
| --- | --- | --- | --- |
| CO₂ | Sensor | ppm | Carbon dioxide measurement |
| Temperature | Sensor | °C | Temperature measurement |
| Humidity | Sensor | % | Relative humidity measurement |
| State | Sensor | — | `up`, `down`, `cooldown`, or `unknown` |
| CO₂ alarm | Binary sensor | — | On when Birdie reports alarm state; unknown for missing or unrecognized state |
| Battery | Sensor | % | Battery level mapped from the device enum |
| CO₂ threshold | Number | ppm | Writable, 400–5000 ppm |
| Cool down period | Number | min | Writable, 1–240 min; distinct from Bluetooth cooldown |

Measurements and alarm state update on polls, rather than continuously.
Missing optional state, battery, or setting values become unknown instead of
retaining a value from an older snapshot. A complete temperature, humidity, and
CO₂ reading is required; humidity outside 0–100% is rejected.

## Installation

### Prerequisites

Before installing, check the following:

| Requirement | What you need |
| --- | --- |
| Home Assistant | **2026.8.1 or newer**, with access to install custom integrations and restart HA. HA OS and the official HA Container image include their required Python runtime. |
| Monitor | A powered, in-range Birdie Pro exposing the supported temperature, humidity, and CO₂ characteristics. Firmware `1.6.6` has been tested; see [compatibility limits](#bluetooth-requirements-and-compatibility) for other firmware/models. |
| Bluetooth connection | Either a supported local Bluetooth adapter accessible to HA, or a network-connected **active ESPHome Bluetooth Proxy** added through HA's ESPHome integration. Advertisement-only reception is insufficient. |
| HA Bluetooth setup | Configure the built-in **Bluetooth** integration under **Settings → Devices & services** and verify that an adapter or proxy is available. Local adapters in Docker/VMs need the host setup below. |
| Installation access | For manual installation, access to HA's persistent config directory; the files must be readable by HA and end up at `custom_components/birdie/manifest.json` within that directory. HACS is an optional alternative. |
| Initial device access | Close the Birdie phone app and any diagnostic tool holding its BLE connection during the first read. No operating-system pairing or pairing PIN is required by this integration. |

Downloads and updates require access to the relevant repositories/package
sources. Once installed, sensor polling uses local Bluetooth and requires no
Birdie cloud account, API key, MQTT broker, or internet connection. A proxy must
remain reachable over the local network. The Birdie app, Tado, RoomMind, and
HomeKit are optional and are not installation dependencies.

### Software dependencies

Home Assistant manages the runtime libraries; **do not manually install or
upgrade Python packages inside your HA installation** for this integration.

- The integration's [manifest](custom_components/birdie/manifest.json) declares
  the built-in `bluetooth` and `bluetooth_adapters` integrations as dependencies.
- HA's Bluetooth integration supplies `bleak`, `bleak-retry-connector`, and the
  Bluetooth adapter/backend libraries. For reference, HA `2026.8.1` specifies
  `bleak==3.0.2` and `bleak-retry-connector==4.6.3` in its
  [Bluetooth manifest](https://github.com/home-assistant/core/blob/2026.8.1/homeassistant/components/bluetooth/manifest.json).
  Later HA releases manage their own versions.
- HA supplies the `voluptuous` schema library. Birdie's own `requirements` list
  is empty because it uses these HA-managed dependencies.
- `requirements-test.txt` is only for running this repository's tests. The
  standalone diagnostic probe separately needs `bleak` on the computer running
  it; neither step is part of normal HA installation.

The Python **3.12+ test instructions below apply only to the mocked test suite**,
not to running Home Assistant itself. HA `2026.8.1` requires Python `3.14.2+`
according to its [package metadata](https://github.com/home-assistant/core/blob/2026.8.1/pyproject.toml);
use the runtime shipped with your HA OS/Container version.

### HACS custom repository

1. Have [HACS installed](https://www.hacs.xyz/docs/use/download/download/), then
   open HACS and choose **Custom repositories** from its menu.
2. Add `https://github.com/BagerRyg/ha-birdie` with category **Integration**.
3. Install Birdie **`v0.1.7`** or later from this fork. If HACS offers only an older
   version, use the manual installation below to install this fork's current code.
4. Restart Home Assistant.
5. Add Birdie from **Settings → Devices & services**, then open **Configure** to
   adjust polling settings.

This fork is a HACS custom repository; it is not the HACS default listing.

### Manual installation or migration from upstream

1. Back up your existing `custom_components/birdie` directory, if present.
2. Download this fork's default branch and copy its `custom_components/birdie`
   directory into Home Assistant's `custom_components` directory, replacing the
   existing Birdie files.
3. Restart Home Assistant. Add Birdie if it is not already configured.

The integration domain and entity unique IDs are unchanged. Existing Birdie
devices, entities, and automations should be retained when replacing upstream.
Install only one Birdie implementation: both projects use the same integration
domain. Ensure future HACS updates come from this fork; installing an upstream
version can replace the polling changes.

## Bluetooth requirements and compatibility

Provide either a supported local adapter or an active ESPHome Bluetooth Proxy.
Birdie is discovered through the integration's setup flow; operating-system
Bluetooth pairing is not required. Manual setup by Bluetooth address is also
available.

### Local adapters: HA OS, virtual machines, and Docker

HA OS handles the Bluetooth software setup. For a local USB adapter in a VM,
pass the adapter through to the HA guest. A host-only adapter is not sufficient.

For **Home Assistant Container on Linux**, BlueZ must run on the host and its
D-Bus socket must be accessible inside the container. HA documents BlueZ
**5.43 minimum / 5.63+ recommended**, recommends `dbus-broker`, and recommends
Linux kernel `5.15.62+`. Merge these settings into the existing HA service in
Docker Compose; retain its existing config volume and other settings:

```yaml
cap_add:
  - NET_ADMIN
  - NET_RAW
volumes:
  - /run/dbus:/run/dbus:ro
```

If the host socket is under `/var/run/dbus`, use
`/var/run/dbus:/run/dbus:ro` instead. These host requirements concern a **local
adapter**; a proxy supplies Bluetooth remotely. See HA's
[official Bluetooth setup and adapter guide](https://www.home-assistant.io/integrations/bluetooth/#requirements-for-linux-systems)
for installation-specific details and supported adapters.

### ESPHome Bluetooth Proxy

Use an ESPHome proxy with active GATT connection support, add it to HA through
the ESPHome integration, and keep its native API reachable from HA. For an ESP32,
the relevant firmware settings are below; this is a fragment, not a complete
board/network configuration:

```yaml
api:

esp32_ble_tracker:

bluetooth_proxy:
  active: true
```

Active **connections** are separate from active **scanning**. The proxy needs a
free connection slot when Birdie is polled; other BLE integrations can occupy
those slots. See the [ESPHome Bluetooth Proxy documentation](https://esphome.io/components/bluetooth_proxy/)
for supported hardware and complete configurations.

Advertisement-only proxies cannot perform the GATT reads and writes this
integration needs.

### Device and firmware compatibility

The original integration supports native Bluetooth and active ESPHome
proxies, with Birdie Pro firmware `1.6.0` and `1.6.1` UUID compatibility. Those
connection paths and UUID mappings are retained. Live polling and phone-access
verification used **one Birdie Pro**, firmware `1.6.6`, native Bluetooth, and
Home Assistant `2026.8.1` on the previous polling patch. It does not constitute
hardware validation of every `v0.1.6` fix. Other Birdie models and firmware must
expose compatible temperature, humidity, and CO₂ GATT characteristics; the Birdie
name alone does not establish compatibility. Physical multi-device operation and
proxy behavior have not been independently verified. See the
[validation record and limits](docs/validation.md).

### Setup and multiple devices

- Close the Birdie app during initial setup if it is holding the connection.
  Confirm that HA has a working Bluetooth adapter or active proxy and that the
  monitor is in range.
- Discovery choices include the Bluetooth address to distinguish monitors with
  the same name. If yours is missing, enter its address manually, for example
  `AA:BB:CC:DD:EE:FF`. Malformed addresses are rejected.
- Add each physical monitor once. Duplicate addresses are blocked, including
  competing discovery/manual setup flows. Give each entry a descriptive name.
- Each entry has separate entity IDs, cached readings, expiry timers, and polling
  options. These boundaries are tested with mocked devices; actual connection
  capacity still depends on the adapter or proxy.
- If readings stop while the phone is connected, close the app and allow the
  configured retry delay. Recent cached readings remain available until expiry.

Bluetooth advertisements and addresses help identify setup choices; they are
not cryptographic proof of a device's identity.

## Validation and diagnostics

Run the behavioral tests with Python 3.12 or newer from the repository root:

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
```

The 76-test behavioral suite passed locally on Python `3.12.14`. It exercises
integration code with mocked Home Assistant and Bluetooth boundaries, using the
real pinned test dependency `voluptuous==0.15.2`.
It covers polling, cleanup, expiry, setup validation, malformed device values,
and isolation between multiple entries. These are regression checks, not a
complete Home Assistant or hardware simulation. The
[validation record](docs/validation.md) separates source review, automated tests,
and the earlier live checks.

Enable debug logging when diagnosing connection problems:

```yaml
logger:
  default: warning
  logs:
    custom_components.birdie: debug
```

The upstream standalone probe is optional. Run it from a repository checkout in
a separate Python virtual environment on a computer with its own Bluetooth
adapter and the operating system's Bluetooth permissions. It connects directly
through that computer's adapter; it does not use HA's ESPHome proxy connection.
These commands are for that environment, not HA's managed Python installation:

```bash
python -m pip install bleak
python tools/birdie_probe.py --scan
python tools/birdie_probe.py --address <BIRDIE_BLUETOOTH_ADDRESS>
```

Its optional `--notify` mode holds a connection for live notifications, so it can
also prevent phone access while running. Avoid running it alongside normal HA
polling when testing connection sharing.

Contributions to the polling changes belong in
[this fork's pull requests](https://github.com/BagerRyg/ha-birdie/pulls).
Include the HA version, Birdie firmware, adapter/proxy type, polling options, and
relevant logs with private identifiers removed when describing a fix.
Debug logs can include Bluetooth addresses; redact these and any personal names
or local network details before posting them.

OTA updates, Birdie data-log downloads, and device firmware changes are outside
this integration's scope.

## Attribution and license

This fork builds on [dellzhui/ha-birdie](https://github.com/dellzhui/ha-birdie),
which provides the original Bluetooth support, discovery flow, entities, and
diagnostic tools. The polling changes are maintained by
[BagerRyg](https://github.com/BagerRyg) and fork contributors.

The Birdie mascot is the brand asset retained from upstream. This is a community
integration for Birdie devices.

The project remains licensed under the **MIT License**. See [LICENSE](LICENSE)
for the original copyright and license terms.
