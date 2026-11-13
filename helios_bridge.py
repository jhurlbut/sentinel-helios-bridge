#!/usr/bin/env python3
"""helios_bridge: present Helios laser DACs to Sentinel as Ether Dream DACs.

Sentinel's Laser Out speaks Ether Dream over TCP. This program answers as one
Ether Dream per Helios and forwards the point stream to the Helios through the
vendor DLL. Laser Out keeps its scanner protection, Frame Lock, arms and
e-stop; the bridge adds only transport.

    python helios_bridge.py                 # real Helios DACs over USB
    python helios_bridge.py --backend null  # virtual DAC, no hardware
    python helios_bridge.py --list          # list Helios DACs and exit

In Laser Out: protocol = etherdream, device = 127.0.0.1:7765 (second DAC 7766).
"""
import argparse
import ctypes
import logging
import os
import socket
import struct
import sys
import threading
import time

import numpy as np

log = logging.getLogger("helios_bridge")

# ---------------------------------------------------------------- formats

ED_POINT = np.dtype([("control", "<u2"), ("x", "<i2"), ("y", "<i2"),
                     ("r", "<u2"), ("g", "<u2"), ("b", "<u2"),
                     ("i", "<u2"), ("u1", "<u2"), ("u2", "<u2")])
assert ED_POINT.itemsize == 18
HELIOS_POINT = np.dtype([("x", "<u2"), ("y", "<u2"),
                         ("r", "u1"), ("g", "u1"), ("b", "u1"), ("i", "u1")])
assert HELIOS_POINT.itemsize == 8

ED_UDP_PORT = 7654
ED_TCP_PORT = 7765
RATE_CHANGE_BIT = 0x8000

LE_READY, LE_ESTOP = 0, 3
PB_IDLE, PB_PREPARED, PB_PLAYING = 0, 1, 2
LEF_ESTOP_PACKET, LEF_ESTOP_EXTERNAL = 0x01, 0x02
PBF_SHUTTER, PBF_UNDERFLOW, PBF_ESTOP = 0x01, 0x02, 0x04

HELIOS_MAX_POINTS = 0xFFF
HELIOS_MAX_PPS = 0xFFFF
HELIOS_FLAGS_SINGLE_MODE = 1 << 1   # play once; DAC goes dark if we stop feeding
MIN_FRAME_POINTS = 20


class BackendError(Exception):
    pass


# ---------------------------------------------------------------- backends

class NullBackend:
    """Virtual Helios: one playing frame plus one queued frame, paced by time."""

    def __init__(self, name="Null Helios", clock_offset_pct=0.0):
        self.name = name
        self.firmware = 0
        self.max_pps = HELIOS_MAX_PPS
        self._scale = 1.0 / (1.0 + clock_offset_pct / 100.0)
        self._play_end = 0.0
        self._pending = None

    def ready(self):
        now = time.perf_counter()
        if self._pending is not None and now >= self._play_end:
            self._play_end += self._pending
            self._pending = None
        return self._pending is None

    def write(self, pts, pps):
        dur = len(pts) / pps * self._scale
        now = time.perf_counter()
        if now >= self._play_end:
            self._play_end = now + dur
        else:
            self._pending = dur

    def stop(self):
        self._play_end = 0.0
        self._pending = None

    def alive(self):
        return True


class HeliosLib:
    """ctypes wrapper around HeliosLaserDAC.dll."""

    def __init__(self, path):
        self.lib = lib = ctypes.CDLL(path)
        u, i = ctypes.c_uint, ctypes.c_int
        lib.OpenDevicesOnlyUsb.restype = i
        lib.OpenDevices.restype = i
        lib.ReScanDevicesOnlyUsb.restype = i
        lib.ReScanDevices.restype = i
        lib.CloseDevices.restype = i
        for fn in ("GetStatus", "GetIsClosed", "Stop", "GetFirmwareVersion",
                   "GetIsUsb", "GetSupportsHigherResolutions"):
            getattr(lib, fn).argtypes = [u]
            getattr(lib, fn).restype = i
        lib.GetName.argtypes = [u, ctypes.c_char_p]
        lib.GetName.restype = i
        lib.SetShutter.argtypes = [u, ctypes.c_bool]
        lib.SetShutter.restype = i
        lib.WriteFrame.argtypes = [u, i, ctypes.c_uint8, ctypes.c_void_p, i]
        lib.WriteFrame.restype = i

    def open(self, network):
        return self.lib.OpenDevices() if network else self.lib.OpenDevicesOnlyUsb()

    def rescan(self, network):
        return self.lib.ReScanDevices() if network else self.lib.ReScanDevicesOnlyUsb()

    def name(self, idx):
        buf = ctypes.create_string_buffer(64)
        if self.lib.GetName(idx, buf) < 0:
            return "Helios %d" % idx
        return buf.value.decode("ascii", "replace")


