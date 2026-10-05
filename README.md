# Rotel Amplifier

Custom Home Assistant integration for Rotel amplifiers and processors that
expose the ASCII control interface over TCP (port `9590`).

Supported out of the box (profiles can be extended, see below):

| Profile | Model | Inputs | Record source |
| --- | --- | --- | --- |
| `ra1572` | Rotel RA-1572 | CD, Tuner, Balanced, Optical Coax 1/2, Line 1/2, Phono | – |
| `ra1572mkii` | Rotel RA-1572 MkII | as RA-1572 plus PC USB and Bluetooth | – |
| `ra1200` | Rotel RA-1200 MkII | as RA-1572 plus PC USB and Bluetooth | – |
| `rcx1570` | Rotel RCX-1570 MkII | CD, Tuner, Coax, Optical, Balanced 1/2, Line, Phono, PC USB, Bluetooth | yes |
| `rcx1500` | Rotel RCX-1500 | HDMI 1–4, Coax, Optical, Balanced 1/2, Line, CD, Tuner, Phono, Bluetooth | yes |
| `rbx1500` | Rotel RBX-1500 | HDMI 1–4, Coax, Optical, Balanced 1–4, Line, CD, Tuner, Bluetooth | – |
| `rca10` | Rotel RCA-10 | HDMI 1/2, Optical, Coax, Balanced, Line, Phono, PC USB, Bluetooth | – |
| `generic` | Unknown Rotel | CD, Tuner, Phono, Coax, Optical, Aux, Balanced, USB, PC USB, Bluetooth | – |

Every input of every profile comes from the catalogue in
`custom_components/rotel_control/const.py`, so a name means the same thing on
every device — and the **Inputs** option trims the list down to what your unit
actually has (see below).

## Features

* **Media player**: power (with the Rotel power interlock), volume, mute,
  input selection. The volume is the single volume control of this
  integration and it runs on the scale of the amplifier itself — the raw
  `0…96` of the front panel, mapped onto the 0…100 % slider Home Assistant
  shows so that the number you set is the number the amplifier displays.
* **Number**: **Bass** and **Treble** (±10 dB, 1 dB steps) and **Balance**
  (−15…+15, left is negative). There is deliberately no volume entity: a
  volume in dB next to the volume slider would be a second control for one
  setting.
* **Select**: input selection, **display brightness** (`dimmer_0!` is the
  brightest, `dimmer_6!` the dimmest) and, for RCX models, the record source
  (disabled by default because only pre-out capable setups need it).
* **Switch**: **Speakers A** and **Speakers B** (`speaker_a_on!` and friends,
  never the toggling form, so no race with a change made on the device), and
  **Tone bypass**.
* **Sensors**: control-connection health, device model (both diagnostic), and
  the firmware version (disabled by default).
* **Config flow**: host/port entry with live verification of the protocol,
  model detection via `model?`, reauth, reconfigure and an options flow. A
  failed attempt keeps everything you typed.
* **Discovery**: SSDP (`manufacturer: Rotel`) and zeroconf; the announcement
  is verified against the protocol and the answering port is pre-filled.
* **Instant updates**: a unit with *auto update* enabled reports every change
  it makes at the front panel; the entities follow immediately instead of
  waiting for the next poll.
