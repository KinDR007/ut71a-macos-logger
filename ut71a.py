#!/usr/bin/env python3
"""Read measurements from UNI-T UT71A/B/C/D/E via UT-D04 cable (WCH CH9325, 1a86:e008).

Protocol references: libsigrok src/dmm/ut71x.c and src/serial_hid_ch9325.c.
"""
import argparse
import csv
import sys
import time

import hid

VID, PID = 0x1A86, 0xE008
BAUD = 2400
PACKET_LEN = 11

# (name, unit, ac, dc), indexed by function byte
FUNCTIONS = [
    ("AC mV", "V", True, False),
    ("DC V", "V", False, True),
    ("AC V", "V", True, False),
    ("DC mV", "V", False, True),
    ("Resistance", "Ohm", False, False),
    ("Capacitance", "F", False, False),
    ("Temperature", "°C", False, False),
    ("uA", "A", False, True),
    ("mA", "A", False, True),
    ("10A", "A", False, True),
    ("Continuity", "Ohm", False, False),
    ("Diode", "V", False, True),
    ("Frequency", "Hz", False, False),
    ("Temperature", "°F", False, False),
    ("Power", "W", False, False),
    ("Loop current", "%", False, True),
]

# Decimal exponent per [function][range]
EXPONENTS = [
    [-5, 0, 0, 0, 0, 0, 0, 0],
    [0, -4, -3, -2, -1, 0, 0, 0],
    [0, -4, -3, -2, -1, 0, 0, 0],
    [-5, 0, 0, 0, 0, 0, 0, 0],
    [0, -2, -1, 0, 1, 2, 3, 0],
    [0, -12, -11, -10, -9, -8, -7, -6],
    [-1, 0, 0, 0, 0, 0, 0, 0],
    [-8, -7, 0, 0, 0, 0, 0, 0],
    [-6, -5, 0, 0, 0, 0, 0, 0],
    [0, -3, 0, 0, 0, 0, 0, 0],
    [-1, 0, 0, 0, 0, 0, 0, 0],
    [-4, 0, 0, 0, 0, 0, 0, 0],
    [-3, -2, -1, 0, 1, 2, 3, 4],
    [-1, 0, 0, 0, 0, 0, 0, 0],
    [0, 0, 0, 0, 0, 0, 0, 0],
    [-2, 0, 0, 0, 0, 0, 0, 0],
]


class Ch9325:
    def __init__(self, path=None):
        if path is None:
            devs = hid.enumerate(VID, PID)
            if not devs:
                raise RuntimeError("Cable 1a86:e008 not found")
            path = devs[0]["path"]
        self.dev = hid.device()
        self.dev.open_path(path)
        # Feature report ID 0: baudrate (LE16) + UART config
        cfg = [0x00, BAUD & 0xFF, BAUD >> 8, 0x00, 0x00, 0x03]
        if self.dev.send_feature_report(cfg) < 0:
            raise RuntimeError(f"CH9325 init failed: {self.dev.error()}")

    def read(self, timeout_ms=1000):
        """Return received UART bytes (parity bit stripped)."""
        report = self.dev.read(8, timeout_ms)
        if not report:
            return b""
        n = report[0] & 0x07
        return bytes(b & 0x7F for b in report[1 : 1 + n])

    def close(self):
        self.dev.close()


def decode(pkt):
    """Decode 11-byte UT71x packet. Return dict or None if invalid."""
    if len(pkt) != PACKET_LEN or pkt[9:] != b"\r\n":
        return None
    digits, rng, func = pkt[0:5], pkt[5] - 0x30, pkt[6] - 0x30
    st1, st2 = pkt[7], pkt[8]
    if not 0 <= rng <= 7 or not 0 <= func <= 15:
        return None
    name, unit, ac, dc = FUNCTIONS[func]
    ac |= bool(st1 & 0x01)
    dc |= bool(st1 & 0x02)
    auto, manual, sign = bool(st2 & 0x01), bool(st2 & 0x02), bool(st2 & 0x04)
    if auto and manual:
        return None

    exp = EXPONENTS[func][rng]
    if digits == b"::0<:":
        value = float("inf")
    elif digits == b":<0::":
        value = float("-inf")
    else:
        count = 4 if digits[4:5] == b":" else 5
        if not digits[:count].isdigit():
            return None
        value = int(digits[:count]) * (10 if count == 4 else 1)
        value = -value if sign else value
        value *= 10.0**exp

    if func == 12 and sign:
        name, unit = "Duty cycle", "%"

    return {
        "value": value,
        "unit": unit,
        "function": name,
        "ac": ac,
        "dc": dc,
        "auto": auto,
        "digits": max(0, -exp),
    }


class Framer:
    """Split UART byte stream into CRLF terminated packets."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while (i := self.buf.find(b"\n")) >= 0:
            pkt, self.buf = bytes(self.buf[: i + 1]), self.buf[i + 1 :]
            if len(pkt) == PACKET_LEN:
                out.append(pkt)
        return out


def packets(link):
    framer = Framer()
    while True:
        yield from framer.feed(link.read())


def fmt(m):
    flags = " ".join(f for f, on in (("AC", m["ac"]), ("DC", m["dc"]), ("AUTO", m["auto"])) if on)
    v = m["value"]
    vs = "OL" if v in (float("inf"), float("-inf")) else f"{v:.{m['digits']}f}"
    return f"{vs} {m['unit']}  [{m['function']}{' ' + flags if flags else ''}]"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-n", "--count", type=int, default=0, help="stop after N samples (0 = forever)")
    ap.add_argument("--csv", metavar="FILE", help="append samples to CSV file")
    ap.add_argument("--raw", action="store_true", help="print raw packets")
    args = ap.parse_args()

    try:
        link = Ch9325()
    except (OSError, RuntimeError) as e:
        sys.exit(f"Error: {e}")

    writer = None
    if args.csv:
        f = open(args.csv, "a", newline="")
        writer = csv.writer(f)
        if f.tell() == 0:
            writer.writerow(["time", "value", "unit", "function", "ac", "dc", "auto"])

    n = 0
    try:
        for pkt in packets(link):
            m = decode(pkt)
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            if args.raw:
                print(ts, pkt)
            if m is None:
                print(ts, "invalid packet:", pkt, file=sys.stderr)
                continue
            print(ts, fmt(m), flush=True)
            if writer:
                writer.writerow([ts, m["value"], m["unit"], m["function"], m["ac"], m["dc"], m["auto"]])
                f.flush()
            n += 1
            if args.count and n >= args.count:
                break
    except KeyboardInterrupt:
        pass
    except OSError as e:
        sys.exit(f"Error: {e} (cable disconnected? see README: Chrome)")
    finally:
        link.close()


if __name__ == "__main__":
    main()
