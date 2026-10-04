# Rotel Amplifier

Custom Home Assistant integration for Rotel amplifiers and processors that
expose the ASCII control interface over TCP (port `9590`).

Supported out of the box (profiles can be extended, see below):

| Profile | Model | Inputs | Record source |
| --- | --- | --- | --- |
| `ra1572` | Rotel RA-1572 | CD, Tuner, Balanced coax 1/2, Optical coax 1/2, Line 1/2, Phono | – |
| `ra1572mkii` | Rotel RA-1572 MkII | as RA-1572 plus PC USB and Bluetooth | – |
| `ra1200` | Rotel RA-1200 MkII | as RA-1572 plus PC USB and Bluetooth | – |
| `rcx1570` | Rotel RCX-1570 MkII | CD, Tuner, Coax, Optical, Balanced, Line, Phono, PC USB, Bluetooth | yes |
| `rcx1500` | Rotel RCX-1500 | HDMI 1–4, Coax, Optical, Balanced 1/2, Line, CD, Tuner, Phono, Bluetooth | yes |
| `rbx1500` | Rotel RBX-1500 | HDMI 1–4, Coax, Optical, Balanced 1–4, Line, CD, Tuner, Bluetooth | – |
| `rca10` | Rotel RCA-10 | HDMI 1/2, Optical, Coax, Balanced, Line, Phono, PC USB, Bluetooth | – |
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
  model detection via `model?`, reauth, reconfigure and an options flow. A
  failed attempt keeps everything you typed.
* **Discovery**: SSDP (`manufacturer: Rotel`) and zeroconf; the announcement
  is verified against the protocol and the answering port is pre-filled.
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

Set **POWER OPTION = Quick** in the amplifier setup menu, otherwise it does not
answer on the network while in standby. With the default *Standby* setting a
running amplifier still works, but waking it from Home Assistant does not.

| Field | Default | Description |
| --- | --- | --- |
| Host | – | IP address or hostname of the amplifier |
| Port | `9590` | TCP port of the Rotel control interface (9600/9500 on older units) |
| Model | `ra1572` | Profile that defines the inputs and the volume range |
| Name | host | Device name shown in Home Assistant |

The model reported by the amplifier itself (`model?`) always wins over the
selected profile, because only the amplifier knows its exact input list.

Options (gear icon on the integration card):

| Field | Default | Description |
| --- | --- | --- |
| Polling interval | `2` s | How often the state is polled (1–300 s) |
| Model | as configured | Switch profile without re-adding the device |

The control protocol does **not** push state changes, so the integration polls
the amplifier. Rotel units are polled at 2 s by default; raise the interval if
you notice traffic on a large network.

Three different errors are shown while adding a device: *failed to
connect* (nothing listens on the address), *connected, but the
device closed the connection* (the amplifier is in standby with the
factory default POWER OPTION = Normal, or another controller such as
the Rotel app already holds its single control connection), and
*connected, but no Rotel answer* (the port is open but speaks
something else, e.g. the web interface).

## Protocol notes

Rotel speaks a line based ASCII protocol without CR/LF: queries are the field
name plus `?`, commands end with `!`, and every reply field is terminated
with `$`.

```
>>> power?
<<< power=on$
>>> volume?
<<< volume=42$
>>> tuner!
<<< source=tuner$
>>> power_on!
<<< power=on$
```

Notes on the implementation:

* a poll sends `power?volume?mute?source?` in a single write and frames the
  answer on `$`, so a reply split over several TCP segments is read correctly;
* `model?` and `version?` are asked once per connection and cached — a
  connection that fails is asked again on the next one, and unsupported
  queries are skipped instead of failing the poll cycle;
* the answer of a poll is collected while the device keeps sending, so a slow
  burst is not cut short; a device that sends more than 4 KiB without framing
  it is disconnected instead of being buffered;
* cached values are truncated, so nothing a device reports can grow the state
  attributes or a diagnostics download;
* a single mutex serialises the socket because the device answers in the order
  it receives commands;
* the raw volume of the device is exposed as the `volume_raw` attribute;
* after power-on the last known volume is re-sent, because Rotel keeps the
  pre-out relays open until a volume has been applied (the "no sound after
  standby" effect).

### Volume scales

Rotel units report the volume either as the raw position of the front panel
scale (`0…96` on the A12/A14/RA-1572 family) or directly in decibel (the
RCX/RBX processors). A profile declares which one it is:

* `VolumeScale.STEPS` – the payload is the number of `volume_step_db` steps
  above `volume_min_db`. For a 0…96 device with the default `-60…+20 dB` bounds
  that maps onto `-60…-12 dB`, so the decibel value is a linear scale and not
  the amplifier's own readout; the raw value is available as `volume_raw`.
* `VolumeScale.DB` – the payload is the volume in decibel, which is what the
  processors report (`-80…+20 dB` for the RCX-1500/RBX-1500).
* `VolumeScale.PERCENT` – the payload is 0…100 percent, for firmware that
  reports a percentage.

A negative volume on a `STEPS` profile is read as decibel, and a volume outside
the expected range is logged with a warning naming the profile.

### Adapting a profile

The volume encoding is the only part that is model specific. Profiles live in
`custom_components/rotel_control/protocol.py` and use:

* the command key of every input (`coax1`), plus the values a firmware revision
  may report for it (`aliases`, e.g. `analog_cd` for `cd`) and the labels earlier
  versions exposed (`aux 1` for `aux1`), so existing automations keep resolving;
* `VolumeScale.*`, `volume_min_db`, `volume_max_db` and `volume_step_db`.

The table above is derived from those profiles; `tests/test_init.py` fails when
the two drift apart. If the volume behaves incorrectly, the client logs a warning
naming the profile and the expected range — adjust the volume scale of the
profile.

## Discovery

Rotel amplifiers do not all advertise a Rotel specific service, so discovery
works in two steps:

1. an SSDP announcement (`manufacturer: Rotel`) or a zeroconf service triggers
   the flow;
2. the flow then asks the announced host for `power?` on the announced port
   and on 9590/9600/9500 — all of them at once — and only offers the device
   when one answers. The port that answered is pre-filled in the form.

Only Rotel specific zeroconf services are matched: a generic matcher such as
`_http._tcp.local.` would start (and abort) a flow for every device on the
network.

Units that do not advertise themselves at all have to be added manually.

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
  listens on loopback and speaks the real protocol;
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
* The record source (`record_source?` / `rec_<input>!`) follows the RCX-1570
  documentation; a processor whose firmware names it differently reports the
  record source as unsupported.
* Zone 2 of two-channel models is not exposed as a separate entity.
* Display brightness, bass/treble and tone bypass are not exposed yet.

## License

MIT — see [LICENSE](LICENSE).