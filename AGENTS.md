# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, Gemini and others) working
in this repository or helping a user set it up.

## What this is

`helios_bridge.py` presents each USB Helios laser DAC as an Ether Dream DAC on
`127.0.0.1:7765+N`, so Sentinel's Laser Out node can drive it with
`protocol=etherdream`. It is pure transport: scanner protection, Frame Lock,
arming and e-stop stay in Sentinel. Keep it that way; do not add content
generation, geometry correction or safety overrides to the bridge.

## Laser safety rules (do not bend)

- **Never arm a laser.** Do not set a Laser Out node's `armed` to true, and
  never try to arm Sentinel's master (`/sentinel/laser/master_armed`); only a
  person clicking **ARM LASERS** may do that.
- **Never run `ed_test_client.py` or start the bridge against real hardware on
  your own.** Both can put light on a wall. Ask the user, who must confirm the
  projector is aimed safely with nobody in the beam.
- Prefer `--backend null` for anything you can test without hardware.
- Never loosen Laser Out's scanner profile (`max_step`, `max_step_change`,
  `min_visible_speed`, dwell, guard, `insert_cap`) to make content fit.
- Keep the fail-dark behaviour: play-once frames, `Stop` + shutter close on
  e-stop, stop, link loss, underflow and USB fault. Any change that could leave
  the last frame looping on the DAC is a bug.

## Layout

- `helios_bridge.py`: Ether Dream TCP server (`VirtualDac`), UDP discovery
  beacons (port 7654), `HeliosBackend` (ctypes over `HeliosLaserDAC.dll`) and
  `NullBackend` (time-paced virtual DAC).
- `ed_test_client.py`: minimal Ether Dream client for checking the bridge.
- `vendor/helios_sdk/`: unmodified upstream binaries and header. Do not edit;
  to update, rebuild or download from Grix/helios_dac and record the commit and
  SHA-256 in `COMMIT.txt`.

## Conventions

- Python 3.8+, standard library plus numpy only. Windows is the target
  platform. Python 3.11+ is recommended: earlier versions round
  `time.sleep()` to the ~15.6 ms Windows tick, which `setup_fine_sleep()`
  works around with `timeBeginPeriod(1)`. Don't remove that fallback.
- Ether Dream points are 18 bytes (signed 16-bit XY, 16-bit colour); Helios
  points are 8 bytes (12-bit XY, 8-bit colour). Conversion lives in
  `to_helios()`; axis flips and `--max-intensity` are applied there.
- Only the DAC thread drives the backend while playing. Rescans happen under
  `scan_gate` and only while every DAC is idle.
- Keep the README's options table in sync with `argparse` in `main()`.

## Testing

```powershell
python helios_bridge.py --backend null           # terminal 1
python ed_test_client.py --seconds 5             # terminal 2 (null backend: no light)
python ed_test_client.py --estop-after 2
```

Expect `played/s` near the requested rate, `underflows=0`, and the e-stop run
answering `!` to data sent after the e-stop. With Sentinel running, a Laser Out
on `etherdream` / `127.0.0.1:7765` should leave `waiting for discovery` within
about a second of starting the bridge.

## Sentinel MCP pointers

If the Sentinel MCP server is available: create the node with
`sentinel_pipeline action=create type=laserout`, set `protocol` to
`etherdream` and `device` to `127.0.0.1:7765` with `sentinel_state`, wire a
Scan Signal with `sentinel_graph action=add_link`, and read the node's
`statusMessage` from `sentinel_pipeline action=info`. Leave it disarmed.
