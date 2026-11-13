# sentinel-helios-bridge

Drive a [Helios laser DAC](https://bitlasers.com/helios-laser-dac/) from
[Sentinel](https://oodlabs.com)'s **Laser Out** node.

Laser Out speaks Ether Dream, LaserCube and ShowNET, but not Helios. This
bridge answers on the local machine as one Ether Dream DAC per Helios and
forwards the point stream to the Helios over USB through the vendor DLL. Laser
Out keeps everything it already does: scanner protection, Frame Lock, the node
arm, the master **ARM LASERS** arm and e-stop. The bridge only adds transport.

```
Sentinel Laser Out ──Ether Dream/TCP 127.0.0.1:7765──► helios_bridge.py ──USB──► Helios DAC ──► projector
```

Because Laser Out sits upstream of the DAC, anything you feed it (Mapping
Editor / adaptive mapping chains, Laser Trace, ILDA Player) works unchanged.

> **Laser safety.** This software sends real light to real scanners. Test with
> the projector aimed at a wall, nobody in the beam, a low `--max-intensity`
> and a small `amplitude_limit`. You are responsible for the safe and legal
> operation of your laser.

## Requirements

- Windows 10/11 x64 (the bundled DLLs are Windows builds)
- Python 3.8+ and numpy (`pip install numpy`)
- A USB Helios DAC (network/IDN Helios: try `--network`, untested)
- Sentinel with the Laser Out node (`laserout`) and a license tier that allows
  hardware laser output (trial installs run hardware protocols as Record)

## Files

| File | What it is |
| --- | --- |
| `helios_bridge.py` | The bridge. |
| `ed_test_client.py` | Streams a small dim green circle over Ether Dream with no Sentinel involved. **Emits light.** |
| `vendor/helios_sdk/` | `HeliosLaserDAC.dll`, `libusb-1.0.dll`, header and example from [Grix/helios_dac](https://github.com/Grix/helios_dac) (MIT; libusb is LGPL-2.1). Source commit in `COMMIT.txt`. |

## Quick start

```powershell
pip install numpy
python helios_bridge.py --list                 # should list your Helios
python helios_bridge.py --max-intensity 0.2    # leave running
```

In Sentinel, create a Laser Out (or retarget an existing one):

| Laser Out parameter | Value |
| --- | --- |
| `protocol` | `etherdream` |
| `device` | `127.0.0.1:7765` (second Helios: `7766`, and so on) |
| `amplitude_limit` | small for first light (new nodes start at 0.25) |

The node status changes from `waiting for discovery` once the bridge is
running. Arm the node, then click **ARM LASERS** in the menu bar.

No hardware yet? `python helios_bridge.py --backend null` runs a virtual DAC so
you can check the Sentinel side end to end.

## First light checklist

Point the projector at a wall with nobody in the beam.

1. Plug in the Helios. `python helios_bridge.py --list` should show it.
2. `python helios_bridge.py --max-intensity 0.2`
3. In a second terminal: `python ed_test_client.py --seconds 5`.
   Expect a small circle, `played/s` equal to the rate in the bridge log, and
   `underflows=0`. Then `python ed_test_client.py --estop-after 2` must go dark.
4. Kill the bridge mid-stream (Ctrl+C, then again with Task Manager). The beam
   must go dark both times.
5. In Sentinel, in a blank project: Laser Out, `protocol=etherdream`,
   `device=127.0.0.1:7765`, small `amplitude_limit`, `test_pattern`
   Orientation F. Arm the node, then click **ARM LASERS**. Fix orientation with
   Laser Out's `flip_x` / `flip_y` / `rotate_180`, or the bridge's `--flip-x`,
   `--flip-y`, `--swap-xy`.
6. Watch `underflows`, `frames_skipped`, `frames_repeated` and
   `frame_latency_ms` on the Laser Out, and `max_gap` in the bridge log.
   Tune `--chunk-ms` (10 to 40) and Laser Out's `sent_ahead_samples`.

## Using it with laser mapping

Mapping geometry is computed before Laser Out, so it does not care which DAC is
on the end. Wire your mapping chain's mapped Scan Signal into the Helios Laser
Out the same way you would for any other DAC. Two things to know:

- **Fix orientation before you calibrate.** Flipping after mapping undoes the
  calibration.
- **Re-check the scanner profile.** `max_step`, `max_step_change`, dwell and
  point rate belong to the scanner, not the DAC. If the Helios drives a
  different projector than your mapping was tuned on, re-tune for it, and
  re-map if the projector moved.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--backend helios\|null` | `helios` | Real hardware, or a virtual DAC |
| `--list` | | List Helios DACs and exit |
| `--network` | off | Also scan for network (IDN) Helios DACs |
| `--port` | `7765` | TCP port of the first DAC; each further DAC is +1 |
| `--lan` | off | Listen on all interfaces and broadcast discovery (default is loopback only) |
| `--max-intensity` | `1.0` | Bridge-side colour ceiling, 0..1 |
| `--flip-x`, `--flip-y`, `--swap-xy` | off | Axis fixes applied in the bridge |
| `--chunk-ms` | `20` | Length of each frame sent to the Helios |
| `--capacity` | `1800` | Advertised Ether Dream point buffer |
| `--max-rate` | `40000` | Highest point rate advertised |
| `--trace 0\|1\|2` | `1` | Quiet, commands, every data packet |

## Behaviour

- One TCP port per Helios, starting at 7765. Loopback only unless `--lan`.
- Frames go to the Helios in play-once mode, so if the bridge dies the DAC
  runs out of points and goes dark by itself.
- E-stop, stop, link loss and underflow all call Helios `Stop` and close the
  shutter. A USB fault is reported to Laser Out as an external e-stop and drops
  the link; after the DAC comes back, clear the e-stop from Sentinel.
- Hot-plug: a Helios plugged in later is picked up while all outputs are idle.
- The Helios has no interlock or temperature readback; those Laser Out
  readouts stay at 0.
- XY is 12-bit and colour 8-bit on the USB Helios. The intensity channel is the
  maximum of R, G and B.

## Status

Tested against the `null` backend and Sentinel's Laser Out. Treat physical
output as unproven on your rig until you have run the first light checklist.
Issues and pull requests are welcome.

## License

The bridge code is MIT (see `LICENSE`). Bundled third-party files keep their
own licenses; see `THIRD_PARTY_NOTICES.md`.

Not affiliated with OODLabs or Bitlasers.
