"""rain_radar: the rain radar around the panel, drawn as a printed map.

Runs on the Tesserae side. ``fetch()`` returns a JSON-serialisable dict, or
{"error": "friendly message"}; it never raises.

Radar comes from a public tile service, fetched with urllib and cached under
``data_dir``; the coast is read from a file beside this module:

* RainViewer's weather-maps index lists the last two hours of radar frames at
  ten minute steps, each a path to a tile set. The public tier serves tiles
  up to zoom 7 only, so radar is read at zoom 7 with 512 px tiles (about half
  a kilometre a pixel) and scaled onto the map. The server ignores the colour
  scheme number and answers in Universal Blue, whose smoothed tiles still use
  one exact colour per dBZ step, so a pixel's reflectivity is a table lookup.
* The coast needs no network: Natural Earth 1:10m land, minor islands and
  lakes (public domain) are packed into land.bin beside this file. The rings
  in view are projected with the same Web Mercator maths, clipped, and sent
  as SVG path data the client paints from the theme's tokens.

The radar layer is built with Pillow (imported inside functions; the widget
degrades to a notice without it): the view is a square covering 1.6 times the
range, the latest frame's reflectivity is classified into light, moderate and
heavy and drawn as stipple in three densities over a transparent background,
and the last hour becomes a strip of thumbnails the same way. The headline is worked out from
the nowcast frames when the index carries any, and otherwise from the drift
of the last half hour's echoes (a cross-correlation of two frames), applied
to the latest frame and sampled within ten kilometres of home.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import struct
import threading
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any

WIDGET = "rain_radar"
VERSION = "0.1.0"
USER_AGENT = f"tesserae/{WIDGET}/{VERSION} (+https://github.com/dmellok/tesserae-widget-rain-radar)"

INDEX_URL = "https://api.rainviewer.com/public/weather-maps.json"
RADAR_ZOOM = 7  # the public tier's ceiling
RADAR_TILE = 512  # px; a zoom 7 tile at 512 px is a zoom 8 tile at 256 px
RADAR_OPTIONS = "1_0"  # smoothed, snow drawn in the rain colours
TILE = 256
TIMEOUT = 15

REQUEST_BUDGET = 40  # tile requests per render; cache hits are free
INDEX_TTL = 120
RADAR_TTL = 2 * 3600
RENDER_TTL = 3600
STALE_TTL = 3 * 3600

MIN_SIDE, MAX_SIDE = 320, 768  # the square the map covers, px
VIEW_SPAN = 1.6  # the square's side, in multiples of the range

LIGHT_DBZ, MODERATE_DBZ, HEAVY_DBZ = 10, 28, 40
HOME_KM = 10.0  # the disc the headline is about
RAIN_FRACTION = 0.04  # of the disc covered before it counts as raining
RANGE_RING_KM = {50: 10, 100: 25, 200: 50}
STEP_MIN = 10
FORECAST_STEPS = 6
THUMB_PX = 160

COLOURS = {
    "teal": {"light": "#2a7f86", "heavy": "#1b3f45", "tint": "#a6cdd1"},
    "ink": {"light": "#1a1a1a", "heavy": "#000000", "tint": "#b9b9b9"},
}
_HERE = os.path.dirname(os.path.abspath(__file__))
_LAND_LOCK = threading.Lock()
_LAND: list | None = None
_PATH_CACHE: dict[tuple, dict[str, str]] = {}

# RainViewer's Universal Blue rain column, one RGBA hex per dBZ from -32 to 95.
# From https://www.rainviewer.com/api/color-schemes.html (the only scheme the
# tile server serves at the time of writing, whichever number is asked for).
_UNIVERSAL_BLUE_HEX = (
    "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000000000000000000000006361591466635a19"
    "69665c1e6c685d246f6b5f29726e612e75706234787364397c75653e7f786744827b6949857d6a4e88806c548b826d59"
    "8e856f5e928871649e93756eaa9e7978b6a97e82c2b4828ccec08796d2c48ba0d6c88faadacc93b4ded097be88ddeeff"
    "6cd1ebff51c5e8ff36bae5ff1baee2ff00a3e0ff009ad5ff0091caff0088bfff007fb4ff0077aaff0070a3ff00699cff"
    "006295ff005b8eff005588ff005180ff004e78ff004a70ff004768ffffee00ffffe000ffffd200ffffc500ffffb700ff"
    "ffaa00ffff9f00ffff9500ffff8b00ffff8100ffff4400fff23600ffe62800ffd91b00ffcd0d00ffc10000ffa80000ff"
    "8f0000ff760000ff5d0000ffffaaffffff9fffffff95ffffff8bffffff81ffffff77ffffff6cffffff62ffffff58ffff"
    "ff4effffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff00ff00ff"
    "00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff"
    "00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff"
)
UNIVERSAL_BLUE = tuple(
    _UNIVERSAL_BLUE_HEX[i : i + 8] for i in range(0, len(_UNIVERSAL_BLUE_HEX), 8)
)

# A reflectivity raster stores dBZ + 32 in an 8-bit pixel, 0 for no echo.
# Anything under LIGHT_DBZ (drizzle, clutter, the translucent sand ramp) is
# dropped at classification time so it never counts as rain.
DBZ_OFFSET = 32
V_LIGHT = LIGHT_DBZ + DBZ_OFFSET
V_MODERATE = MODERATE_DBZ + DBZ_OFFSET
V_HEAVY = HEAVY_DBZ + DBZ_OFFSET


def _colour_table() -> dict[tuple[int, int, int], int]:
    """Opaque-ish RGB -> dBZ for every drawn entry of the scheme."""
    out: dict[tuple[int, int, int], int] = {}
    for i, hx in enumerate(UNIVERSAL_BLUE):
        r, g, b, a = (int(hx[k : k + 2], 16) for k in (0, 2, 4, 6))
        if a == 0:
            continue
        out.setdefault((r, g, b), i - DBZ_OFFSET)
    return out


COLOUR_TO_DBZ = _colour_table()


# ---------------------------------------------------------------- tile maths


def tile_xy(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Fractional Web Mercator tile coordinates of a point."""
    lat = max(-85.05, min(85.05, lat))
    n = 2.0**zoom
    x = (lon + 180.0) / 360.0 * n
    la = math.radians(lat)
    y = (1.0 - math.log(math.tan(la) + 1.0 / math.cos(la)) / math.pi) / 2.0 * n
    return x, y


