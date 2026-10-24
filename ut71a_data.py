"""Recording storage, incremental statistics and level-of-detail data for UT71A Logger.

Everything here is O(1) amortized per appended sample and O(log N) per query, so the GUI
stays responsive with 10^7-10^8 samples.
"""
import csv
import io
import json
import math
import os
import zipfile
from datetime import datetime

import numpy as np

CSV_FIELDS = ["time_s", "timestamp", "value", "unit", "function", "ac", "dc", "auto"]
PROJECT_FORMAT = "ut71a-logger-project"
PROJECT_VERSION = 2
LOD_FACTOR = 16  # samples per block in the min/max pyramid

FLAG_AC, FLAG_DC, FLAG_AUTO = 1, 2, 4


class Buf:
    """Growable 1-D numpy array."""

    def __init__(self, dtype, capacity=1024):
        self.a = np.empty(capacity, dtype)
        self.n = 0

    def __len__(self):
        return self.n

    @property
    def view(self):
        return self.a[: self.n]

    def _reserve(self, m):
        if m > len(self.a):
            new = np.empty(max(m, 2 * len(self.a)), self.a.dtype)
            new[: self.n] = self.a[: self.n]
            self.a = new

    def append(self, x):
        self._reserve(self.n + 1)
        self.a[self.n] = x
        self.n += 1

    def extend(self, x):
        x = np.asarray(x, self.a.dtype)
        m = self.n + len(x)
        self._reserve(m)
        self.a[self.n : m] = x
        self.n = m

    def last(self, default=0):
        return self.a[self.n - 1] if self.n else default

    def clear(self):
        self.n = 0


class Codes:
    """Small string table stored as uint8 codes."""

    def __init__(self, names=()):
        self.names = list(names)
        self.index = {n: i for i, n in enumerate(self.names)}

    def code(self, name):
        if name not in self.index:
            self.index[name] = len(self.names)
            self.names.append(name)
        return self.index[name]


