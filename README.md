# Ninebot Scooter — Home Assistant integration

A local, Bluetooth-LE integration for **Segway / Ninebot** kick-scooters. No
cloud account, no MQTT, no bridge — Home Assistant talks to the scooter directly
over BLE (through the local adapter or any connectable ESPHome Bluetooth proxy).

Both protocol generations are implemented: the classic one used by the MAX G30 and
its relatives, and the newer AES-encrypted one used by models such as the Max G3.
See [Model support](#model-support) for what is confirmed on real hardware, and
[the account lock](#newer-models-and-the-account-lock) if a newer scooter refuses
to pair.

> **Status: beta.** Sensors and controls are confirmed working on a **MAX G30D**.
> Started as a packaged, HACS-installable fork of
> [ownbee/ninebot-integration](https://github.com/ownbee/ninebot-integration) +
> [ownbee/ninebot-ble](https://github.com/ownbee/ninebot-ble), with the protocol
> library and its `miauth` crypto **vendored in** so nothing is pip-installed at
> runtime.

## What you get

Sensors, polled while the scooter is awake and in range:

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

| Model | Protocol | Status |
|---|---|---|
| MAX G30 / G30D | legacy | ✅ Confirmed working — sensors + controls |
| E / ES / F series | legacy | Likely to work — untested |
| **Max G3 / G3 Plus** | Encryption2 | ⚠️ Protocol confirmed working on hardware, but blocked if the scooter is bound to a Segway account — see below |
| G2, F2, E-series | Encryption2 | Untested — reports welcome |

**Works on every model, whatever the protocol:** presence (**In range**),
**Signal strength** and **Last seen**. These come from the Bluetooth
advertisement, so they need no connection, no pairing and no supported protocol —
useful on their own for knowing when the scooter arrives, leaves, or is moved.

Newer vehicles speak a different, AES-encrypted protocol. Confusingly they expose
Segway's own GATT service `6e400001-0000-0000-006e-696e65626f74` ("ninebot" in
ASCII) *and* the classic Nordic UART service — and which one they actually answer
on varies by model (a Max G3 answers on the classic one). The integration works
this out on the first connection, remembers it, and builds the matching entities.
Writes (controls) are implemented for the legacy protocol only.

**If your model doesn't work, the most useful thing you can send** is the
integration's **Download diagnostics** file, attached to an issue. It records what
the scooter advertises, which GATT services it exposes, and why the last attempt
failed — enough to tell whether the protocol can apply. No coding needed.

## Newer models and the account lock

A Max G3 has been confirmed to complete the full encrypted handshake with this
integration: it answers, its replies decrypt correctly, and its serial number
reads back. **But if the scooter has ever been paired with the official
Segway-Ninebot app, that is usually where it stops.**

The vehicle stores a 16-byte pairing password internally and will not register a
second client while it holds one. It reports `stored password: True`, ignores the
pairing request, and no amount of pressing the power button changes that.

**Things that do *not* clear it** (all tested on a Max G3):

- Unlinking the vehicle in the app ("entkoppeln")
- Deleting the Bluetooth bond on the phone
- Clearing the Segway app's storage and cache
- A button-combo factory reset on the scooter itself
- Registering the scooter to a **different Segway account**

The password appears to live in the vehicle's controller and on Segway's servers,
so an owner cannot remove it. A scooter that has **never** been paired with the
app is unaffected — it will pair with Home Assistant normally after one press of
the power button.

### Getting in anyway: reuse the app's password

The integration has a **Pairing password** option (under *Configure*). Given the
password the app itself uses, it skips pairing entirely and authenticates
directly. Two ways to obtain it, both requiring access to your own devices:

**From the app's local data** — the app keeps its own copy, keyed by serial:

```bash
python3 tools/recover_password_from_app.py APPDATA --capture btsnoop_hci.log --name <SERIAL>
```

`APPDATA` can be an iOS `com.ninebot.segway.plist` (key `<SERIAL>_decrypt`), an
Android shared-prefs XML or database, or an `adb backup` file. Every 16-byte value
found is **verified against the AUTH handshake** in a Bluetooth capture of the same
scooter, so the password it prints is proven correct before you use it. Paste the
result into the **Pairing password** option.

- **iOS is the easy route:** a normal *unencrypted* Finder/iTunes backup contains
  the plist. No jailbreak needed.
- **On Android, `adb backup` does not work** — Segway ships the app with
  `allowBackup=false`, so the backup comes out empty (a 1 KB file of zeros). Root
  access is needed to read `/data/data/com.ninebot.segway/shared_prefs/`.

**From a first-time pairing capture** — if the scooter has never been paired, or
you are pairing a fresh one, record it with Android's Bluetooth HCI snoop log and:

```bash
python3 tools/extract_pairing_password.py btsnoop_hci.log --name <SERIAL>
```

This reads the password straight out of the exchange. It only works during a
genuine first pairing: a reconnect never transmits the password. Add `--all` to
print every decrypted frame, which is also the easiest way to see which boards and
registers a model really uses.

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

On first connect the scooter registers Home Assistant as an authorised client.
When Home Assistant asks — a notification appears in the UI — **press the power
button on the scooter once** to confirm. The key is generated locally and then
stored, so this is a one-time step; no cloud login is involved.

> Notes:
> - Ninebot scooters remember a single paired client. Pairing Home Assistant may
>   require you to re-pair the official Segway-Ninebot app afterwards, and vice
>   versa.
> - On **newer models already paired with the app**, the button press cannot work
>   at all — see [the account lock](#newer-models-and-the-account-lock).

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