class HeliosBackend:
    def __init__(self, hl, idx, opts):
        self.hl, self.idx, self.opts = hl, idx, opts
        self.name = hl.name(idx)
        self.firmware = hl.lib.GetFirmwareVersion(idx)
        self.max_pps = HELIOS_MAX_PPS

    def ready(self):
        r = self.hl.lib.GetStatus(self.idx)
        if r < 0:
            raise BackendError("GetStatus returned %d" % r)
        return r == 1

    def write(self, pts, pps):
        out = to_helios(pts, self.opts)
        r = self.hl.lib.WriteFrame(self.idx, int(pps), HELIOS_FLAGS_SINGLE_MODE,
                                   out.ctypes.data, len(out))
        if r < 0:
            raise BackendError("WriteFrame returned %d" % r)

    def stop(self):
        self.hl.lib.Stop(self.idx)            # blanks and centres; blocks 100 ms
        self.hl.lib.SetShutter(self.idx, False)

    def alive(self):
        return self.hl.lib.GetIsClosed(self.idx) == 0


def to_helios(pts, opts):
    """Ether Dream points (signed 16-bit XY, 16-bit colour) to Helios 12/8-bit."""
    out = np.empty(len(pts), HELIOS_POINT)
    x = (pts["x"].astype(np.int32) + 32768) >> 4
    y = (pts["y"].astype(np.int32) + 32768) >> 4
    if opts.swap_xy:
        x, y = y, x
    if opts.flip_x:
        x = 4095 - x
    if opts.flip_y:
        y = 4095 - y
    out["x"], out["y"] = x, y
    k = opts.max_intensity
    r = (pts["r"] >> 8).astype(np.float32) * k
    g = (pts["g"] >> 8).astype(np.float32) * k
    b = (pts["b"] >> 8).astype(np.float32) * k
    out["r"], out["g"], out["b"] = r, g, b
    out["i"] = np.maximum(np.maximum(out["r"], out["g"]), out["b"])
    return out


# ---------------------------------------------------------------- virtual DAC

