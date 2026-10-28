"""Build land.bin, the coastline the widget draws its map from.

Natural Earth (public domain, naturalearthdata.com) 1:10m land, minor islands
and the larger lakes, simplified with Douglas-Peucker to about 650 m, quantised
to 1/1000 of a degree and packed as zigzag varints, delta-coded, zlib'd. The
widget reads the file once per process and projects the rings it needs.

Run from the repo root, with network:

    python tools/pack_land.py

It downloads the three GeoJSON files to tools/out/ (kept out of the tarball)
and writes land.bin beside server.py. The output is deterministic for a given
Natural Earth revision.
"""

from __future__ import annotations

import json
import math
import struct
import sys
import urllib.request
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
TARGET = HERE.parent / "land.bin"

BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
LAYERS = [
    ("ne_10m_land.geojson", 0),  # kind 0: land
    ("ne_10m_minor_islands.geojson", 0),
    ("ne_10m_lakes.geojson", 1),  # kind 1: water on land
]

TOLERANCE_DEG = 0.006  # degrees, roughly 650 m at the equator
QUANT = 1000  # units per degree, about 110 m
LAKE_MAX_SCALERANK = 5  # the lakes that show at 1:10m small scales


def _download(name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    if not path.exists():
        print("downloading", name)
        req = urllib.request.Request(
            BASE + name,
            headers={"User-Agent": "tesserae-widget-quakes-nearby/pack_land"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            path.write_bytes(resp.read())
    return path


def _dp(points: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Douglas-Peucker, iterative so a 100k point ring does not hit the
    recursion limit."""
    n = len(points)
    if n < 3:
        return points
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = points[a]
        bx, by = points[b]
        dx, dy = bx - ax, by - ay
        seg = math.hypot(dx, dy)
        best, best_i = -1.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            if seg == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / seg
            if d > best:
                best, best_i = d, i
        if best > tol:
            keep[best_i] = True
            stack.append((a, best_i))
            stack.append((best_i, b))
    return [p for p, k in zip(points, keep) if k]


def _zigzag(v: int) -> int:
    return (v << 1) ^ (v >> 63)


def _varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _rings(geometry: dict) -> list[list[list[float]]]:
    t = geometry["type"]
    if t == "Polygon":
        return [geometry["coordinates"][0]]
    if t == "MultiPolygon":
        return [poly[0] for poly in geometry["coordinates"]]
    return []


def main() -> int:
    body = bytearray()
    total_pts = 0
    total_rings = 0
    for name, kind in LAYERS:
        data = json.loads(_download(name).read_text(encoding="utf-8"))
        for feature in data["features"]:
            props = feature.get("properties") or {}
            if kind == 1 and int(props.get("scalerank") or 0) > LAKE_MAX_SCALERANK:
                continue
            for ring in _rings(feature["geometry"]):
                pts = [(float(p[0]), float(p[1])) for p in ring]
                if pts and pts[0] == pts[-1]:
                    pts = pts[:-1]
                pts = _dp(pts, TOLERANCE_DEG)
                q = []
                last = None
                for lon, lat in pts:
                    c = (int(round(lon * QUANT)), int(round(lat * QUANT)))
                    if c != last:
                        q.append(c)
                        last = c
                if len(q) < 3:
                    continue
                total_rings += 1
                total_pts += len(q)
                body += _varint(kind)
                body += _varint(len(q))
                px, py = 0, 0
                for x, y in q:
                    body += _varint(_zigzag(x - px))
                    body += _varint(_zigzag(y - py))
                    px, py = x, y
    packed = zlib.compress(bytes(body), 9)
    header = b"TQL1" + struct.pack("<II", total_rings, QUANT)
    TARGET.write_bytes(header + packed)
    print(
        f"{total_rings} rings, {total_pts} points, {len(body)} raw, {len(packed)} packed -> {TARGET}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
