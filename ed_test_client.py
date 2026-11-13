#!/usr/bin/env python3
"""Minimal Ether Dream client: streams a small dim circle to check a DAC or the bridge
without Sentinel.

    python ed_test_client.py --seconds 5
    python ed_test_client.py --estop-after 2     # check that an e-stop goes dark
"""
import argparse
import math
import socket
import struct
import sys
import time


def recv(s, n):
    buf = b""
    while len(buf) < n:
        part = s.recv(n - len(buf))
        if not part:
            raise ConnectionError("closed")
        buf += part
    return buf


def read_resp(s):
    d = recv(s, 22)
    resp, cmd = chr(d[0]), d[1]
    (_proto, le, pb, _src, lef, pbf, _sf, full, rate, count) = struct.unpack("<BBBBHHHHII", d[2:])
    return dict(resp=resp, cmd=cmd, le=le, pb=pb, lef=lef, pbf=pbf, full=full, rate=rate, count=count)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7765)
    ap.add_argument("--rate", type=int, default=20000)
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--size", type=float, default=0.15, help="circle radius, fraction of full field")
    ap.add_argument("--intensity", type=float, default=0.1, help="0..1, green")
    ap.add_argument("--target", type=int, default=900, help="buffer fullness to hold")
    ap.add_argument("--estop-after", type=float, default=0.0)
    a = ap.parse_args()

    n_circle = 400
    circle = []
    for k in range(n_circle):
        t = 2 * math.pi * k / n_circle
        x = int(32767 * a.size * math.cos(t))
        y = int(32767 * a.size * math.sin(t))
        circle.append(struct.pack("<HhhHHHHHH", 0, x, y, 0, int(65535 * a.intensity), 0, 0, 0, 0))
    pos = 0

    def batch(n):
        nonlocal pos
        out = b"".join(circle[(pos + k) % n_circle] for k in range(n))
        pos = (pos + n) % n_circle
        return b"d" + struct.pack("<H", n) + out

    s = socket.create_connection((a.host, a.port), timeout=3)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    st = read_resp(s)
    print("hello:", st)
    if st["le"] != 0:
        s.sendall(b"c")
        print("clear:", read_resp(s))
    s.sendall(b"p")
    st = read_resp(s)
    print("prepare:", st)
    if st["resp"] != "a":
        return 1
    s.sendall(batch(a.target))
    st = read_resp(s)
    s.sendall(b"b" + struct.pack("<HI", 0, a.rate))
    st = read_resp(s)
    print("begin:", st)
    t0 = time.perf_counter()
    naks = 0
    min_full, max_full = 1 << 30, 0
    while True:
        el = time.perf_counter() - t0
        if el >= a.seconds:
            break
        if a.estop_after and el >= a.estop_after:
            s.sendall(b"\x00")
            print("e-stop:", read_resp(s))
            s.sendall(batch(100))
            print("data after e-stop (expect '!'):", read_resp(s)["resp"])
            s.sendall(b"c")
            print("clear:", read_resp(s))
            s.close()
            return 0
        room = a.target - st["full"]
        if room >= 80:
            s.sendall(batch(room))
        else:
            time.sleep(0.002)
            s.sendall(b"?")
        st = read_resp(s)
        if st["resp"] != "a":
            naks += 1
            if st["pb"] == 0:
                print("DAC went idle:", st)
                break
        min_full, max_full = min(min_full, st["full"]), max(max_full, st["full"])
    el = time.perf_counter() - t0
    print("played %d points in %.2fs = %.0f pps (asked %d), fullness %d..%d, naks=%d" % (
        st["count"], el, st["count"] / el, a.rate, min_full, max_full, naks))
    s.sendall(b"s")
    print("stop:", read_resp(s))
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
