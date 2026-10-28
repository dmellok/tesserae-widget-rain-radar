"""Unit tests for rain_radar: offline and deterministic.

Radar tiles are made up here from the scheme's own colours, so the
classification, the drift and the headline are tested against pictures
whose answer is known. Nothing touches the network: urlopen is replaced.
"""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "rain_radar_server_under_test", HERE.parent / "server.py"
)
server = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(server)

PIL = server._pil()
MELBOURNE = {"latitude": -37.81, "longitude": 144.96, "label": "Melbourne"}


def _hex_rgba(dbz: int) -> tuple[int, int, int, int]:
    hx = server.UNIVERSAL_BLUE[dbz + server.DBZ_OFFSET]
    return tuple(int(hx[k : k + 2], 16) for k in (0, 2, 4, 6))


def _tile(blobs: list[tuple[tuple[int, int, int, int], int]]) -> bytes:
    """A 512 px radar tile with ellipses of a given dBZ, as PNG bytes."""
    im = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for box, dbz in blobs:
        d.ellipse(box, fill=_hex_rgba(dbz))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# ----- tile maths -------------------------------------------------------


def test_melbourne_tile_index_at_zoom_8():
    """The standard slippy-map formula: (144.96 + 180) / 360 * 256 = 231.08."""
    assert server.tile_index(-37.81, 144.96, 8) == (231, 157)
    assert server.tile_index(-37.81, 144.96, 7) == (115, 78)
    assert server.tile_index(51.5, -0.12, 7) == (63, 42)


def test_metres_per_pixel_halves_per_zoom():
    a = server.metres_per_pixel(-37.81, 8)
    b = server.metres_per_pixel(-37.81, 9)
    assert abs(a / b - 2.0) < 1e-9
    assert 470 < a < 500  # about 483 m at Melbourne's latitude


def test_plan_view_keeps_the_block_small_and_home_central():
    for long_side in (240, 640, 1200, 1600):
        for rng in (50, 100, 200):
            v = server.plan_view(-37.81, 144.96, rng, long_side)
            x0, y0, x1, y1 = v["base"]
            assert (x1 - x0 + 1) <= 4 and (y1 - y0 + 1) <= 4
            rx0, ry0, rx1, ry1 = v["radar"]
            assert (rx1 - rx0 + 1) * (ry1 - ry0 + 1) <= 9
            assert v["side"] <= server.MAX_SIDE
            # the square covers 1.6 times the range
            assert abs(v["side"] * v["mpp"] / 1000.0 - 1.6 * rng) < 1.0


# ----- classification ---------------------------------------------------


def test_synthetic_tile_classifies_into_three_bands():
    png = _tile(
        [((0, 0, 100, 100), 15), ((200, 200, 300, 300), 32), ((400, 400, 500, 500), 48)]
    )
    raster = server.dbz_raster(png, PIL)
    assert raster is not None and raster.mode == "L"
    assert raster.getpixel((50, 50)) == 15 + server.DBZ_OFFSET
    assert raster.getpixel((250, 250)) == 32 + server.DBZ_OFFSET
    assert raster.getpixel((450, 450)) == 48 + server.DBZ_OFFSET
    assert raster.getpixel((150, 150)) == 0
    light = server._band(raster, server.V_LIGHT, server.V_MODERATE)
    moderate = server._band(raster, server.V_MODERATE, server.V_HEAVY)
    heavy = server._band(raster, server.V_HEAVY, 256)
    assert light.getpixel((50, 50)) == 255 and moderate.getpixel((50, 50)) == 0
    assert moderate.getpixel((250, 250)) == 255 and heavy.getpixel((250, 250)) == 0
    assert heavy.getpixel((450, 450)) == 255


def test_drizzle_below_ten_dbz_is_not_rain():
    png = _tile([((0, 0, 200, 200), 4)])
    raster = server.dbz_raster(png, PIL)
    assert raster.getpixel((100, 100)) == 0


def test_a_placeholder_card_is_not_a_radar_tile():
    im = Image.new("RGBA", (256, 256), (255, 255, 255, 200))
    ImageDraw.Draw(im).rectangle((40, 100, 216, 156), fill=(0, 0, 0, 140))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    assert server.dbz_raster(buf.getvalue(), PIL) is None


def test_disc_sample_reads_the_home_disc_only():
    raster = Image.new("L", (400, 400), 0)
    ImageDraw.Draw(raster).rectangle((0, 0, 400, 120), fill=server.V_HEAVY + 5)
    far = server.disc_sample(raster, 200, 200, 40, PIL)
    assert far["any"] == 0 and server.classify_sample(far) == "dry"
    ImageDraw.Draw(raster).ellipse((180, 180, 220, 220), fill=server.V_MODERATE + 1)
    near = server.disc_sample(raster, 200, 200, 40, PIL)
    assert 0.2 < near["any"] < 0.35 and server.classify_sample(near) == "moderate"


# ----- headline ---------------------------------------------------------


def _s(any_=0.0, moderate=0.0, heavy=0.0):
    return {
        "any": any_,
        "light": any_ - moderate - heavy,
        "moderate": moderate,
        "heavy": heavy,
        "max_dbz": 0,
    }


