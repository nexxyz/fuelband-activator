# FuelBand WSL maintenance CLI

This is a small, dependency-free Python 3 command-line tool for the
reverse-engineered FuelBand feature protocol over Linux `hidraw`.

**Transport scope:** this release intentionally supports only VID:PID
`11AC:317D`, and is intended for WSL2 x86-64. It enumerates `/dev/hidraw*`,
uses `HIDIOCGRAWINFO`, and refuses to run unless exactly one matching device
is present.

## Prerequisites

1. Windows 11 x86-64.
2. WSL2 x86-64 with a working Linux distribution.
3. `usbipd-win` installed on Windows.
4. Python 3 available as `python3` inside the selected WSL distribution.
5. A FuelBand with VID:PID `11AC:317D` attached to WSL and visible as the
   only matching `/dev/hidraw*` device. Commands run as WSL root; the wrapper
   supplies `--user root`.

Find the USB bus ID in ordinary PowerShell:

```powershell
usbipd list
```

The first time, bind the matching `11AC:317D` device from **Administrator
PowerShell**. This is an explicit, one-time permission step:

```powershell
usbipd bind --busid <BUSID>
```

The wrapper never performs binding or elevation. After binding, attach and
detach explicitly:

```powershell
.\fuelband.ps1 attach -BusId <BUSID>
.\fuelband.ps1 detach -BusId <BUSID>
```

Both wrapper operations require the provided BUSID to appear on a usbipd list
line containing the supported `11AC:317D` VID:PID.

## Usage

Open PowerShell in this directory. If local script execution is blocked, allow
this session only:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

Attach the already-bound device, then use the read-only status command:

```powershell
.\fuelband.ps1 attach -BusId <BUSID>
.\fuelband.ps1 status
```

`status` reads only state setting 78, RTC time/date, and name setting 97. It
does not query credential, token, or other private settings. By default it
prints high-level safe values, not raw requests or responses.

The supported commands are:

```powershell
.\fuelband.ps1 set-time
.\fuelband.ps1 set-time 2026-07-26T12:34:56-04:00
.\fuelband.ps1 set-name Alice
.\fuelband.ps1 set-target -Fuel 5000
.\fuelband.ps1 mark-imprinted -Experimental -Yes
```

`set-time` without an argument uses the current WSL local time. An optional
ISO-8601 timestamp may include an offset; an offset timestamp is converted to
the WSL local wall-clock fields because the device RTC stores no offset. The
readback is accepted only when it is the requested value or up to two seconds
later.

`set-name` accepts printable ASCII only, from 1 through 23 bytes, and
verifies setting 97 after the write. The device readback slot is fixed at 25
bytes; the returned name is NUL-trimmed from that padded storage value.

`set-target FUEL` writes the same unsigned 32-bit little-endian **NikeFuel
daily target** to settings 40–46, Monday through Sunday, one day at a time.
This is a weekly daily-target operation, not a steps target. Each day requires
an ACK and strict readback verification before the next day is attempted. The
operation is non-atomic: a disconnect can leave only some days updated. There
are no hidden retries; rerun the command safely to converge all seven days.
`FUEL` must be in the range 1..0xffffffff.

`mark-imprinted` is **experimental** and is not proven to finish onboarding.
Both the PowerShell wrapper switches `-Experimental -Yes` and the Python CLI
flags `--experimental --yes` are required before it can write; the wrapper
forwards the Python flags only for this command. It reads
the four-byte state setting first, ORs bit 1 only when clear, preserves every
other byte, performs one write with no retry, and verifies the readback. It
makes no claim about onboarding success.

Use `-Distribution <name>` for a non-default WSL distribution and
`-VerboseOutput` to request nonsecret framed request/response hex:

```powershell
.\fuelband.ps1 -Distribution Ubuntu status -VerboseOutput
```

The Python CLI can also be called directly from WSL (the Windows `F:` drive is
normally `/mnt/f`):

```bash
sudo python3 /mnt/f/dev/fuelband-activator/release/fuelband_cli.py status
sudo python3 /mnt/f/dev/fuelband-activator/release/fuelband_cli.py --verbose status
sudo python3 /mnt/f/dev/fuelband-activator/release/fuelband_cli.py set-time 2026-07-26T12:34:56Z
sudo python3 /mnt/f/dev/fuelband-activator/release/fuelband_cli.py set-target 5000
sudo python3 /mnt/f/dev/fuelband-activator/release/fuelband_cli.py mark-imprinted --experimental --yes
```

`--verbose` is opt-in and is limited to the nonsecret fields handled by this
CLI: state, RTC, name, and their protocol framing. No credential or token
setting is available through the status allowlist.

When finished:

```powershell
.\fuelband.ps1 detach -BusId <BUSID>
```

For a clean release-tree check (no device access):

```powershell
python .\check_release_layout.py
```

## macOS variant

The native macOS `hidapi` implementation is in [`macos/`](macos/), with its
own requirements, README, and mocked tests. It is separate from the WSL
transport and is currently untested on real macOS hardware in this
environment.

## Disclaimer

This is an independent, reverse-engineered hardware tool. It is not made,
supported, or endorsed by Nike. Use it only with hardware you own or are
authorized to service. Writes can change persistent device state; inspect the
output and source before use. There is no warranty, and the authors are not
responsible for device state, data loss, or hardware damage. The experimental
`mark-imprinted` operation is not proven to finish onboarding and makes no
success claim.

This release contains no credentials, access tokens, bundled firmware,
executables, cloned repositories, or downloaded artifacts.

## Credits and provenance

The HID framing and command behavior were informed by the public prior
reverse-engineering work in [rbrune/fuelband-usb](https://github.com/rbrune/fuelband-usb)
and its related [Fuelbandsync](https://github.com/rbrune/Fuelbandsync) project.
This release contains a fresh minimal implementation and does not bundle or
copy binaries from those projects.

## License

MIT. See [LICENSE](LICENSE).
