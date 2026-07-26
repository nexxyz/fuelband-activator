# FuelBand native macOS variant

This directory is a separate native macOS artifact. It uses the external
[`hidapi`](https://pypi.org/project/hidapi/) Python package (`import hid`) and
macOS's native HID transport. It does not use WSL, `hidraw`, usbipd, kernel
drivers, or a custom driver.

The implementation is limited to exactly one HID collection with VID:PID
`11AC:317D`, usage page `0xFF00`, and usage `0x01`. It sends 64-byte
zero-padded feature reports, selects output report IDs 09/0A/0B/0C from
framed command length, requests input feature report ID 04, and accepts only
the observed protocol response report ID 01.

## Install

Use Python 3 on macOS and install the one external dependency:

```sh
python3 -m pip install -r requirements.txt
```

The code is intended to be usable on both Apple Silicon (arm64) and Intel
(x86_64) macOS, subject to a compatible Python 3 and a hidapi build. This
environment cannot test native macOS HID access or real hardware, so this
variant is **untested on real macOS hardware here**.

## Usage

Connect the FuelBand directly and ensure macOS exposes exactly one matching
HID collection. From this directory:

```sh
python3 fuelband_macos.py status
python3 fuelband_macos.py set-time
python3 fuelband_macos.py set-time 2026-07-26T12:34:56-04:00
python3 fuelband_macos.py set-name Alice
python3 fuelband_macos.py set-target 5000
python3 fuelband_macos.py mark-imprinted --experimental --yes
```

`status` reads only device-state setting 78, RTC time/date, and name setting
97. It never queries credential, token, or other sensitive settings. Raw
requests and responses are hidden by default; add `--verbose` before the
subcommand only when nonsecret protocol framing is useful.

`set-time` without an argument uses macOS local time. An ISO-8601 timestamp may
include an offset; offset timestamps are converted to macOS local wall-clock
fields because the device RTC stores no offset. Readback is accepted only at
the requested value or up to two seconds later.

`set-name` accepts printable ASCII of 1–23 bytes for writing. Setting 97 has a
fixed 25-byte readback slot, so the implementation requires that width and
NUL-trims the padded contents before comparison.

`set-target FUEL` writes the same unsigned 32-bit little-endian NikeFuel daily
target to settings 40–46, Monday through Sunday. This is a weekly daily-target
operation, not a steps target. Each day is ACKed and read back before the next
write. The operation is non-atomic: a disconnect can leave only some days
updated. There are no hidden retries; rerun the command safely to converge
the week. `FUEL` must be 1..0xffffffff.

`mark-imprinted` is experimental and is **not proven to finish onboarding**.
Both flags are required before it can write. It reads all four state bytes,
ORs bit 1 only when clear, preserves every other byte, performs exactly one
write with no retry, and verifies the readback. It makes no onboarding claim.

## Limitations and disclaimer

This is an independent, reverse-engineered hardware tool. It is not made,
supported, or endorsed by Nike. Use it only with hardware you own or are
authorized to service. Writes can change persistent device state; inspect the
source and output before use. There is no warranty, and the authors are not
responsible for device state, data loss, or hardware damage. Native macOS
hardware behavior has not been validated in this environment.

## Credits

Protocol framing and command behavior were informed by the public prior
reverse-engineering work in
[rbrune/fuelband-usb](https://github.com/rbrune/fuelband-usb) and its related
[Fuelbandsync](https://github.com/rbrune/Fuelbandsync) project. This artifact
contains no copied bundled binaries, credentials, tokens, or downloaded
repositories.

## Tests

The tests use only the Python standard library and mocked hidapi objects; they
do not open hardware:

```sh
python3 -m unittest discover -s . -p 'test_*.py'
python3 -m py_compile fuelband_macos.py test_fuelband_macos.py
```