class VirtualDac:
    """One Ether Dream endpoint backed by one Helios."""

    def __init__(self, backend, index, opts, scan_gate):
        self.backend, self.index, self.opts = backend, index, opts
        self.scan_gate = scan_gate
        self.port = opts.port + index
        self.mac = bytes([0x02, 0x48, 0x45, 0x4C, 0x00, index & 0xFF])  # 02:'HEL'
        self.lock = threading.Lock()
        self.fifo = bytearray()
        self.rate_queue = []
        self.rate = 0
        self.playback = PB_IDLE
        self.le_state = LE_READY
        self.le_flags = 0
        self.pb_flags = 0
        self.point_count = 0
        self.fault = False
        self.stop_req = False
        self.running = True
        self.conn = None
        # stats
        self.underflows = 0
        self.chunks = 0
        self.max_write_ms = 0.0
        self.max_gap_ms = 0.0
        self._last_write = None
        self._starve_since = None
        threading.Thread(target=self._dac_loop, daemon=True,
                         name="dac%d" % index).start()
        threading.Thread(target=self._server_loop, daemon=True,
                         name="srv%d" % index).start()

    # ---- status

    def status_bytes(self):
        with self.lock:
            return struct.pack("<BBBBHHHHII", 0, self.le_state, self.playback, 0,
                               self.le_flags, self.pb_flags, 0,
                               min(len(self.fifo) // 18, 0xFFFF),
                               self.rate, self.point_count & 0xFFFFFFFF)

    def beacon(self):
        return struct.pack("<6sHHHI", self.mac, 0x4845, 2, self.opts.capacity,
                           min(self.backend.max_pps, self.opts.max_rate)) + self.status_bytes()

    # ---- state changes (call with self.lock held)

    def _go_idle(self):
        self.playback = PB_IDLE
        self.pb_flags &= ~PBF_SHUTTER
        self.fifo.clear()
        self.rate_queue.clear()
        self.stop_req = True

    def _estop(self, flag):
        self.le_state = LE_ESTOP
        self.le_flags |= flag
        self.pb_flags |= PBF_ESTOP
        self._go_idle()

    # ---- DAC thread: the only thread that drives the backend while playing

    def _dac_loop(self):
        while self.running:
            if self.stop_req:
                self.stop_req = False
                with self.scan_gate:
                    try:
                        self.backend.stop()
                    except Exception as e:  # noqa: BLE001
                        log.warning("[%d] stop failed: %s", self.index, e)
                self._last_write = None
                self._starve_since = None
                continue
            if self.playback != PB_PLAYING or self.fault:
                time.sleep(0.002)
                continue
            try:
                if not self.backend.ready():
                    time.sleep(0.0003)
                    continue
                chunk, rate = self._take_chunk()
                if chunk is None:
                    time.sleep(0.0003)
                    continue
                t0 = time.perf_counter()
                self.backend.write(chunk, rate)
                t1 = time.perf_counter()
            except BackendError as e:
                self._on_fault(str(e))
                continue
            if self._last_write is not None:
                self.max_gap_ms = max(self.max_gap_ms, (t0 - self._last_write) * 1e3)
            self._last_write = t0
            self.max_write_ms = max(self.max_write_ms, (t1 - t0) * 1e3)
            self.chunks += 1
            with self.lock:
                self.point_count += len(chunk)

    def _take_chunk(self):
        """Pop the next frame for the DAC, or (None, 0) while waiting for data."""
        with self.lock:
            if self.playback != PB_PLAYING:
                return None, 0
            rate = self.rate
            avail = len(self.fifo) // 18
            want = int(rate * self.opts.chunk_ms / 1000.0)
            want = max(MIN_FRAME_POINTS, min(want, HELIOS_MAX_POINTS))
            now = time.perf_counter()
            if avail < want:
                if self._starve_since is None:
                    self._starve_since = now
                if (now - self._starve_since) * 1000.0 < self.opts.chunk_ms * 0.5:
                    return None, 0
                if avail < MIN_FRAME_POINTS:
                    # The DAC has run dry: Ether Dream semantics, back to idle.
                    self.underflows += 1
                    self.pb_flags |= PBF_UNDERFLOW
                    self._go_idle()
                    log.warning("[%d] underflow (fifo=%d)", self.index, avail)
                    return None, 0
                want = avail
            self._starve_since = None
            pts = np.frombuffer(bytes(self.fifo[:want * 18]), ED_POINT)
            flagged = np.flatnonzero(pts["control"] & RATE_CHANGE_BIT)
            if len(flagged) and flagged[0] == 0:
                if self.rate_queue:
                    self.rate = rate = self.rate_queue.pop(0)
                flagged = flagged[1:]
            if len(flagged):
                want = int(flagged[0])      # next rate change starts its own frame
                pts = pts[:want]
            del self.fifo[:want * 18]
            return pts, rate

    def _on_fault(self, why):
        log.error("[%d] DAC fault: %s", self.index, why)
        with self.lock:
            self.fault = True
            self._estop(LEF_ESTOP_EXTERNAL)
            conn = self.conn
        if conn:
            try:
                conn.close()
            except OSError:
                pass

    # ---- TCP server

    def _server_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1) \
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE") else None
        srv.bind((self.opts.bind, self.port))
        srv.listen(2)
        log.info("[%d] '%s' fw=%s listening on %s:%d", self.index,
                 self.backend.name, self.backend.firmware, self.opts.bind, self.port)
        while self.running:
            conn, addr = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self.lock:
                old, self.conn = self.conn, conn
            if old:
                try:
                    old.close()
                except OSError:
                    pass
            threading.Thread(target=self._client, args=(conn, addr), daemon=True).start()

    def _reply(self, conn, resp, cmd):
        conn.sendall(bytes([ord(resp), cmd]) + self.status_bytes())

    @staticmethod
    def _recv(conn, n):
        buf = bytearray()
        while len(buf) < n:
            part = conn.recv(n - len(buf))
            if not part:
                raise ConnectionError("closed")
            buf += part
        return bytes(buf)

    def _client(self, conn, addr):
        log.info("[%d] client connected from %s:%d", self.index, *addr)
        trace = self.opts.trace
        try:
            self._reply(conn, "a", ord("?"))
            while True:
                cmd = self._recv(conn, 1)[0]
                resp = "a"
                if cmd == ord("d"):
                    n = struct.unpack("<H", self._recv(conn, 2))[0]
                    data = self._recv(conn, n * 18)
                    with self.lock:
                        if self.le_state != LE_READY:
                            resp = "!"
                        elif self.playback == PB_IDLE:
                            resp = "I"
                        elif len(self.fifo) // 18 + n > self.opts.capacity:
                            resp = "F"
                        else:
                            self.fifo += data
                    if trace > 1 or (trace and resp != "a"):
                        log.info("[%d] d n=%d -> %s", self.index, n, resp)
                elif cmd == ord("p"):
                    with self.scan_gate, self.lock:
                        if self.le_state != LE_READY:
                            resp = "!"
                        elif self.playback != PB_IDLE:
                            resp = "I"
                        else:
                            self.playback = PB_PREPARED
                            self.pb_flags = 0
                            self.point_count = 0
                            self.fifo.clear()
                            self.rate_queue.clear()
                    if trace:
                        log.info("[%d] prepare -> %s", self.index, resp)
                elif cmd == ord("b"):
                    _lwm, rate = struct.unpack("<HI", self._recv(conn, 6))
                    with self.lock:
                        if self.playback != PB_PREPARED or not 0 < rate <= self.backend.max_pps:
                            resp = "I"
                        else:
                            self.rate = rate
                            self.playback = PB_PLAYING
                            self.pb_flags |= PBF_SHUTTER
                            self._starve_since = None
                    if trace:
                        log.info("[%d] begin rate=%d -> %s", self.index, rate, resp)
                elif cmd in (0x71, 0x74):   # 'q'; 0x74 is the spec's known typo
                    rate = struct.unpack("<I", self._recv(conn, 4))[0]
                    with self.lock:
                        if self.playback == PB_IDLE or not 0 < rate <= self.backend.max_pps:
                            resp = "I"
                        else:
                            self.rate_queue.append(rate)
                    if trace:
                        log.info("[%d] queue rate=%d -> %s", self.index, rate, resp)
                elif cmd == ord("s"):
                    with self.lock:
                        if self.playback == PB_IDLE:
                            resp = "I"
                        self._go_idle()
                    if trace:
                        log.info("[%d] stop -> %s", self.index, resp)
                elif cmd in (0x00, 0xFF):
                    with self.lock:
                        self._estop(LEF_ESTOP_PACKET)
                    log.warning("[%d] E-STOP from client", self.index)
                elif cmd == ord("c"):
                    with self.lock:
                        if self.fault:
                            resp = "!"
                        else:
                            self.le_state = LE_READY
                            self.le_flags = 0
                            self.pb_flags &= ~PBF_ESTOP
                    if trace:
                        log.info("[%d] clear e-stop -> %s", self.index, resp)
                elif cmd == ord("?"):
                    if trace > 1:
                        log.info("[%d] ping", self.index)
                elif cmd == ord("v"):
                    conn.sendall(b"helios_bridge 1".ljust(32, b"\0"))
                    continue
                else:
                    log.error("[%d] unknown command 0x%02X, closing link", self.index, cmd)
                    break
                self._reply(conn, resp, cmd)
        except (ConnectionError, OSError) as e:
            log.info("[%d] client gone: %s", self.index, e)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self.lock:
                if self.conn is conn:
                    self.conn = None
                    self._go_idle()     # link lost: go dark

    def shutdown(self):
        self.running = False
        try:
            self.backend.stop()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- main