def test_headline_rain_on_the_way():
    samples = [
        (0, _s()),
        (10, _s()),
        (20, _s(0.2)),
        (30, _s(0.4)),
        (40, _s()),
        (50, _s()),
        (60, _s()),
    ]
    out = server.headline(samples, {"km": 18.0, "dir": "west"}, 36.0, "east", 100)
    assert out["headline"] == "Rain in about 20 min"
    assert out["sub"] == "Rain 18 km to the west, moving east at 36 km/h"
    assert out["status"] == "soon"


def test_headline_heavy_rain_on_the_way():
    samples = [(0, _s()), (10, _s(0.3, 0.1, 0.1))]
    out = server.headline(samples, {"km": 5.0, "dir": "north"}, None, None, 50)
    assert out["headline"] == "Heavy rain in about 10 min"
    assert out["sub"] == "Rain 5 km to the north"


def test_headline_raining_now_and_easing():
    samples = [
        (0, _s(0.5, 0.2)),
        (10, _s(0.3, 0.1)),
        (20, _s(0.1)),
        (30, _s(0.02)),
        (40, _s()),
        (50, _s()),
        (60, _s()),
    ]
    out = server.headline(samples, {"km": 0.0, "dir": "north"}, 30.0, "south-east", 100)
    assert out["headline"] == "Raining now, easing in about 30 min"
    assert out["sub"] == "Moving south-east at 30 km/h"
    assert out["status"] == "now"
    solid = [(m, _s(0.5)) for m in range(0, 70, 10)]
    assert (
        server.headline(solid, None, 2.0, "east", 100)["headline"]
        == "Light rain now, set in for the hour"
    )
    assert server.headline(solid, None, 2.0, "east", 100)["sub"] == "Barely moving"


def test_headline_dry_hour():
    samples = [(m, _s()) for m in range(0, 70, 10)]
    out = server.headline(samples, None, None, None, 100)
    assert out["headline"] == "Dry for the next hour"
    assert out["sub"] == "No rain within 80 km"
    assert out["status"] == "dry"
    near = server.headline(samples, {"km": 42.4, "dir": "south-west"}, None, None, 50)
    assert near["sub"] == "Rain 42 km to the south-west"


def test_compass_points():
    assert server.compass(0, -1) == "north"
    assert server.compass(1, 0) == "east"
    assert server.compass(1, 1) == "south-east"
    assert server.compass(-1, 0.1) == "west"


# ----- drift ------------------------------------------------------------


def test_drift_is_recovered_from_two_shifted_frames():
    side = 400
    older = Image.new("L", (side, side), 0)
    ImageDraw.Draw(older).ellipse((60, 160, 160, 260), fill=server.V_MODERATE)
    latest = Image.new("L", (side, side), 0)
    ImageDraw.Draw(latest).ellipse((120, 160, 220, 260), fill=server.V_MODERATE)
    drift = server.estimate_drift(latest, older, 30.0, PIL)
    assert drift is not None
    vx, vy = drift
    assert abs(vx * 30.0 - 60.0) <= 6.0  # moved 60 px east in 30 min
    assert abs(vy * 30.0) <= 6.0
    assert server.compass(vx, vy) == "east"
    # the same blob projected 30 minutes on lands 60 px further east
    shifted = server.shift_raster(latest, int(round(vx * 30)), int(round(vy * 30)), PIL)
    assert shifted.getpixel((230, 210)) == server.V_MODERATE
    assert shifted.getpixel((100, 210)) == 0


def test_drift_needs_rain_in_both_frames():
    blank = Image.new("L", (400, 400), 0)
    rainy = Image.new("L", (400, 400), 0)
    ImageDraw.Draw(rainy).ellipse((100, 100, 200, 200), fill=server.V_LIGHT)
    assert server.estimate_drift(rainy, blank, 30.0, PIL) is None


def test_nearest_echo_distance_and_bearing():
    side, px_per_km = 400, 4.0
    raster = Image.new("L", (side, side), 0)
    ImageDraw.Draw(raster).ellipse(
        (40, 180, 80, 220), fill=server.V_LIGHT
    )  # 140 px west of centre
    near = server.nearest_echo(raster, px_per_km, PIL)
    assert near is not None and near["dir"] == "west"
    assert 28 <= near["km"] <= 38


# ----- fetch, offline ---------------------------------------------------


def test_fetch_without_a_place_asks_for_one(tmp_path):
    out = server.fetch({}, {}, ctx={"data_dir": str(tmp_path)})
    assert "error" in out and "place" in out["error"].lower()


def test_fetch_with_no_network_returns_an_error_dict(tmp_path, monkeypatch):
    def down(*_a, **_k):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", down)
    out = server.fetch(MELBOURNE, {}, ctx={"data_dir": str(tmp_path), "fresh": True})
    assert out == {"error": "Couldn't reach RainViewer right now."}


