# Changelog

## 0.1.6

Reliability and validation follow-up to the Bluetooth polling fork. Polling
defaults and existing entity IDs are unchanged.

- Reject malformed manual Bluetooth addresses and prevent duplicate entries
  when setup flows compete. Show addresses in discovery choices and allow manual
  addresses alongside discovered devices.
- Handle Bluetooth errors that occur before connecting and malformed setting
  write/readback values through the integration's normal error paths.
- Run bounded disconnect cleanup when a session is cancelled, and clean up
  polling and discovery resources if integration setup fails.
- Keep stopped devices unavailable if a queued setting write fails after shutdown.
- Clear missing optional state, battery, and setting values in a new snapshot.
  Report unknown alarm state for unrecognized Birdie states, and reject humidity
  readings outside 0–100%.
- Reschedule an expiry callback that fires early so a cached reading still
  becomes unavailable at its actual age limit.
- Extend the behavioral suite for setup, error handling, lifecycle cleanup,
  sensor values, and separate caches/timers/options for multiple entries.
- Document pairing troubleshooting and the limits of source review, mocked
  tests, and the previous hardware checks in [the validation record](docs/validation.md).

## 0.1.5

- Publish the polling fork with configurable poll interval, Bluetooth cooldown,
  retry delay, session timeout, and maximum sample age.
- Release the Bluetooth connection between reads so HA and the Birdie phone app
  can take turns. Preserve recent readings through failed polls and mark them
  unavailable at expiry.
- Document separate Home Assistant mean helpers for thermostat fallback, add
  screenshots, and retain upstream attribution and the MIT license.

Based on [dellzhui/ha-birdie v0.1.3](https://github.com/dellzhui/ha-birdie/tree/v0.1.3).