* **Events**: every change the amplifier reports fires
  `rotel_control_command_received`, ready to be used in automations (see
  [Events](#events)).
* **Diagnostics**: download of the profile, connection state and the last
  snapshot for bug reports.
* **Logo**: `custom_components/rotel_control/brand/` carries the icon and the
  logo, so the setup dialog and the integration page are not blank. See
  [Brand assets](#brand-assets).

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
| Inputs | the ones of the model | Keep only the inputs your amplifier has |
| Instant updates from the device | on | Apply what the amplifier reports on its own and fire `rotel_control_command_received` |

**Inputs** is the escape hatch for a unit whose front panel differs from its
profile: it takes the protocol values of the catalogue (`coax1`, `bal_xlr`, …),
accepts the labels shown in the interface, and the selection is what the media
player and the input select then offer. Leave it alone to use the profile as
it is. The integrated amplifiers have a *single* balanced input, reported by
the hardware as `bal_xlr`; profiles for processors with several XLR inputs
(`bal_xlr1`…`bal_xlr4`) keep the numbering.

A task keeps reading the control connection for what the amplifier reports on
its own, and the poll is the safety net for the many units that only answer
questions. Rotel units are polled at 2 s by default; raise the interval if you
notice traffic on a large network, or turn **Instant updates from the device**
off to rely on polling alone.

Three different errors are shown while adding a device: *failed to
connect* (nothing listens on the address), *connected, but the
device closed the connection* (the amplifier is in standby with the
factory default POWER OPTION = Normal, or another controller such as
the Rotel app already holds its single control connection), and
*connected, but no Rotel answer* (the port is open but speaks
something else, e.g. the web interface).

### Events

Every change the integration notices — pushed by the amplifier or found by the
poll — fires `rotel_control_command_received` on the Home Assistant bus. A
change Home Assistant caused itself (a service call, an automation, a
dashboard) is applied to the entities but does *not* fire an event, and neither
does a reply that only confirms a command that was just sent.

```yaml
automation:
  - alias: "Rotel: pause the amplifier when the TV goes off"
    triggers:
      - trigger: event
        event_type: rotel_control_command_received
        event_data:
          changes:
            power:
              new: false
    actions:
      - action: media_player.turn_off
        target:
          entity_id: media_player.living_room_amplifier
```

The event data carries:

| Key | Description |
| --- | --- |
| `changes` | `{attribute: {old, new}}` for everything that moved, e.g. `volume_db`, `source`, `mute`, `bass_db`, `speaker_a` |
| `origin` | `push` when the amplifier reported it, `poll` when the next poll noticed it |
| `device_id` | Device registry id of the amplifier, for device triggers |
| `entry_id`, `host`, `port` | Which amplifier reported it |

Values are plain data (`source` is reported as its protocol value, `coax1`),
and a value that is not known yet — or was not known before — is not announced,
so the first snapshot after a restart stays quiet. Automations that should also
react to changes Home Assistant made can use the standard `state_changed` event
of the media player instead.

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
>>> bass_-04!
<<< bass=-04$
>>> balance_l02!
<<< balance=L02$
>>> speaker_b_on!
<<< speaker=a_b$
>>> dimmer_3!
<<< dimmer=3$
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
  it receives commands, and one task per connection owns the read side: a reply
  that nobody was waiting for is an unsolicited report of the amplifier, which
  is applied to the state and announced as an event;
* the answer to a command is not a report. Each command remembers which field
  the device is expected to send back (and for how long), so the confirmation of
  a volume change Home Assistant just made cannot be mistaken for somebody
  turning the knob;
* the raw volume of the device is exposed as the `volume_raw` attribute;
* after power-on the last known volume is re-sent, because Rotel keeps the
  pre-out relays open until a volume has been applied (the "no sound after
  standby" effect).

### Volume scales

Rotel units report the volume either as the raw position of the front panel
scale (`0…96` on the A12/A14/RA-1572 family) or directly in decibel (the
RCX/RBX processors). A profile declares which one it is:

* `VolumeScale.STEPS` – the payload is the position on the front panel scale,
  `vol_<NN>!` with `NN` from `0…96`. That is the only volume scale the protocol
  speaks for these units, and it is what the media player shows: the slider
  carries the **number of the device**, so setting 20 % sends `vol_20!` and the
  amplifier display and the slider always read the same number. Since Home
  Assistant gives a media player 101 steps (0…100 %) and the device has 96
  positions, the part of the slider above 96 has no position to move to and
  stays at the maximum. The decibel bounds of such a profile (`-60…-12 dB` in
  0.5 dB steps by default) are only the internal anchor the rest of the
  integration carries the position in — a linear scale and not the amplifier's
  own readout; the raw value is available as `volume_raw`.
* `VolumeScale.DB` – the payload is the volume in decibel, which is what the
  processors report (`-80…+20 dB` for the RCX-1500/RBX-1500). Such a scale has
  no zero position to count from, so the slider spreads over the decibel range
  instead of over the numbers of the device.
* `VolumeScale.PERCENT` – the payload is 0…100 percent, for firmware that
  reports a percentage.

A profile that reports a raw volume must span exactly 96 positions; a
`RotelModel` whose bounds do not is refused at import time rather than sending
positions the device ignores. A negative volume on a `STEPS` profile is read as
decibel, and a volume outside the expected range is logged with a warning
naming the profile.

### Tone controls

The tone block, the speaker relays and the front display dimmer are polled
together with the rest of the state and only need a query to be answered
once, so they stay in sync without extra traffic per entity:

| Entity | Command | Reply |
| --- | --- | --- |
| Bass / Treble | `bass_000!`, `bass_+05!`, `bass_-10!` (and `treble_`) | `bass=+05$`, `treble=000$` |
| Balance | `balance_000!`, `balance_l15!`, `balance_r03!` | `balance=L15$`, `balance=000$` |
| Tone bypass | `bypass_on!` / `bypass_off!` | `bypass=on$` |
| Speakers A/B | `speaker_a_on!`, `speaker_a_off!`, `speaker_b_on!`, `speaker_b_off!` | `speaker=a$`, `speaker=a_b$`, `speaker=off$` |
| Display brightness | `dimmer_0!` … `dimmer_6!` | `dimmer=3$` |

Values are three digit tokens with a sign (`000`, `+05`, `-10`), so an
exponent-less whole decibel is what the device accepts — and the entities
offer exactly those steps. The balance is reported as `L01`…`L15`/`R01`…`R15`
and is shown as −15…+15 so its slider is centred; both the upper and the lower
case spelling of the sides is understood.

Firmware before the current generation names the tone bypass `tone`
(`tone_on!`, `tone?`). Both queries are asked once, and whichever the device
answers is the one the switch uses afterwards, so no configuration is needed
for either generation. A device that answers neither — or no tone block at all
— leaves the switch unavailable and lists the query in the diagnostics instead
of failing the poll.

The two spellings have **opposite senses**, which Rotel's own command lists
give away (`tone_on!` is listed as the counterpart of `bypass_off!`,
`tone_off!` of `bypass_on!`), so `bypass=on` and `tone=off` are the same state:
the tone block out of the signal path. `protocol.tone_bypass_command()` and
`protocol.tone_bypass_is_bypassed()` follow that, and the switch means "tone
block bypassed" on either firmware.

This matters more than it looks: **Rotel ships these units with the tone block
bypassed at the factory**, and an amplifier that has the block out of the
signal path does not apply a `bass_XX!`/`treble_XX!` command — the setting is
simply dropped and the next poll reports the old level.

The integration cannot tell a dropped command from a rejected one, so it does
two things:

* **while the bypass is known to be on**, the bass and treble numbers are
  marked `unavailable`, so the device card offers no slider that cannot work.
  Home Assistant also refuses a service call on an unavailable entity, so the
  command is not even sent; the balance is a control of its own and stays
  usable. The numbers come back with the level the amplifier reports as soon as
  the bypass is switched off;
* **while it cannot be told** (a device that answers neither `bypass?` nor
  `tone?`), the numbers stay usable — unknown is not the same as bypassed — and
  a tone command the amplifier does not confirm is logged as a warning naming
  the bypass, because nothing else would explain it.

To hear a change, switch the tone block in: `switch.<amp>_tone_bypass` off, or
MENU → TONE BYPASS → OFF on the amplifier (the setting is stored permanently).

A setting is published from the amplifier's own answer to the command
(`RotelApi._async_command` waits up to `COMMAND_CONFIRM_TIMEOUT` for the field
the command changes), and is deliberately **not** polled again straight
afterwards: a poll that runs while the change is still being applied reads the
value from *before* it and would put the old one back into the user interface.
An amplifier that never answers a command keeps the value that was sent and is
corrected by the next scheduled poll, and the reply of a speaker command names
the state of both groups, so switching one follows the other. Power, volume,
mute and the input selectors still force a refresh: a command there can change
more than the field it names, and their answers are not read back.

### Adapting a profile

Profiles live in `custom_components/rotel_control/protocol.py` and are built
from the inputs of `const.py`:

* add a new input to `const.py` once (`RotelInput("coax3", "Coax 3", …)`) and
  every profile that lists it, the input selector of the options flow and the
  resolution of reported values all know about it;
* build the input list of a profile from those constants, and use
  `dataclasses.replace` (or `_line()`/`_balanced()`/`_hdmi()`) for the inputs a
  particular unit numbers differently;
* `VolumeScale.*`, `volume_min_db`, `volume_max_db` and `volume_step_db` decide
  how a volume is encoded; a `VolumeScale.STEPS` profile must span exactly
  `VOLUME_UNITS` (96) positions, which is what the default bounds of
  `-60…-12 dB` in 0.5 dB steps do;
* `tone_control`, `tone_bypass`, `speaker_groups` and `dimmer` declare which
  extra entities a profile gets, and `RotelModel.queries` derives the queries
  that are asked from them — a profile can never ask for a control it does not
  have.

Every `RotelInput` also carries the values a firmware revision may report for
it (`aliases`, e.g. `analog_cd` for `cd`, `aux1` for `aux`, `bal_xlr1` for
`bal_xlr`), so existing automations and unusual firmware keep resolving.

The table above is derived from those profiles; `tests/test_init.py` fails when
the two drift apart. If the volume behaves incorrectly, the client logs a warning
naming the profile and the expected range — adjust the volume scale of the
profile. A unit whose input list is simply different needs no code at all: set
**Inputs** in the options flow.

## Brand assets

Home Assistant 2026.3 and newer serve the images of a custom integration from
`custom_components/<domain>/brand/`, and HACS expects the same folder (with at
least an `icon.png`) for the repository card. Both are provided, together with
the dark-mode variants and the `@2x` resolutions the brand rules ask for:

| File | Size | Used for |
| --- | --- | --- |
| `brand/icon.png` | 256×256 | Setup dialog and integration list |
| `brand/icon@2x.png` | 512×512 | High DPI screens |
| `brand/logo.png` | 512×256 | Integration page header |
| `brand/logo@2x.png` | 1024×512 | High DPI screens |
| `brand/dark_*.png` | as above | Dark theme |
| `icon.png`, `logo.png` (repository root) | 512×512, 1024×512 | HACS repository card and the README |

The artwork is generated instead of being the Rotel trademark:

```bash
python tools/make_brand_images.py   # needs Pillow
```

Restart Home Assistant after the files change; the list of custom components is
scanned at startup. Nothing else is needed — there is no `manifest.json` key for
images, and `iot_class` only describes how the integration talks to the device.

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

`protocol.py` and `const.py` have no Home Assistant imports, so the wire
format can be tested without a running Home Assistant:

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
* The tone controls use the `bypass`/`tone` naming of the current and the
  immediately preceding firmware. A unit that needs neither cannot have its
  bass/treble set, and no profile exposes the `tone_max`/`display` queries.
* Whether an amplifier reports its changes on its own depends on its *auto
  update* setting and on its firmware. A unit that never does is followed by the
  poll only, which means a knob turn shows up after at most one polling
  interval, with `origin: poll` in the event.

## License

MIT — see [LICENSE](LICENSE).