class Recording:
    def __init__(self):
        self.clear()

    def clear(self):
        self.t0 = None
        self._t = Buf(np.float64)
        self._v = Buf(np.float32)
        self._unit = Buf(np.uint8)
        self._func = Buf(np.uint8)
        self._flags = Buf(np.uint8)
        self.units = Codes()
        self.funcs = Codes()

    def __len__(self):
        return self._t.n

    @property
    def t(self):
        return self._t.view

    @property
    def v(self):
        return self._v.view

    @property
    def unit_codes(self):
        return self._unit.view

    def append(self, epoch, value, unit, func, ac, dc, auto, t=None):
        if self.t0 is None:
            self.t0 = epoch if t is None else epoch - t
        self._t.append(epoch - self.t0 if t is None else t)
        self._v.append(value)
        self._unit.append(self.units.code(unit))
        self._func.append(self.funcs.code(func))
        self._flags.append(FLAG_AC * bool(ac) | FLAG_DC * bool(dc) | FLAG_AUTO * bool(auto))
        return len(self) - 1

    def sample(self, i):
        """(t, epoch, value, unit, function, ac, dc, auto) of sample i."""
        f = int(self._flags.a[i])
        t = float(self._t.a[i])
        return (t, self.t0 + t, float(self._v.a[i]), self.units.names[self._unit.a[i]],
                self.funcs.names[self._func.a[i]], bool(f & FLAG_AC), bool(f & FLAG_DC), bool(f & FLAG_AUTO))

    def unit(self, i):
        return self.units.names[self._unit.a[i]]

    def row(self, i):
        t, epoch, v, unit, func, ac, dc, auto = self.sample(i)
        ts = datetime.fromtimestamp(epoch).isoformat(timespec="milliseconds")
        return [f"{t:.3f}", ts, f"{v:.6g}", unit, func, int(ac), int(dc), int(auto)]

    # CSV

    def save_csv(self, path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(CSV_FIELDS)
            for i in range(len(self)):
                w.writerow(self.row(i))

    def load_csv(self, path):
        """Load CSV written by this app or by `ut71a.py --csv`."""
        self.clear()
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("timestamp"):
                    epoch = datetime.fromisoformat(r["timestamp"]).timestamp()
                else:
                    epoch = datetime.strptime(r["time"], "%Y-%m-%d %H:%M:%S").timestamp()
                t = float(r["time_s"]) if r.get("time_s") else None
                self.append(epoch, float(r["value"]), r["unit"], r["function"],
                            parse_bool(r["ac"]), parse_bool(r["dc"]), parse_bool(r["auto"]), t)
        if not len(self):
            raise ValueError("file contains no samples")

    # project arrays

    def arrays(self):
        return {"t": self.t, "v": self.v, "unit": self._unit.view, "func": self._func.view,
                "flags": self._flags.view}

    def from_arrays(self, t0, arrays, units, funcs):
        self.clear()
        n = len(arrays["t"])
        if not n:
            raise ValueError("project contains no samples")
        if any(len(a) != n for a in arrays.values()):
            raise ValueError("inconsistent array lengths")
        self.t0 = float(t0)
        self.units, self.funcs = Codes(units), Codes(funcs)
        self._t.extend(arrays["t"])
        self._v.extend(arrays["v"])
        self._unit.extend(arrays["unit"])
        self._func.extend(arrays["func"])
        self._flags.extend(arrays["flags"])


def parse_bool(s):
    return s.strip().lower() in ("1", "true", "yes")


class Series:
    """Values of one display unit (others are NaN) with prefix sums and a min/max pyramid."""

    def __init__(self):
        self.reset(None)

    def reset(self, unit_code):
        self.code = unit_code
        self.n = 0
        self.y = Buf(np.float32)
        self.cnt = Buf(np.int32)    # cnt[i] = finite samples among 0..i-1
        self.area = Buf(np.float64)  # area[i] = integral over segments 0..i-1 (segment k joins k, k+1)
        self.span = Buf(np.float64)  # same for the duration of valid segments
        self.cnt.append(0)
        self.levels = []  # [(min Buf, max Buf)], level L has blocks of LOD_FACTOR**(L+1) samples

    def sync(self, rec):
        """Process samples appended to `rec` since the last call."""
        n0, n1 = self.n, len(rec)
        if n1 <= n0:
            return
        v = rec.v[n0:n1]
        y = np.where((rec.unit_codes[n0:n1] == self.code) & np.isfinite(v), v, np.nan).astype(np.float32)
        self.y.extend(y)
        fin = np.isfinite(y)
        self.cnt.extend(self.cnt.last() + np.cumsum(fin, dtype=np.int64))

        if n0 == 0:
            self.area.append(0.0)
            self.span.append(0.0)
        s0 = max(n0 - 1, 0)
        yy = self.y.view[s0:n1].astype(np.float64)
        dt = np.diff(rec.t[s0:n1])
        ok = np.isfinite(yy[:-1]) & np.isfinite(yy[1:])
        self.area.extend(self.area.last() + np.cumsum(np.where(ok, (yy[:-1] + yy[1:]) * 0.5 * dt, 0.0)))
        self.span.extend(self.span.last() + np.cumsum(np.where(ok, dt, 0.0)))
        self.n = n1
        self._update_levels()

    def _update_levels(self):
        F = LOD_FACTOR
        lo = hi = self.y.view
        L = 0
        while len(lo) >= F:
            if L == len(self.levels):
                self.levels.append((Buf(np.float32), Buf(np.float32)))
            mins, maxs = self.levels[L]
            done, full = len(mins), len(lo) // F
            if full > done:
                mins.extend(np.fmin.reduce(lo[done * F : full * F].reshape(-1, F), axis=1))
                maxs.extend(np.fmax.reduce(hi[done * F : full * F].reshape(-1, F), axis=1))
            lo, hi = mins.view, maxs.view
            L += 1

    def minmax(self, i0, i1):
        """(min, max) of finite values in samples i0..i1-1, or None."""
        F = LOD_FACTOR
        lo_v, hi_v = math.inf, -math.inf
        lo = hi = self.y.view
        L = -1
        while i0 < i1:
            a, b = -(-i0 // F) * F, (i1 // F) * F
            if a >= b or L + 1 >= len(self.levels):
                a = b = i1
            if a > i0:
                lo_v = min(lo_v, np.fmin.reduce(lo[i0:a]))
                hi_v = max(hi_v, np.fmax.reduce(hi[i0:a]))
            if i1 > b:
                lo_v = min(lo_v, np.fmin.reduce(lo[b:i1]))
                hi_v = max(hi_v, np.fmax.reduce(hi[b:i1]))
            if a == b == i1:
                break
            L += 1
            lo, hi = self.levels[L][0].view, self.levels[L][1].view
            i0, i1 = a // F, b // F
        if not (math.isfinite(lo_v) and math.isfinite(hi_v)):
            return None
        return float(lo_v), float(hi_v)

    def stats(self, t, i0, i1, unit):
        """Statistics of samples i0..i1-1; `t` is the recording time array."""
        i0, i1 = max(i0, 0), min(i1, self.n)
        if i1 <= i0:
            return None
        n = int(self.cnt.a[i1] - self.cnt.a[i0])
        if not n:
            return None
        dur = float(t[i1 - 1] - t[i0])
        area = float(self.area.a[i1 - 1] - self.area.a[i0])
        span = float(self.span.a[i1 - 1] - self.span.a[i0])
        avg = area / span if span > 0 else float(np.nanmean(self.y.view[i0:i1]))
        mn, mx = self.minmax(i0, i1)
        return {
            "n": n,
            "dur": dur,
            "rate": (n - 1) / dur if dur > 0 else None,
            "min": mn,
            "max": mx,
            "avg": avg,
            "charge": area / 3600 if unit == "A" else None,  # Ah
        }

    def envelope(self, t, i0, i1, max_points):
        """Plot data for samples i0..i1-1 with at most ~max_points points.

        Returns (x, y, raw). When decimated, each block contributes its min and max,
        so peaks stay visible.
        """
        n = i1 - i0
        if n <= max_points or not self.levels:
            return t[i0:i1], self.y.view[i0:i1], True
        L = 0
        while L + 1 < len(self.levels) and 2 * n / LOD_FACTOR ** (L + 1) > max_points:
            L += 1
        xs, ys = self._envelope(t, i0, i1, L)
        return np.concatenate(xs), np.concatenate(ys), False

    def _envelope(self, t, i0, i1, L):
        if L < 0 or i1 <= i0:
            return [t[i0:i1]], [self.y.view[i0:i1]]
        bs = LOD_FACTOR ** (L + 1)
        mins, maxs = self.levels[L]
        b0, b1 = i0 // bs, min(i1 // bs, len(mins))
        if b1 <= b0:
            return self._envelope(t, i0, i1, L - 1)
        x = np.repeat(t[b0 * bs : b1 * bs : bs], 2)
        y = np.empty(2 * (b1 - b0), np.float32)
        y[0::2] = mins.view[b0:b1]
        y[1::2] = maxs.view[b0:b1]
        tx, ty = self._envelope(t, b1 * bs, i1, L - 1)
        return [x, *tx], [y, *ty]


def find_active_regions(t, y, threshold=0.2, merge_samples=3, min_samples=2):
    """Find bursts of activity above a quiet baseline.

    Returns [(start, end)] times. The level is baseline + `threshold` * (peak - baseline);
    gaps of up to `merge_samples` samples are merged and each region is widened by one
    sample on both sides so that rising and falling edges are included.
    Returns [] when the signal has no clear baseline/peak contrast.
    """
    ok = np.isfinite(y)
    if ok.sum() < 5:
        return []
    t, y = np.asarray(t)[ok], np.asarray(y, np.float64)[ok]
    base, peak = float(np.percentile(y, 10)), float(y.max())
    span = peak - base
    level = base + threshold * span
    quiet = y[y <= level]
    noise = 1.4826 * float(np.median(np.abs(quiet - np.median(quiet)))) if len(quiet) else 0.0
    if span <= max(0.5 * abs(base), 10 * noise) or span <= 0:
        return []

    edges = np.flatnonzero(np.diff(np.concatenate(([0], (y > level).astype(np.int8), [0]))))
    runs = [[s, e] for s, e in zip(edges[0::2], edges[1::2])]  # e is exclusive
    merged = []
    for s, e in runs:
        if merged and s - merged[-1][1] <= merge_samples:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    last = len(t) - 1
    return [(float(t[max(s - 1, 0)]), float(t[min(e, last)])) for s, e in merged if e - s >= min_samples]


# projects

def save_project(path, rec, selections, supply_v):
    meta = {
        "format": PROJECT_FORMAT,
        "version": PROJECT_VERSION,
        "saved": datetime.now().isoformat(timespec="seconds"),
        "supply_v": supply_v,
        "t0": rec.t0,
        "units": rec.units.names,
        "functions": rec.funcs.names,
        "selections": selections,
    }
    tmp = f"{path}.tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        z.writestr("project.json", json.dumps(meta, indent=1))
        for name, arr in rec.arrays().items():
            with z.open(f"{name}.npy", "w", force_zip64=True) as f:
                np.save(f, arr)
    os.replace(tmp, path)


def load_project(path, rec):
    """Fill `rec` from a project file; returns (selections, supply_v)."""
    if not zipfile.is_zipfile(path):
        return _load_project_v1(path, rec)
    with zipfile.ZipFile(path) as z:
        meta = json.loads(z.read("project.json"))
        _check_meta(meta)
        arrays = {}
        for name in ("t", "v", "unit", "func", "flags"):
            with z.open(f"{name}.npy") as f:
                arrays[name] = np.load(io.BytesIO(f.read()), allow_pickle=False)
    rec.from_arrays(meta["t0"], arrays, meta["units"], meta["functions"])
    return meta.get("selections", []), meta.get("supply_v")


def _check_meta(meta):
    if meta.get("format") != PROJECT_FORMAT:
        raise ValueError("not a UT71A Logger project")
    if meta.get("version", 0) > PROJECT_VERSION:
        raise ValueError(f"project version {meta['version']} is newer than this application")


def _load_project_v1(path, rec):
    """Version 1 projects were plain JSON with per-sample lists."""
    with open(path, encoding="utf-8") as f:
        meta = json.load(f)
    _check_meta(meta)
    d = meta["recording"]
    rec.clear()
    for t, epoch, v, unit, func, ac, dc, auto in zip(d["time_s"], d["epoch"], d["value"], d["unit"],
                                                      d["function"], d["ac"], d["dc"], d["auto"]):
        rec.append(float(epoch), float(v), unit, func, ac, dc, auto, float(t))
    if not len(rec):
        raise ValueError("project contains no samples")
    return meta.get("selections", []), meta.get("supply_v")