def tile_index(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    x, y = tile_xy(lat, lon, zoom)
    return int(math.floor(x)), int(math.floor(y))


def metres_per_pixel(lat: float, zoom: int, tile_px: int = TILE) -> float:
    return 156543.03392 * math.cos(math.radians(lat)) / (2.0**zoom) / (tile_px / TILE)


def plan_view(lat: float, lon: float, range_km: int, long_side: int) -> dict[str, Any]:
    """Pick a zoom and the square of map pixels the cell will show.

    The square covers ``VIEW_SPAN`` times the range so a 16:10 cell shows the
    range across its short side under object-fit: cover. The side is aimed at
    about four fifths of the cell's long side (so the stipple is drawn near
    the size it is displayed at), capped so the base block is at most 4x4
    tiles, and the radar tiles under it are at most 2x2.
    """
    target = max(MIN_SIDE, min(MAX_SIDE, int(long_side * 0.8) or MIN_SIDE))
    span_m = VIEW_SPAN * range_km * 1000.0
    wanted_mpp = span_m / target
    zoom = int(
        round(math.log2(156543.03392 * math.cos(math.radians(lat)) / wanted_mpp))
    )
    zoom = max(4, min(12, zoom))
    side = int(round(span_m / metres_per_pixel(lat, zoom)))
    while side > MAX_SIDE and zoom > 4:
        zoom -= 1
        side = int(round(span_m / metres_per_pixel(lat, zoom)))
    mpp = metres_per_pixel(lat, zoom)
    fx, fy = tile_xy(lat, lon, zoom)
    cxp, cyp = fx * TILE, fy * TILE  # world pixels at this zoom
    half = side / 2.0
    x0, x1 = int(math.floor((cxp - half) / TILE)), int(math.floor((cxp + half) / TILE))
    y0, y1 = int(math.floor((cyp - half) / TILE)), int(math.floor((cyp + half) / TILE))
    # radar tiles are zoom 7 at 512 px: 2 ** (zoom - 8) world pixels per radar pixel
    rscale = 2.0 ** (zoom - 8)
    rtile = RADAR_TILE * rscale  # one radar tile, in world pixels at this zoom
    rx0, rx1 = (
        int(math.floor((cxp - half) / rtile)),
        int(math.floor((cxp + half) / rtile)),
    )
    ry0, ry1 = (
        int(math.floor((cyp - half) / rtile)),
        int(math.floor((cyp + half) / rtile)),
    )
    return {
        "zoom": zoom,
        "side": side,
        "mpp": mpp,
        "px_per_km": 1000.0 / mpp,
        "cx": cxp,
        "cy": cyp,
        "base": (x0, y0, x1, y1),
        "radar": (rx0, ry0, rx1, ry1),
        "rscale": rscale,
        "centre_tile": tile_index(lat, lon, zoom),
    }


# ---------------------------------------------------------------- network


def _pil():
    try:
        from PIL import Image, ImageChops, ImageDraw, ImageFilter
    except ImportError:
        return None
    return Image, ImageChops, ImageDraw, ImageFilter


def _get(url: str, timeout: int = TIMEOUT) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


class TileStore:
    """Tiles for one render: what is on disk, and what may still be fetched.

    Jobs are registered in priority order (base map, the latest frame, the
    nowcast, then the past frames newest first); ``prefetch`` reads what the
    cache has, spends the request budget on the first missing tiles, and
    fetches those in parallel. Cache hits are free. Tiles past the budget
    are simply absent this render and come in on the next one.
    """

    def __init__(
        self, data_dir: Path, limit: int = REQUEST_BUDGET, workers: int = 8
    ) -> None:
        self.data_dir = data_dir
        self.limit = limit
        self.workers = workers
        self.used = 0
        self.failed = 0
        self.jobs: list[tuple[Path, str, int]] = []
        self.bytes: dict[Path, bytes | None] = {}

    def want(self, path: Path, url: str, ttl: int) -> None:
        if path not in self.bytes and all(j[0] != path for j in self.jobs):
            self.jobs.append((path, url, ttl))

    def _fresh(self, path: Path, ttl: int) -> bytes | None:
        try:
            if path.exists() and time.time() - path.stat().st_mtime < ttl:
                return path.read_bytes()
        except OSError:
            pass
        return None

    def prefetch(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        to_fetch: list[tuple[Path, str]] = []
        for path, url, ttl in self.jobs:
            raw = self._fresh(path, ttl)
            if raw is not None:
                self.bytes[path] = raw
            elif self.used < self.limit:
                self.used += 1
                to_fetch.append((path, url))
            else:
                self.bytes[path] = None
        self.jobs = []
        if not to_fetch:
            return

        def one(job: tuple[Path, str]) -> tuple[Path, bytes | None]:
            path, url = job
            raw = _get(url)
            if raw is not None:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(raw)
                except OSError:
                    pass
            return path, raw

        with ThreadPoolExecutor(max_workers=min(self.workers, len(to_fetch))) as pool:
            for path, raw in pool.map(one, to_fetch):
                self.bytes[path] = raw
                if raw is None:
                    self.failed += 1

    def get(self, path: Path) -> bytes | None:
        return self.bytes.get(path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def load_index(data_dir: Path, fresh: bool = False) -> dict[str, Any] | None:
    """RainViewer's frame index, kept two minutes so editor renders share it."""
    path = data_dir / "index.json"
    if not fresh:
        try:
            if path.exists() and time.time() - path.stat().st_mtime < INDEX_TTL:
                cached = _read_json(path)
                if isinstance(cached, dict):
                    return cached
        except OSError:
            pass
    raw = _get(INDEX_URL)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("radar"), dict):
        return None
    _write_json(path, data)
    return data


def _prune(folder: Path, older_than: int) -> None:
    try:
        now = time.time()
        for p in folder.glob("*"):
            if p.is_file() and now - p.stat().st_mtime > older_than:
                p.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- rasters


def dbz_raster(png: bytes, pil=None):
    """A radar tile -> 'L' image of dBZ + 32 (0 where there is no echo).

    Returns None for a tile that is not a radar tile: the public tier answers
    an opaque "Zoom Level Not Supported" card past zoom 7, and any pixel whose
    colour is not in the scheme means the tile is not one to read.
    """
    pil = pil or _pil()
    if pil is None:
        return None
    Image, ImageChops, _ImageDraw, _ImageFilter = pil
    try:
        tile = Image.open(io.BytesIO(png)).convert("RGBA")
    except (OSError, ValueError):
        return None
    colours = tile.getcolors(4096)
    if colours is None:
        return None
    lut_by_colour: dict[tuple[int, int, int, int], int] = {}
    for _count, rgba in colours:
        if rgba[3] == 0:
            lut_by_colour[rgba] = 0
            continue
        dbz = COLOUR_TO_DBZ.get(rgba[:3])
        if dbz is None:
            return None
        lut_by_colour[rgba] = max(0, dbz + DBZ_OFFSET) if dbz >= LIGHT_DBZ else 0
    if all(v == 0 for v in lut_by_colour.values()):
        return Image.new("L", tile.size, 0)
    # Quantise to the scheme's own colours, then map palette index -> value.
    used = [rgba for rgba in lut_by_colour if lut_by_colour[rgba] > 0]
    pal = Image.new("P", (1, 1))
    flat: list[int] = []
    for rgba in used:
        flat.extend(rgba[:3])
    flat.extend(used[-1][:3] * (256 - len(used)))
    pal.putpalette(flat)
    q = tile.convert("RGB").quantize(palette=pal, dither=Image.Dither.NONE)
    table = bytes(lut_by_colour[used[i]] if i < len(used) else 0 for i in range(256))
    values = Image.frombytes("L", tile.size, q.tobytes().translate(table))
    alpha = tile.getchannel("A").point(lambda a: 255 if a > 0 else 0)
    return ImageChops.multiply(values, alpha)


def _stitch(
    view: dict[str, Any],
    tiles: dict[tuple[int, int], Any],
    box: tuple,
    tile_px: int,
    mode: str,
    fill,
    pil,
):
    """Paste a block of tiles and cut the view square out of it."""
    Image = pil[0]
    x0, y0, x1, y1 = box
    sheet = Image.new(mode, ((x1 - x0 + 1) * tile_px, (y1 - y0 + 1) * tile_px), fill)
    for (tx, ty), im in tiles.items():
        if im is not None:
            sheet.paste(im, ((tx - x0) * tile_px, (ty - y0) * tile_px))
    return sheet


def radar_jobs(
    view: dict[str, Any], host: str, frame_path: str, data_dir: Path
) -> list[tuple[int, int, Path, str]]:
    rx0, ry0, rx1, ry1 = view["radar"]
    key = (
        frame_path.rstrip("/").rsplit("/", 1)[-1]
        or hashlib.md5(frame_path.encode()).hexdigest()[:12]
    )
    n = 2**RADAR_ZOOM
    out = []
    for ty in range(ry0, ry1 + 1):
        for tx in range(rx0, rx1 + 1):
            if not (0 <= ty < n):
                continue
            wx = tx % n
            path = data_dir / "tiles" / "radar" / f"{key}_{wx}_{ty}.png"
            url = f"{host}{frame_path}/{RADAR_TILE}/{RADAR_ZOOM}/{wx}/{ty}/2/{RADAR_OPTIONS}.png"
            out.append((tx, ty, path, url))
    return out


def radar_raster(
    view: dict[str, Any], host: str, frame_path: str, store: TileStore, pil
):
    """One frame's reflectivity over the view square, as an 'L' raster."""
    Image = pil[0]
    rx0, ry0, _rx1, _ry1 = view["radar"]
    tiles: dict[tuple[int, int], Any] = {}
    got = 0
    for tx, ty, path, _url in radar_jobs(view, host, frame_path, store.data_dir):
        raw = store.get(path)
        im = dbz_raster(raw, pil) if raw else None
        if im is not None:
            got += 1
        tiles[(tx, ty)] = im
    if not got:
        return None
    sheet = _stitch(view, tiles, view["radar"], RADAR_TILE, "L", 0, pil)
    side = view["side"]
    s = view["rscale"]
    left = (view["cx"] - side / 2.0) / s - rx0 * RADAR_TILE
    top = (view["cy"] - side / 2.0) / s - ry0 * RADAR_TILE
    return sheet.transform(
        (side, side),
        Image.Transform.EXTENT,
        (left, top, left + side / s, top + side / s),
        Image.Resampling.BILINEAR,
    )


# ---------------------------------------------------------------- coastline from land.bin


def _read_varints(buf: bytes):
    pos = 0
    n = len(buf)
    while pos < n:
        v = 0
        shift = 0
        while True:
            b = buf[pos]
            pos += 1
            v |= (b & 0x7F) << shift
            if not b & 0x80:
                break
            shift += 7
        yield v


def _unzigzag(v: int) -> int:
    return (v >> 1) ^ -(v & 1)


def _load_land() -> list[
    tuple[int, list[tuple[float, float]], tuple[float, float, float, float]]
]:
    """Decode land.bin once per process into (kind, ring, bbox) tuples;
    kind 0 is land, kind 1 water on land (lakes). Natural Earth 1:10m,
    packed by tools/pack_land.py."""
    global _LAND
    if _LAND is not None:
        return _LAND
    with _LAND_LOCK:
        if _LAND is not None:
            return _LAND
        with open(os.path.join(_HERE, "land.bin"), "rb") as fh:
            raw = fh.read()
        if raw[:4] != b"TQL1":
            raise ValueError("land.bin has the wrong header")
        _count, quant = struct.unpack("<II", raw[4:12])
        it = _read_varints(zlib.decompress(raw[12:]))
        rings = []
        q = float(quant)
        try:
            while True:
                kind = next(it)
                n = next(it)
                pts = []
                x = y = 0
                minx = miny = 1e9
                maxx = maxy = -1e9
                for _ in range(n):
                    x += _unzigzag(next(it))
                    y += _unzigzag(next(it))
                    lon, lat = x / q, y / q
                    pts.append((lon, lat))
                    minx, maxx = min(minx, lon), max(maxx, lon)
                    miny, maxy = min(miny, lat), max(maxy, lat)
                rings.append((kind, pts, (minx, miny, maxx, maxy)))
        except StopIteration:
            pass
        _LAND = rings
        return rings


def _clip_poly(
    pts: list[tuple[float, float]], lo: float, hi: float
) -> list[tuple[float, float]]:
    """Sutherland-Hodgman clip of a closed polygon to the square [lo, hi]^2."""

    def clip_edge(poly, inside, intersect):
        out = []
        if not poly:
            return out
        prev = poly[-1]
        prev_in = inside(prev)
        for cur in poly:
            cur_in = inside(cur)
            if cur_in:
                if not prev_in:
                    out.append(intersect(prev, cur))
                out.append(cur)
            elif prev_in:
                out.append(intersect(prev, cur))
            prev, prev_in = cur, cur_in
        return out

    def x_at(p, q, x):
        t = (x - p[0]) / (q[0] - p[0])
        return (x, p[1] + (q[1] - p[1]) * t)

    def y_at(p, q, y):
        t = (y - p[1]) / (q[1] - p[1])
        return (p[0] + (q[0] - p[0]) * t, y)

    poly = clip_edge(pts, lambda p: p[0] >= lo, lambda p, q: x_at(p, q, lo))
    poly = clip_edge(poly, lambda p: p[0] <= hi, lambda p, q: x_at(p, q, hi))
    poly = clip_edge(poly, lambda p: p[1] >= lo, lambda p, q: y_at(p, q, lo))
    poly = clip_edge(poly, lambda p: p[1] <= hi, lambda p, q: y_at(p, q, hi))
    return poly


def land_rings(
    view: dict[str, Any], home_lat: float, home_lon: float
) -> list[tuple[int, list]]:
    """The land.bin rings in view, projected to the map square's pixels and
    clipped just outside it: (kind, points), kind 0 land and 1 lakes."""
    side = view["side"]
    z = view["zoom"]
    left = view["cx"] - side / 2.0
    top = view["cy"] - side / 2.0
    units_per_deg_lon = TILE * (2.0**z) / 360.0
    half_lon = (side / 2.0) / units_per_deg_lon * 1.1
    phi = math.radians(max(-85.0, min(85.0, home_lat)))
    half_lat = (side / 2.0) / (units_per_deg_lon / max(0.05, math.cos(phi))) * 1.3
    lat_lo, lat_hi = home_lat - half_lat, home_lat + half_lat
    out = []
    for kind, pts, (minx, miny, maxx, maxy) in _load_land():
        if maxy < lat_lo or miny > lat_hi:
            continue
        # Shift the ring by a multiple of 360 degrees so it sits the short
        # way round from home; a point-by-point wrap smears an island that
        # straddles the far side of the world across the map.
        centre = (minx + maxx) / 2.0
        shift = round((home_lon - centre) / 360.0) * 360.0
        if maxx + shift < home_lon - half_lon or minx + shift > home_lon + half_lon:
            continue
        poly = []
        for lon, lat in pts:
            fx, fy = tile_xy(lat, lon + shift, z)
            poly.append((fx * TILE - left, fy * TILE - top))
        clipped = _clip_poly(poly, -4.0, side + 4.0)
        if len(clipped) >= 3:
            out.append((kind, clipped))
    return out


def _ring_path(poly: list[tuple[float, float]], min_step: float = 0.4) -> str:
    """One closed subpath to a tenth of a pixel; points closer than
    min_step px to the last one kept are dropped."""
    out = []
    last = None
    for x, y in poly:
        if (
            last is not None
            and abs(x - last[0]) < min_step
            and abs(y - last[1]) < min_step
        ):
            continue
        out.append(f"{x:.1f} {y:.1f}")
        last = (x, y)
    if len(out) < 3:
        return ""
    return "M" + "L".join(out) + "Z"


def land_paths(
    view: dict[str, Any], home_lat: float, home_lon: float
) -> dict[str, str]:
    """SVG path data for the land and the lakes over the map square, in its
    pixels, from Natural Earth 1:10m (land.bin). The client paints them from
    the theme's tokens, so the coast is crisp at any size and no base map
    tiles are fetched."""
    key = (round(home_lat, 4), round(home_lon, 4), view["zoom"], view["side"])
    hit = _PATH_CACHE.get(key)
    if hit is not None:
        return hit
    paths = {0: [], 1: []}
    for kind, poly in land_rings(view, home_lat, home_lon):
        d = _ring_path(poly)
        if d:
            paths[kind].append(d)
    out = {"land": "".join(paths[0]), "water": "".join(paths[1])}
    if len(_PATH_CACHE) > 16:
        _PATH_CACHE.clear()
    _PATH_CACHE[key] = out
    return out


# ---------------------------------------------------------------- drawing


_PATTERNS: dict[tuple, Any] = {}


def _dots(side: int, pitch: int, dot: int, pil):
    """A stipple mask: one ``dot`` px square per ``pitch`` px cell, rows offset
    by half a cell. Squares, not discs: at two or three pixels a disc only
    rasterises as a plus sign, and a square dithers to a clean dot."""
    key = (side, pitch, dot)
    if key in _PATTERNS:
        return _PATTERNS[key]
    Image, _c, ImageDraw, _f = pil
    cell = Image.new("L", (pitch, pitch * 2), 0)
    d = ImageDraw.Draw(cell)
    o = (pitch - dot) // 2
    d.rectangle((o, o, o + dot - 1, o + dot - 1), fill=255)
    half = pitch // 2
    d.rectangle(
        (o + half, pitch + o, o + half + dot - 1, pitch + o + dot - 1), fill=255
    )
    d.rectangle(
        (o + half - pitch, pitch + o, o + half - pitch + dot - 1, pitch + o + dot - 1),
        fill=255,
    )
    row = Image.new("L", (side + pitch, pitch * 2), 0)
    for x in range(0, side + pitch, pitch):
        row.paste(cell, (x, 0))
    full = Image.new("L", (side + pitch, side + pitch * 2), 0)
    for y in range(0, side + pitch * 2, pitch * 2):
        full.paste(row, (0, y))
    out = full.crop((0, 0, side, side))
    if len(_PATTERNS) > 24:
        _PATTERNS.clear()
    _PATTERNS[key] = out
    return out


def _band(raster, lo: int, hi: int):
    table = bytes(255 if lo <= v < hi else 0 for v in range(256))
    return raster.point(list(table))


def _hex(c: str) -> tuple[int, int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), 255


def paint_rain(side: int, raster, colours: dict[str, str], pil, flat: bool = False):
    """The three bands as an RGBA layer over a transparent background:
    stippled for the map, flat tints for a thumbnail."""
    Image, ImageChops, _d, _f = pil
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    light = _band(raster, V_LIGHT, V_MODERATE)
    moderate = _band(raster, V_MODERATE, V_HEAVY)
    heavy = _band(raster, V_HEAVY, 256)
    if flat:
        canvas.paste(_hex(colours["tint"]), mask=light)
        canvas.paste(_hex(colours["light"]), mask=moderate)
        canvas.paste(_hex(colours["heavy"]), mask=heavy)
        return canvas
    canvas.paste(
        _hex(colours["light"]), mask=ImageChops.multiply(light, _dots(side, 6, 2, pil))
    )
    canvas.paste(
        _hex(colours["light"]),
        mask=ImageChops.multiply(moderate, _dots(side, 4, 3, pil)),
    )
    canvas.paste(_hex(colours["heavy"]), mask=heavy)
    return canvas


def _data_uri(im, pil, colors: int = 16) -> str:
    """An RGBA layer as a palette PNG with transparency, base64."""
    Image = pil[0]
    buf = io.BytesIO()
    try:
        q = im.quantize(
            colors=colors, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE
        )
        q.save(buf, "PNG", optimize=True)
    except (ValueError, OSError):
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# ---------------------------------------------------------------- reading the echoes


def disc_sample(
    raster, cx: float, cy: float, r_px: float, pil, disc=None
) -> dict[str, float]:
    """Fractions of a disc covered by each band."""
    Image, ImageChops, ImageDraw, _f = pil
    if disc is None:
        disc = Image.new("L", raster.size, 0)
        ImageDraw.Draw(disc).ellipse(
            (cx - r_px, cy - r_px, cx + r_px, cy + r_px), fill=255
        )
    n = disc.histogram()[255] or 1
    hist = ImageChops.multiply(raster, disc).histogram()
    light = sum(hist[V_LIGHT:V_MODERATE])
    moderate = sum(hist[V_MODERATE:V_HEAVY])
    heavy = sum(hist[V_HEAVY:])
    return {
        "any": (light + moderate + heavy) / n,
        "light": light / n,
        "moderate": moderate / n,
        "heavy": heavy / n,
        "max_dbz": max((i for i in range(256) if hist[i]), default=0) - DBZ_OFFSET,
    }


def classify_sample(s: dict[str, float]) -> str:
    """'dry', 'light', 'moderate' or 'heavy' for one disc sample."""
    if s["any"] < RAIN_FRACTION:
        return "dry"
    if s["heavy"] >= 0.02:
        return "heavy"
    if s["moderate"] + s["heavy"] >= 0.04:
        return "moderate"
    return "light"


def estimate_drift(
    latest, older, lag_min: float, pil, grid: int = 80
) -> tuple[float, float] | None:
    """How the echoes moved, in view pixels per minute, from two frames.

    Both rasters are reduced to a coarse rain mask and the older one is slid
    over the newer; the shift with the best Jaccard overlap wins. None when
    either frame has too little rain to say.
    """
    Image, ImageChops, _d, _f = pil
    side = latest.size[0]
    cell = side / grid

    def coarse(r):
        m = _band(r, V_LIGHT, 256).resize((grid, grid), Image.Resampling.BOX)
        return m.point(lambda v: 255 if v >= 64 else 0)

    new, old = coarse(latest), coarse(older)
    n_new, n_old = new.histogram()[255], old.histogram()[255]
    if n_new < 12 or n_old < 12:
        return None
    # A quarter of the grid each way: 0.4 of the range in the lag, which is
    # 40 km/h at 50 km range over a half-hour lag and far more at 200 km.
    reach = grid // 4
    inner = (reach, reach, grid - reach, grid - reach)
    new_c = new.crop(inner)
    n_new_c = new_c.histogram()[255]
    best, best_shift = -1.0, (0, 0)
    for dy in range(-reach, reach + 1):
        for dx in range(-reach, reach + 1):
            old_s = ImageChops.offset(old, dx, dy).crop(inner)
            inter = ImageChops.multiply(old_s, new_c).histogram()[255]
            union = old_s.histogram()[255] + n_new_c - inter
            score = inter / union if union else 0.0
            if score > best + 1e-9 or (
                abs(score - best) < 1e-9
                and dx * dx + dy * dy < best_shift[0] ** 2 + best_shift[1] ** 2
            ):
                best, best_shift = score, (dx, dy)
    if best < 0.15:
        return None
    dx, dy = best_shift
    return dx * cell / lag_min, dy * cell / lag_min


def shift_raster(raster, sx: int, sy: int, pil):
    """Translate without wrap-around; the exposed edge is no echo."""
    Image, ImageChops, _d, _f = pil
    if sx == 0 and sy == 0:
        return raster
    side = raster.size[0]
    out = ImageChops.offset(raster, sx, sy)
    blank = Image.new("L", (side, side), 0)
    if sx > 0:
        out.paste(blank.crop((0, 0, sx, side)), (0, 0))
    elif sx < 0:
        out.paste(blank.crop((0, 0, -sx, side)), (side + sx, 0))
    if sy > 0:
        out.paste(blank.crop((0, 0, side, sy)), (0, 0))
    elif sy < 0:
        out.paste(blank.crop((0, 0, side, -sy)), (0, side + sy))
    return out


COMPASS = [
    "north",
    "north-east",
    "east",
    "south-east",
    "south",
    "south-west",
    "west",
    "north-west",
]


def compass(dx: float, dy: float) -> str:
    """Eight-point name for a vector with x east and y south."""
    ang = (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0
    return COMPASS[int((ang + 22.5) // 45) % 8]


def nearest_echo(
    raster, px_per_km: float, pil, grid: int = 96
) -> dict[str, Any] | None:
    """Distance and bearing from home to the nearest rain on the map."""
    Image = pil[0]
    side = raster.size[0]
    m = _band(raster, V_LIGHT, 256).resize((grid, grid), Image.Resampling.BOX)
    data = m.tobytes()
    cell = side / grid
    best = None
    c = (grid - 1) / 2.0
    for i, v in enumerate(data):
        if v < 48:
            continue
        gx, gy = i % grid, i // grid
        d2 = (gx - c) ** 2 + (gy - c) ** 2
        if best is None or d2 < best[0]:
            best = (d2, gx - c, gy - c)
    if best is None:
        return None
    km = math.sqrt(best[0]) * cell / px_per_km
    return {"km": km, "dir": compass(best[1], best[2])}


def headline(
    samples: list[tuple[int, dict[str, float]]],
    nearest: dict[str, Any] | None,
    drift_kmh: float | None,
    drift_dir: str | None,
    range_km: int,
) -> dict[str, str]:
    """Plain words from the disc samples: (minutes from now, sample), now first."""
    now = samples[0][1]
    future = samples[1:]
    level = classify_sample(now)
    if level != "dry":
        lead = {
            "heavy": "Heavy rain now",
            "moderate": "Raining now",
            "light": "Light rain now",
        }[level]
        first_dry = next((m for m, s in future if classify_sample(s) == "dry"), None)
        if first_dry is None:
            head = lead + ", set in for the hour"
        elif first_dry <= STEP_MIN:
            head = lead + ", easing soon"
        elif first_dry < STEP_MIN * FORECAST_STEPS:
            head = lead + f", easing in about {first_dry} min"
        else:
            head = lead + ", easing within the hour"
        status = "now"
    else:
        hit = next(((m, s) for m, s in future if classify_sample(s) != "dry"), None)
        if hit is not None:
            minutes, s = hit
            word = "Heavy rain" if classify_sample(s) == "heavy" else "Rain"
            head = f"{word} in about {minutes} min"
            status = "soon"
        else:
            head = "Dry for the next hour"
            status = "dry"
    moving = ""
    if drift_kmh is not None and drift_dir:
        moving = (
            f"moving {drift_dir} at {int(round(drift_kmh))} km/h"
            if drift_kmh >= 5
            else "barely moving"
        )
    if level != "dry":
        sub = moving.capitalize() if moving else "Over home now"
    elif nearest is not None:
        km = nearest["km"]
        where = (
            "under 2 km away"
            if km < 2
            else f"{int(round(km))} km to the {nearest['dir']}"
        )
        sub = f"Rain {where}" + (f", {moving}" if moving else "")
    else:
        sub = f"No rain within {int(round(range_km * VIEW_SPAN / 2))} km"
    return {"headline": head, "sub": sub, "status": status, "level": level}


# ---------------------------------------------------------------- time


def _tz_of(options: dict[str, Any]) -> tzinfo:
    from zoneinfo import ZoneInfo

    for value in (options.get("timezone"), options.get("location")):
        if isinstance(value, dict):
            value = value.get("timezone")
        if isinstance(value, str) and value.strip():
            try:
                return ZoneInfo(value.strip())
            except (ValueError, KeyError, OSError):
                pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def _clock(ts: int, tz: tzinfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


# ---------------------------------------------------------------- fetch


def _int_option(
    options: dict[str, Any], name: str, default: int, allowed: tuple[int, ...]
) -> int:
    try:
        v = int(float(options.get(name) or default))
    except (TypeError, ValueError):
        v = default
    return v if v in allowed else default


def _stale(data_dir: Path) -> dict[str, Any] | None:
    folder = data_dir / "render"
    try:
        files = sorted(
            folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError:
        return None
    for p in files:
        try:
            if time.time() - p.stat().st_mtime > STALE_TTL:
                break
        except OSError:
            continue
        data = _read_json(p)
        if isinstance(data, dict) and data.get("map"):
            return data
    return None


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    try:
        lat = float(options.get("latitude"))
        lon = float(options.get("longitude"))
    except (TypeError, ValueError):
        return {"error": "Set a place in this cell's settings to draw its radar."}
    label = str(options.get("label") or "").strip()
    range_km = _int_option(options, "range", 100, (50, 100, 200))
    frames_n = _int_option(options, "frames", 6, (4, 6, 8))
    colour = str(options.get("colour") or "teal")
    colours = COLOURS.get(colour) or COLOURS["teal"]
    layout = str(options.get("layout") or "strip")
    title = options.get("title")
    title = "Rain radar" if title is None else str(title)

    pil = _pil()
    if pil is None:
        return {
            "error": "Pillow is needed to draw the radar map. Install it in Tesserae's Python environment."
        }
    Image = pil[0]

    data_dir = Path(ctx.get("data_dir") or os.path.join(os.getcwd(), "data", WIDGET))
    fresh = bool(ctx.get("fresh"))
    tz = _tz_of(options)

    index = load_index(data_dir, fresh=fresh)
    if index is None:
        stale = _stale(data_dir)
        if stale is not None:
            stale = dict(stale)
            stale["notices"] = [
                f"RainViewer didn't answer; frames from {stale.get('latest_time', '?')}"
            ]
            stale.update(
                {
                    "layout": layout,
                    "title": title,
                    "label": label or stale.get("label", ""),
                }
            )
            return stale
        return {"error": "Couldn't reach RainViewer right now."}

    host = str(index.get("host") or "https://tilecache.rainviewer.com")
    past = [
        f
        for f in (index["radar"].get("past") or [])
        if isinstance(f, dict) and f.get("path")
    ]
    nowcast = [
        f
        for f in (index["radar"].get("nowcast") or [])
        if isinstance(f, dict) and f.get("path")
    ]
    past.sort(key=lambda f: int(f.get("time") or 0))
    nowcast.sort(key=lambda f: int(f.get("time") or 0))
    if not past:
        return {"error": "RainViewer has no radar frames right now."}
    latest = past[-1]
    latest_ts = int(latest.get("time") or 0)

    long_side = (
        max(int(ctx.get("cell_w") or 0), int(ctx.get("cell_h") or 0))
        or max(int(ctx.get("panel_w") or 0), int(ctx.get("panel_h") or 0))
        or 800
    )
    view = plan_view(lat, lon, range_km, long_side)
    side = view["side"]

    key_src = json.dumps(
        [
            round(lat, 4),
            round(lon, 4),
            range_km,
            frames_n,
            colour,
            side,
            view["zoom"],
            latest_ts,
            [int(f.get("time") or 0) for f in nowcast[:3]],
            VERSION,
        ],
    )
    key = hashlib.md5(key_src.encode("utf-8")).hexdigest()[:16]
    render_path = data_dir / "render" / f"{key}.json"
    if not fresh:
        try:
            if (
                render_path.exists()
                and time.time() - render_path.stat().st_mtime < RENDER_TTL
            ):
                cached = _read_json(render_path)
                if (
                    isinstance(cached, dict)
                    and cached.get("map")
                    and not cached.get("partial")
                ):
                    cached.update(
                        {
                            "layout": layout,
                            "title": title,
                            "label": label or cached.get("label", ""),
                        }
                    )
                    return cached
        except OSError:
            pass

    notices: list[str] = []
    nowcast_frames = []
    for f in nowcast[:3]:
        if int(round((int(f.get("time") or 0) - latest_ts) / 60.0)) > 0:
            nowcast_frames.append(f)
    strip_frames = past[-(frames_n + 1) : -1]

    # Everything this render wants, in the order the budget should spend on
    # it, then fetched in parallel.
    store = TileStore(data_dir)
    for f in [latest] + nowcast_frames + list(reversed(strip_frames)):
        for _tx, _ty, path, url in radar_jobs(view, host, str(f["path"]), data_dir):
            store.want(path, url, RADAR_TTL)
    store.prefetch()

    try:
        coast = land_paths(view, lat, lon)
    except (OSError, ValueError):
        coast = {"land": "", "water": ""}
        notices.append("Coastline unavailable")

    latest_raster = radar_raster(view, host, str(latest["path"]), store, pil)
    if latest_raster is None:
        stale = _stale(data_dir)
        if stale is not None:
            stale = dict(stale)
            stale["notices"] = [
                f"Radar tiles didn't load; frames from {stale.get('latest_time', '?')}"
            ]
            stale.update(
                {
                    "layout": layout,
                    "title": title,
                    "label": label or stale.get("label", ""),
                }
            )
            return stale
        return {"error": "Couldn't load the radar tiles right now."}

    # Nowcast frames, when the index has any (the public tier carries none
    # at the time of writing).
    nowcast_rasters: list[tuple[int, Any]] = []
    for f in nowcast_frames:
        ts = int(f.get("time") or 0)
        minutes = int(round((ts - latest_ts) / 60.0))
        r = radar_raster(view, host, str(f["path"]), store, pil)
        if r is not None:
            nowcast_rasters.append((minutes, r))

    # Past frames for the strip and the drift, newest first: a frame past
    # the budget breaks the run so the strip never has a hole in it.
    past_rasters: dict[int, Any] = {}
    for f in reversed(strip_frames):
        r = radar_raster(view, host, str(f["path"]), store, pil)
        if r is None:
            break
        past_rasters[int(f.get("time") or 0)] = r
    partial = len(past_rasters) < len(strip_frames)
    if partial:
        notices.append("More frames on the next refresh")

    # ---- the pictures
    main = paint_rain(side, latest_raster, colours, pil)
    frames_out = []
    for f in strip_frames:
        ts = int(f.get("time") or 0)
        r = past_rasters.get(ts)
        if r is None:
            continue
        thumb = paint_rain(side, r, colours, pil, flat=True).resize(
            (THUMB_PX, THUMB_PX), Image.Resampling.BOX
        )
        sample = disc_sample(
            r, side / 2.0, side / 2.0, HOME_KM * view["px_per_km"], pil
        )
        frames_out.append(
            {
                "time": _clock(ts, tz),
                "ts": ts,
                "thumb": _data_uri(thumb, pil, 32),
                "level": classify_sample(sample),
            }
        )

    # ---- the headline
    px_per_km = view["px_per_km"]
    r_px = HOME_KM * px_per_km
    disc = Image.new("L", (side, side), 0)
    pil[2].Draw(disc).ellipse(
        (side / 2 - r_px, side / 2 - r_px, side / 2 + r_px, side / 2 + r_px), fill=255
    )
    samples: list[tuple[int, dict[str, float]]] = [
        (0, disc_sample(latest_raster, side / 2, side / 2, r_px, pil, disc))
    ]

    drift = None
    for lag in (3, 2, 1):
        if len(strip_frames) >= lag:
            ts = int(strip_frames[-lag].get("time") or 0)
            older = past_rasters.get(ts)
            lag_min = (latest_ts - ts) / 60.0
            if older is not None and lag_min > 0:
                drift = estimate_drift(latest_raster, older, lag_min, pil)
                if drift is not None:
                    break
    drift_kmh = drift_dir = None
    if drift is not None:
        vx, vy = drift
        speed_px_min = math.hypot(vx, vy)
        drift_kmh = speed_px_min / px_per_km * 60.0
        drift_dir = compass(vx, vy) if speed_px_min > 0 else None

    source = "none"
    if nowcast_rasters:
        source = "nowcast"
        for minutes, r in nowcast_rasters:
            samples.append(
                (minutes, disc_sample(r, side / 2, side / 2, r_px, pil, disc))
            )
        covered = max(m for m, _r in nowcast_rasters)
    else:
        covered = 0
    if drift is not None or not nowcast_rasters:
        if drift is not None:
            source = "drift" if source == "none" else source
        else:
            source = "persistence" if source == "none" else source
        vx, vy = drift or (0.0, 0.0)
        for k in range(1, FORECAST_STEPS + 1):
            minutes = k * STEP_MIN
            if minutes <= covered:
                continue
            shifted = shift_raster(
                latest_raster, int(round(vx * minutes)), int(round(vy * minutes)), pil
            )
            samples.append(
                (minutes, disc_sample(shifted, side / 2, side / 2, r_px, pil, disc))
            )
    samples.sort(key=lambda s: s[0])

    nearest = nearest_echo(latest_raster, px_per_km, pil)
    words = headline(samples, nearest, drift_kmh, drift_dir, range_km)

    out: dict[str, Any] = {
        "label": label,
        "title": title,
        "layout": layout,
        "colour": colour,
        "colours": colours,
        "range_km": range_km,
        "ring_km": RANGE_RING_KM.get(range_km, 25),
        "ring_px": round(RANGE_RING_KM.get(range_km, 25) * px_per_km, 1),
        "home_px": round(HOME_KM * px_per_km, 1),
        "map": _data_uri(main, pil, 16),
        "land": coast["land"],
        "lakes": coast["water"],
        "map_px": side,
        "px_per_km": round(px_per_km, 3),
        "zoom": view["zoom"],
        "centre_tile": list(view["centre_tile"]),
        "frames": frames_out,
        "latest_time": _clock(latest_ts, tz),
        "latest_ts": latest_ts,
        "headline": words["headline"],
        "sub": words["sub"],
        "status": words["status"],
        "level": words["level"],
        "forecast_source": source,
        "drift_kmh": None if drift_kmh is None else round(drift_kmh),
        "drift_dir": drift_dir,
        "nearest_km": None if nearest is None else round(nearest["km"], 1),
        "samples": [[m, round(s["any"], 3), classify_sample(s)] for m, s in samples],
        "credits": "Radar: RainViewer \u00b7 Coast: Natural Earth",
        "notices": notices,
        "partial": partial,
        "requests": store.used,
    }
    _write_json(render_path, out)
    _prune(data_dir / "render", STALE_TTL)
    _prune(data_dir / "tiles" / "radar", RADAR_TTL + 3600)
    return out
