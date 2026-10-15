# Validation record

This record distinguishes source review, mocked regression tests, and physical
device checks. It is not a security certification or a guarantee that every
Birdie model, firmware, Bluetooth adapter, or proxy will work.

## Source review

The reviewed baseline was
[`v0.1.5` at `3fcadc4`](https://github.com/BagerRyg/ha-birdie/commit/3fcadc4).
The source audit did not confirm a security vulnerability or find committed
credentials in the reviewed source. It identified correctness and reliability
issues addressed by `v0.1.6`:

| Area | Change |
| --- | --- |
| Setup | Validate manual addresses, distinguish discovery choices by address, allow manual entry, and guard against duplicate confirmation |
| Connection failures | Normalize Bluetooth failures before a connection is established so retry handling still applies |
| Setting writes | Handle malformed write/readback values as integration errors |
| Cancellation and unload | Complete bounded disconnect cleanup and release resources after failed setup |
| Snapshot consistency | Clear missing optional state, battery, and setting values instead of retaining old values |
| Expiry | Reschedule an early callback until the sample's actual age limit |
| Device values | Keep unrecognized alarm states unknown and reject humidity outside 0–100% |

Bluetooth discovery names, service UUIDs, and addresses are identification hints,
not cryptographic device authentication. A compatible advertisement is not proof
of a trusted device. Device firmware security and authentication are outside this
source review's scope. Logs may contain Bluetooth addresses; redact addresses,
personal names, and local network details before sharing diagnostics.

## Automated regression tests

From the repository root, with Python 3.12 or newer:

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
```

**76 tests passed locally on Python `3.12.14`**, using the real
`voluptuous==0.15.2` dependency pinned in `requirements-test.txt`. The behavioral
suite exercises the real integration code while mocking Home Assistant,
Bluetooth, and timing boundaries. It covers:

- Poll/read/disconnect behavior, cooldown, retries, session errors, setting
  writes, cancellation, setup cleanup, and shutdown.
- Cached sample age, expiry and recovery, missing optional values, malformed
  readings, and unknown device states.
- Manual and discovered setup choices, address validation, and duplicate-entry
  handling.
- Multiple entries with independent entity IDs, caches, expiry timers, and
  polling options.

The suite tests integration behavior, not the full Home Assistant runtime or a
physical Bluetooth radio. In particular, passing multi-entry tests does not prove
simultaneous operation of several monitors on a real adapter. Consult each
commit's test results separately; this document does not assert that a remote
CI run has passed. The [test workflow](../.github/workflows/tests.yml) is configured
for Python `3.12`, `3.13`, and `3.14`, using pinned official actions, read-only
permissions, and a five-minute job timeout. The local result above establishes
only the tested Python `3.12.14` run.

## Hardware checks already performed

The earlier polling patch was checked with **one Birdie Pro**, firmware `1.6.6`,
native Bluetooth, and Home Assistant `2026.8.1`:

- The Birdie iOS app connected directly and updated its readings while HA left
  the Bluetooth connection free.
- HA resumed scheduled readings after the phone app was fully closed.
- Two successive automatic polls read a snapshot and disconnected, leaving
  approximately three minutes between sessions.
- Home Assistant mean helpers used the thermostat alone when Birdie was
  unavailable and returned to combined readings after Birdie recovered.

These observations establish the earlier polling approach on that setup. They
do **not** validate every change in `v0.1.6` on hardware. The one-hour expiry was
tested with mocked time, not by withholding the physical monitor for an hour.
The README screenshots also show the earlier live installation.

## Compatibility limits

The upstream integration includes Birdie Pro firmware `1.6.0` and `1.6.1` UUID
mappings and native/active-proxy connection paths, which this fork retains.
That is distinct from this fork's own live test coverage.

Other Birdie models and firmware must expose compatible temperature, humidity,
and CO₂ GATT characteristics for a complete snapshot. This fork does not claim
universal Birdie compatibility. Physical multi-device operation, ESPHome proxy
operation with the new changes, and arbitrary firmware combinations still need
device-specific verification.

HA and the phone take turns using Bluetooth. The fork cannot guarantee that a
phone will release its connection, that every connection attempt will succeed,
or that an adapter can support a particular number of simultaneous devices.
