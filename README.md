# Ninebot Scooter — Home Assistant integration

A local, Bluetooth-LE integration for **Segway / Ninebot** kick-scooters
(Max G30 / G30D, ES, E, F series). No cloud account, no MQTT, no bridge — Home
Assistant talks to the scooter directly over BLE (through the local adapter or
any connectable ESPHome Bluetooth proxy).

> **Status: beta.** Built as a packaged, HACS-installable, self-contained fork of
> [ownbee/ninebot-integration](https://github.com/ownbee/ninebot-integration) +
> [ownbee/ninebot-ble](https://github.com/ownbee/ninebot-ble), with the protocol
> library and its `miauth` crypto **vendored in** so nothing is pip-installed at
> runtime. The upstream author only verified the **F-series**; **G30/G30D support
> is expected but not yet confirmed on real hardware** — testing welcome.

## What you get

Read-only sensors, polled while the scooter is awake and in range:

- **Battery**: charge %, voltage, current, two temperatures, health, capacity,
  remaining mAh, BMS serial & firmware
- **Ride data**: total mileage, trip mileage, remaining & predicted range,
  average speed, total/riding operation time, power (W)
- **Controller**: body temperature, supply voltage, firmware versions (ESC / BLE),
  error & alarm codes
- **State**: lock, speed-limit, buzzer/alarm, activated, external battery present,
  operating mode (Normal/Eco/Sport), KERS level, cruise control, tail light,
  configured speed limits

### Controls (experimental, since v0.2.0)

Writable entities:

- **Ride mode** (select): Normal / Eco / Sport
- **Recuperation / KERS** (select): Off / Medium / Strong
- **Cruise control** (switch)
- **Tail light** (switch)

> ⚠️ **Writes are experimental.** The write frame is community-derived and not
> yet fully verified across firmwares — treat the controls as best-effort and
> check that the value actually changes. Lock/unlock and speed-limit controls are
> intentionally **not** shipped yet: a wrong speed-limit write is dangerous, so
> they follow once the write path is confirmed on real hardware.

The **polling interval** (default 30 s) is configurable under the integration's
**Configure** button.

## Requirements

- Home Assistant **2024.12** or newer with the Bluetooth integration set up
  (USB adapter passed through, or an ESPHome proxy with `active: true`).
- The scooter must be **awake** and within Bluetooth range when polled.
- Firmware new enough to speak the encrypted (miauth) protocol — very old
  firmwares may not work.

## Installation (HACS)

Until this is in the default HACS store, add it as a **custom repository**:

1. HACS → three-dot menu → **Custom repositories**
2. Repository: `https://github.com/BobMcGlobus/ha-ninebot`, category **Integration**
3. Install **Ninebot Scooter**, then restart Home Assistant.

The scooter is auto-discovered via its Bluetooth advertisement. Go to
**Settings → Devices & Services**, you should see a discovered *Ninebot* device —
or add it manually via **Add Integration → Ninebot Scooter**.

### First-time pairing

On first connect the scooter registers Home Assistant as an authorized device.
When Home Assistant asks, **press the power button on the scooter once** to
confirm. No key extraction and no cloud login are required — the app-side key is
generated locally per session.

> Note: Ninebot scooters typically remember a single paired controller key.
> Depending on firmware, pairing Home Assistant may re-register the scooter and
> require you to re-pair the official Segway-Ninebot app afterwards (and vice
> versa).

## Credits

- [ownbee/ninebot-ble](https://github.com/ownbee/ninebot-ble) (MIT) — protocol
  library, vendored in `custom_components/ninebot_scooter/ninebot_ble/`.
- [dnandha/miauth](https://github.com/dnandha/miauth) (AGPL-3.0) — Ninebot crypto,
  vendored in `.../ninebot_ble/_miauth/`; itself a port of
  [scooterhacking/NinebotCrypto](https://github.com/scooterhacking/NinebotCrypto).

## License

Because this project bundles AGPL-3.0 code (`miauth`), the whole project is
distributed under the **GNU AGPL-3.0**. See [LICENSE](LICENSE) and
[NOTICE](NOTICE).

This project is not affiliated with, endorsed by, or supported by Segway or
Ninebot. Use at your own risk.
