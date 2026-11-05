# Rotel Control — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/default)
[![Version](https://img.shields.io/github/v/release/drtsb/ha-rotel-integration?color=blue)](https://github.com/drtsb/ha-rotel-integration/releases)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-blue)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Custom Home Assistant integration for Rotel amplifiers and AV processors that expose the ASCII control interface over TCP (default port `9590`, also supports `9500`/`9600`).

---

## 🎧 Supported Models

> **Note:** Hardware testing was conducted on the **Rotel RA-1572**. Other device profiles are implemented based on official Rotel ASCII protocol specifications.

| Profile | Model | Status | Inputs | Record source |
| --- | --- | --- | --- | --- |
| `ra1572` | Rotel RA-1572 | ✅ Tested (Hardware) | CD, Tuner, Balanced, Optical Coax 1/2, Line 1/2, Phono | – |
| `ra1572mkii` | Rotel RA-1572 MkII | 📋 Protocol Spec | as RA-1572 plus PC USB and Bluetooth | – |
| `ra1200` | Rotel RA-1200 MkII | 📋 Protocol Spec | as RA-1572 plus PC USB and Bluetooth | – |
| `rcx1570` | Rotel RCX-1570 MkII | 📋 Protocol Spec | CD, Tuner, Coax, Optical, Balanced 1/2, Line, Phono, PC USB, Bluetooth | yes |
| `rcx1500` | Rotel RCX-1500 | 📋 Protocol Spec | HDMI 1–4, Coax, Optical, Balanced 1/2, Line, CD, Tuner, Phono, Bluetooth | yes |
| `rbx1500` | Rotel RBX-1500 | 📋 Protocol Spec | HDMI 1–4, Coax, Optical, Balanced 1–4, Line, CD, Tuner, Bluetooth | – |
| `rca10` | Rotel RCA-10 | 📋 Protocol Spec | HDMI 1/2, Optical, Coax, Balanced, Line, Phono, PC USB, Bluetooth | – |
| `generic` | Unknown Rotel | 📋 Protocol Spec | CD, Tuner, Phono, Coax, Optical, Aux, Balanced, USB, PC USB, Bluetooth | – |

Every input comes from the central catalogue in `custom_components/rotel_control/const.py`. You can trim down the list of inputs for your unit via the integration options flow.

---

## ✨ Features

* **Media Player**:
  * Power control with Rotel power interlock.
  * Direct 1:1 volume scale mapping (`0…96` on the device front panel mapped to Home Assistant's `0…100%` slider).
  * Mute and input source selection.
* **Audio & Tone Controls (`number` & `switch`)**:
  * **Bass** and **Treble** (`±10 dB` in 1 dB steps).
  * **Balance** (`-15…+15`).
  * **Tone Bypass**: toggle to bypass tone controls. Sliders automatically become unavailable when bypassed to prevent unsaved changes.
* **Speaker Output Selection (`switch`)**:
  * Independent **Speakers A** and **Speakers B** controls using explicit discrete commands.
* **Display & Extras (`select` & `sensor`)**:
  * **Display Dimmer** brightness selection (`dimmer_0` to `dimmer_6`).
  * **Record Source** selection (for supported RCX processors).
  * Diagnostic sensors for connection health, detected model, and firmware version.
* **Asynchronous TCP Push & Instant Updates**:
  * Push-based connection updates state instantly when changed at the front panel without polling lag.
  * Respects device Standby and Auto Power Down (APD) modes (no aggressive keep-alive polling).
* **Auto-Discovery**:
  * SSDP and Zeroconf discovery automatically detect Rotel devices on your local network.
* **Automations & Events**:
  * Every reported status change fires a native `rotel_control_command_received` event for Home Assistant automations.

---

## 📦 Installation

### HACS (Recommended)

1. Open **HACS** in Home Assistant.
2. Click the three dots in the top-right corner and select **Custom repositories**.
3. Add `https://github.com/drtsb/ha-rotel-integration` with Category **Integration**.
4. Click **Download**.
5. Restart Home Assistant.
6. Go to **Settings → Devices & services → Add integration → Rotel Control**.

### Manual Installation

Copy the `custom_components/rotel_control` directory into your Home Assistant `<config>/custom_components/` directory and restart Home Assistant.

---

## 🎨 Custom Lovelace Card

A dedicated Hi-Fi style Lovelace card for Rotel amplifiers is available in a separate repository:

👉 **[ha-rotel-card on GitHub](https://github.com/drtsb/ha-rotel-card)**

```yaml
type: custom:rotel-amplifier-card
entity: media_player.rotel
```

---

## ⚙️ Configuration & Options

When adding the integration or configuring it later via **Settings → Devices & Services → Rotel Control → Configure**:

| Field | Default | Description |
| --- | --- | --- |
| Host | – | IP address or hostname of the amplifier |
| Port | `9590` | TCP control port (`9590`, `9600`, or `9500` depending on model) |
| Model | `ra1572` | Profile defining inputs and volume behavior (auto-detected if device responds to `model?`) |
| Name | host | Device name in Home Assistant |

### Integration Options

* **Polling Interval**: Fallback check frequency (default `2` s, range 1–300 s).
* **Inputs**: Customize and filter available input sources.
* **Instant Updates**: Process push updates from the amplifier and fire `rotel_control_command_received` events.

---

## ⚡ Automations & Events

Every state update received from the amplifier fires `rotel_control_command_received` on the Home Assistant event bus:

```yaml
automation:
  - alias: "Rotel: pause playback when amplifier turns off"
    triggers:
      - trigger: event
        event_type: rotel_control_command_received
        event_data:
          changes:
            power:
              new: false
    actions:
      - action: media_player.media_pause
        target:
          entity_id: media_player.living_room_speaker
```

### Event Payload Data

| Key | Description |
| --- | --- |
| `changes` | Dictionary of changed attributes with `old` and `new` values |
| `origin` | `push` (reported by amplifier) or `poll` (detected during status check) |
| `device_id` | Home Assistant device registry ID |
| `entry_id`, `host`, `port` | Connection identifier details |

---

## 🛠️ Development & Testing

For contributors looking to run linting and tests locally:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements_test.txt

# Run linter and type checks
ruff check custom_components tests
mypy custom_components/rotel_control

# Run test suite
pytest -q
```

---

## 📄 License

This project is licensed under the MIT License — see [LICENSE](LICENSE) for details.