def test_fetch_draws_a_map_from_fake_tiles(tmp_path, monkeypatch):
    """Everything the widget asks for, answered from memory: the index, one
    Esri tile with a lake and a road, and radar tiles with a blob that drifts
    east towards home between frames."""
    now = 1_793_000_000
    past = [{"time": now - 600 * (6 - i), "path": f"/v2/radar/f{i}"} for i in range(7)]
    index = {
        "host": "https://tilecache.rainviewer.com",
        "radar": {"past": past, "nowcast": []},
    }

    base = Image.new("L", (256, 256), 232)
    d = ImageDraw.Draw(base)
    d.ellipse((20, 20, 120, 120), fill=208)
    d.line((0, 200, 256, 200), fill=255, width=2)
    base_buf = io.BytesIO()
    base.save(base_buf, "JPEG", quality=95)

    def radar_png(step: int) -> bytes:
        x = 100 + step * 40
        return _tile([((x, 200, x + 90, 290), 34)])

    class Resp:
        def __init__(self, raw):
            self.raw = raw

        def read(self):
            return self.raw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    calls = []

    def fake_open(req, timeout=0):
        url = req.full_url
        calls.append(url)
        if url == server.INDEX_URL:
            return Resp(json.dumps(index).encode("utf-8"))
        if "arcgisonline" in url:
            return Resp(base_buf.getvalue())
        if "tilecache.rainviewer.com" in url:
            step = int(url.split("/v2/radar/f")[1].split("/")[0])
            return Resp(radar_png(step))
        raise urllib.error.URLError("unexpected " + url)

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    ctx = {"data_dir": str(tmp_path), "cell_w": 640, "cell_h": 400, "fresh": True}
    out = server.fetch({**MELBOURNE, "range": "100", "frames": "6"}, {}, ctx=ctx)
    assert "error" not in out, out.get("error")
    assert out["map"].startswith("data:image/png;base64,")
    assert out["map_px"] > 0 and out["ring_km"] == 25
    assert len(out["frames"]) == 6
    stamps = [f["ts"] for f in out["frames"]]
    assert stamps == sorted(stamps) and stamps[-1] < out["latest_ts"]
    assert out["requests"] <= server.REQUEST_BUDGET
    assert out["forecast_source"] in ("drift", "persistence")
    assert out["headline"]
    # the second render is served from the render cache without a request
    before = len(calls)
    again = server.fetch(
        {**MELBOURNE, "range": "100", "frames": "6"}, {}, ctx={**ctx, "fresh": False}
    )
    assert again["map"] == out["map"]
    assert len(calls) == before


# ----- the offline coastline --------------------------------------------


def test_natural_earth_fallback_draws_port_phillip_bay():
    """Melbourne's CBD is land and the bay 20 km south of it is water."""
    view = server.plan_view(-37.81, 144.96, 100, 640)
    grey = server.natural_earth_grey(view, -37.81, 144.96, PIL)
    side = view["side"]
    ppk = view["px_per_km"]
    assert grey.size == (side, side)
    assert grey.getpixel((side // 2, side // 2 - int(3 * ppk))) == server.LAND_GREY
    assert grey.getpixel((side // 2, side // 2 + int(20 * ppk))) == server.WATER_GREY
    canvas = server.two_tone(grey, PIL, roads=False, zoom=99)
    assert canvas.getpixel((side // 2, side // 2 + int(20 * ppk))) == server.WATER


def test_a_placeholder_base_tile_is_not_a_canvas():
    card = Image.new("L", (256, 256), 255)
    ImageDraw.Draw(card).text((60, 120), "API KEY REQUIRED", fill=90)
    assert not server._looks_like_canvas(card)
    sea = Image.new("L", (256, 256), server.WATER_GREY)
    assert server._looks_like_canvas(sea)
    land = Image.new("L", (256, 256), server.LAND_GREY)
    ImageDraw.Draw(land).line((0, 100, 256, 100), fill=255, width=3)
    assert server._looks_like_canvas(land)


def test_fetch_falls_back_to_the_offline_coast_when_tiles_are_placeholders(
    tmp_path, monkeypatch
):
    now = 1_793_000_000
    past = [{"time": now - 600 * (3 - i), "path": f"/v2/radar/g{i}"} for i in range(4)]
    index = {
        "host": "https://tilecache.rainviewer.com",
        "radar": {"past": past, "nowcast": []},
    }
    card = Image.new("L", (256, 256), 255)
    ImageDraw.Draw(card).text((60, 120), "API KEY REQUIRED", fill=90)
    card_buf = io.BytesIO()
    card.save(card_buf, "PNG")

    class Resp:
        def __init__(self, raw):
            self.raw = raw

        def read(self):
            return self.raw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout=0):
        url = req.full_url
        if url == server.INDEX_URL:
            return Resp(json.dumps(index).encode("utf-8"))
        if "arcgisonline" in url:
            return Resp(card_buf.getvalue())
        return Resp(_tile([((200, 200, 300, 300), 30)]))

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    ctx = {"data_dir": str(tmp_path), "cell_w": 640, "cell_h": 400, "fresh": True}
    out = server.fetch({**MELBOURNE, "frames": "4"}, {}, ctx=ctx)
    assert "error" not in out
    assert out["base_source"] == "natural_earth"
    assert out["credits"].endswith("Natural Earth")
