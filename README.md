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

### Controls

Writing has been confirmed on a G30D (the scooter acknowledges the write and
reports the new value back). Every control reads the register back after writing
and raises an error if the scooter did not accept the change.

- **Lock** — the scooter's built-in lock
- **Ride mode** (select): Normal / Eco / Sport
- **Recuperation / KERS** (select): Off / Medium / Strong
- **Cruise control** (switch)
- **Tail light** (switch)

Locking flips a single bit of a packed status word, leaving the other flags in it
untouched. Some firmwares may only permit this from the official app — you'll get
an explicit error rather than a silent no-op.

The **polling interval** (default 30 s) is configurable under the integration's
**Configure** button.

### Normal mode speed limit (disabled by default)

`number.<scooter>_normal_mode_speed_limit` writes the scooter's normal-mode speed
limit register.

> **This does not unlock a higher top speed.** On a G30D the scooter accepts the
> write and reports the new value, but the speed actually ridden does not change —
> the model's cap is enforced elsewhere. The register is exposed for
> experimentation, not as a working "unlock".

> ⚠️ **Read before enabling.** Raising the limit beyond the speed your model is
> homologated for (20 km/h for a German G30D) voids its type approval — and with
> it your insurance cover — on public roads. In Germany riding uninsured is a
> criminal offence, not a fine. The scooter's brakes, frame and lights were
> certified for the original speed. Whether you may use this on private land, on
> public roads, or at all, is your responsibility and depends on where you are.

Safeguards: the entity is disabled by default, values are clamped to
6–30 km/h, and the integration refuses to write at all if it cannot make sense of
the register the scooter reports (encoding differs between firmwares). Every write
is read back and an error is raised if the scooter did not accept it.

## Model support

> ⚠️ **Only the Ninebot MAX G30 / G30D is confirmed working.** Everything else is
> **under active development and will probably not work yet.** If you have another
> model, reports are very welcome — please attach the diagnostics download.

| Model | Protocol | Status |
|---|---|---|
| MAX G30 / G30D | legacy | ✅ Confirmed working (sensors + controls) |
| E / ES / F series | legacy | Likely to work — untested |
| **Max G3, G2, F2, E-series** | Encryption2 | 🚧 In development — sensors only, **untested on hardware** |

Newer vehicles speak a different, AES-encrypted protocol over their own GATT
service `6e400001-0000-0000-006e-696e65626f74` ("ninebot" in ASCII). Confusingly
they *also* advertise the classic Nordic UART service used by older models, but
never answer on it. The integration detects which protocol a vehicle speaks on the
first connection, remembers it, and builds the matching entities. Writing
(controls) is implemented for the legacy protocol only.

Pairing a newer vehicle may require pressing its power button once while Home
Assistant connects; the session key is then stored and reused.

**If your model doesn't work, this is the most useful thing you can send:**
open the integration, use **Download diagnostics**, and attach the file to an
issue. It contains what the scooter advertises and which GATT services it exposes
— that alone shows whether the existing protocol can apply. No coding needed.

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