def beacon_loop(dacs, opts):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    targets = ["127.0.0.1"] + (["255.255.255.255"] if opts.lan else [])
    while True:
        for d in list(dacs):
            if d.fault:
                continue
            for t in targets:
                try:
                    s.sendto(d.beacon(), (t, ED_UDP_PORT))
                except OSError:
                    pass
        time.sleep(1.0)


def stats_loop(dacs):
    last = {}
    while True:
        time.sleep(1.0)
        for d in list(dacs):
            if d.playback != PB_PLAYING:
                last.pop(d.index, None)
                continue
            pc = d.point_count
            prev = last.get(d.index)
            last[d.index] = pc
            if prev is None or pc < prev:   # first sample, or a new prepare reset the count
                continue
            log.info("[%d] playing rate=%d played/s=%d fifo=%d chunks=%d "
                     "max_gap=%.1fms max_write=%.1fms underflows=%d",
                     d.index, d.rate, pc - prev, len(d.fifo) // 18, d.chunks,
                     d.max_gap_ms, d.max_write_ms, d.underflows)
            d.max_gap_ms = d.max_write_ms = 0.0


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--backend", choices=["helios", "null"], default="helios")
    ap.add_argument("--dll", default=os.path.join(here, "vendor", "helios_sdk", "HeliosLaserDAC.dll"))
    ap.add_argument("--list", action="store_true", help="list Helios DACs and exit")
    ap.add_argument("--network", action="store_true", help="also scan for network (IDN) Helios DACs")
    ap.add_argument("--bind", default="127.0.0.1", help="TCP listen address (default loopback only)")
    ap.add_argument("--lan", action="store_true", help="listen on all interfaces and broadcast discovery")
    ap.add_argument("--port", type=int, default=ED_TCP_PORT, help="TCP port of the first DAC")
    ap.add_argument("--capacity", type=int, default=1800, help="advertised point buffer")
    ap.add_argument("--max-rate", type=int, default=40000)
    ap.add_argument("--chunk-ms", type=float, default=20.0, help="length of each frame sent to the Helios")
    ap.add_argument("--max-intensity", type=float, default=1.0, help="bridge-side colour ceiling, 0..1")
    ap.add_argument("--flip-x", action="store_true")
    ap.add_argument("--flip-y", action="store_true")
    ap.add_argument("--swap-xy", action="store_true")
    ap.add_argument("--null-clock-offset", type=float, default=0.0, help="null backend clock error, percent")
    ap.add_argument("--trace", type=int, default=1, help="0 quiet, 1 commands, 2 every data packet")
    opts = ap.parse_args()
    if opts.lan:
        opts.bind = "0.0.0.0"
    opts.max_intensity = max(0.0, min(1.0, opts.max_intensity))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(message)s",
                        datefmt="%H:%M:%S")

    scan_gate = threading.RLock()
    dacs = []
    hl = None
    if opts.backend == "null":
        dacs.append(VirtualDac(NullBackend(clock_offset_pct=opts.null_clock_offset), 0, opts, scan_gate))
    else:
        hl = HeliosLib(opts.dll)
        n = hl.open(opts.network)
        log.info("Helios scan: %d device(s)", n)
        if opts.list:
            for i in range(max(n, 0)):
                print("%d: %s  firmware=%d  usb=%d  highres=%d" % (
                    i, hl.name(i), hl.lib.GetFirmwareVersion(i), hl.lib.GetIsUsb(i),
                    hl.lib.GetSupportsHigherResolutions(i)))
            hl.lib.CloseDevices()
            return 0
        for i in range(max(n, 0)):
            dacs.append(VirtualDac(HeliosBackend(hl, i, opts), i, opts, scan_gate))
        if not dacs:
            log.info("no Helios found yet; waiting for one to be plugged in")

    threading.Thread(target=beacon_loop, args=(dacs, opts), daemon=True).start()
    threading.Thread(target=stats_loop, args=(dacs,), daemon=True).start()

    try:
        while True:
            time.sleep(2.0)
            if hl is None:
                continue
            # Hot-plug and fault recovery. Never rescan while any DAC has output.
            if not dacs or any(d.fault for d in dacs):
                with scan_gate:
                    if any(d.playback != PB_IDLE for d in dacs):
                        continue
                    n = hl.rescan(opts.network)
                    for i in range(len(dacs), max(n, 0)):
                        dacs.append(VirtualDac(HeliosBackend(hl, i, opts), i, opts, scan_gate))
                    for d in dacs:
                        if d.fault and d.backend.alive():
                            log.info("[%d] DAC is back; waiting for the client to clear the e-stop", d.index)
                            d.fault = False
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        for d in dacs:
            d.shutdown()
        if hl is not None:
            hl.lib.CloseDevices()
    return 0


if __name__ == "__main__":
    sys.exit(main())
