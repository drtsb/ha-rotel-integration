# Rotel Amplifier

Custom Home Assistant integration for Rotel amplifiers and processors that
expose an ASCII control interface over TCP (port `9500` by default) or RS-232.

Supported out of the box (profiles can be extended, see below):

| Profile | Model | Inputs | Record source |
| --- | --- | --- | --- |
| `ra1572` | Rotel RA-1572 | CD, Tuner, Balanced coax 1/2, Optical coax 1/2, Line 1/2, Phono | – |
| `ra1572mkii` | Rotel RA-1572 MkII | CD, Tuner, Balanced coax 1/2, Optical coax 1/2, Line 1/2, Phono | – |
| `ra1200` | Rotel RA-1200 | CD, Tuner, Aux 1/2, Coax 1/2, Optical 1/2, Phono, Balanced | – |
| `rcx1570` | Rotel RCX-1570 | CD, Tuner, Aux, Coax, Optical, Balanced, Phono, Bluetooth | yes |
| `rcx1500` | Rotel RCX-1500 | HDMI 1–4, Coax, Optical, Balanced, Line, CD, Tuner, Phono | yes |
| `rbx1500` | Rotel RBX-1500 | HDMI 1–4, Coax, Optical, Balanced 1–4, Line 1/2 | – |
| `rca10` | Rotel RCA-10 | HDMI 1/2, Optical, Coax, Balanced, Line, Phono | – |
| `generic` | Unknown Rotel | common Rotel naming | – |

## Features

* **Media player**: power (with the Rotel power interlock), volume, mute,
  input selection.
* **Number**: precise volume in dB (0.5 dB steps) next to the 0–100 % slider.
* **Select**: input selection (and record source for RCX models, disabled by
  default because only pre-out capable setups need it).
* **Sensors**: control-connection health, device model (both diagnostic), and
  the firmware version (disabled by default).
* **Config flow**: host/port entry with live verification of the protocol,
  model detection via `MODEL_QUERY`, reauth, reconfigure and an options flow.
* **Discovery**: SSDP (`manufacturer: Rotel`) and zeroconf
  (`_rotel._tcp.local.` and friends) when the unit advertises itself; manual
  entry always works.
* **Diagnostics**: download of the profile, connection state and the last
  snapshot for bug reports.

## Installation

### HACS (recommended)

1. Add this repository to HACS as a *custom repository* of type
   **Integration**.
2. Install **Rotel Amplifier**.
3. Restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration → Rotel
   Amplifier**.

### Manual

Copy `custom_components/rotel_control` into the `custom_components`
directory of your Home Assistant configuration and restart.

## Configuration

| Field | Default | Description |
| --- | --- | --- |
| Host | – | IP address or hostname of the amplifier |
| Port | `9500` | TCP port of the Rotel control interface |
| Model | `ra1572` | Profile that defines the inputs and the volume range |
| Name | host | Device name shown in Home Assistant |

Options (gear icon on the integration card):

| Field | Default | Description |
| --- | --- | --- |
| Polling interval | `2` s | How often the state is polled (1–300 s) |
| Model | as configured | Switch profile without re-adding the device |

The control protocol does **not** push state changes, so the integration polls
the amplifier. Rotel units are polled at 2 s by default; raise the interval if
you notice traffic on a large network.

## Protocol notes

Rotel speaks a line based ASCII protocol:

```
>>> VOLUME_QUERY
<<< VOLUME 95        # 95 steps of 0.5 dB above -60 dB  ->  -12.5 dB
>>> POWER on
>>> MUTE on
>>> SOURCE TUNER
```

Notes on the implementation:

* every command is a single `\r\n` terminated ASCII line;
* queries are answered with `COMMAND value`, write commands normally return
  nothing, so late answers are drained before the next query;
* a single mutex serialises the socket because the device exposes one UART;
* `MODEL_QUERY`/`FIRMWARE_QUERY` are optional — unsupported queries are
  skipped instead of failing the poll cycle;
* after power-on the last known volume is re-sent, because Rotel keeps the
  pre-out relays open until a volume has been applied (the "no sound after
  standby" effect).

### Adapting a profile

The volume encoding is the only part that is model specific. Profiles live in
`custom_components/rotel_control/protocol.py` and use:

* `VolumeScale.STEPS` (default) – the payload is the number of `volume_step_db`
  steps above `volume_min_db`. Lossless: `-60…+20 dB` with `0.5 dB` steps is
  `0…160`.
* `VolumeScale.PERCENT` – the payload is `0…100` percent of the range. Use
  this only for firmware that reports a percentage; note that a percent
  payload cannot express every 0.5 dB step.

If the volume behaves incorrectly, the client logs a warning naming the
profile and the expected range — adjust `volume_scale`, `volume_min_db`,
`volume_max_db`, `volume_step_db` and the `inputs` of the profile.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements_test.txt
ruff check custom_components tests
mypy custom_components/rotel_control
pytest -q
```

The test suite has three layers:

* `tests/test_protocol.py` — the wire format, no Home Assistant involved;
* `tests/test_api.py` — the asyncio TCP client against a fake amplifier that
  listens on loopback;
* `tests/test_init.py` — the config flow, setup, entities and services inside a
  real Home Assistant instance (`pytest-homeassistant-custom-component`).

`protocol.py` has no Home Assistant imports, so the wire format can be tested
without a running Home Assistant:

```bash
pytest tests/test_protocol.py -q
```

## Known limitations

* Only the ASCII control protocol is implemented; the Rotel "USB/HDMI-CEC"
  control path and the HEOS/RTL (RCX-1500 "Rotel Home Theater") protocols are
  not.
* Zone 2 of two-channel models is not exposed as a separate entity.
* Display brightness, bass/treble and the RCX "record" routing of the RCX-1570
  beyond `REC_SELECT` are not exposed yet.

## License

MIT — see [LICENSE](LICENSE).