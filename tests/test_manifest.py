"""The options panel stays short: place, range and layout up front, the rest
under More options."""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST = json.loads(
    (Path(__file__).resolve().parent.parent / "plugin.json").read_text(encoding="utf-8")
)
OPTIONS = MANIFEST["cell_options"]

ORDER = ["location", "range", "layout", "frames", "colour", "title"]
UP_FRONT = ["location", "range", "layout"]


def _up_front(o: dict) -> bool:
    return not o.get("show_if") and not o.get("advanced")


def test_option_names_and_order() -> None:
    assert [o["name"] for o in OPTIONS] == ORDER


def test_few_options_up_front() -> None:
    assert [o["name"] for o in OPTIONS if _up_front(o)] == UP_FRONT


def test_advanced_options_come_last() -> None:
    seen = False
    for o in OPTIONS:
        if o.get("advanced"):
            seen = True
        else:
            assert not seen, f"{o['name']} follows an advanced option"


def test_manifest_essentials() -> None:
    assert MANIFEST["name"] == "Rain Radar"
    assert MANIFEST["icon"] == "ph-cloud-rain"
    assert MANIFEST["category"] == "weather"
    assert MANIFEST["design"] == {"palette": "extended"}
    assert "network:api.rainviewer.com" in MANIFEST["requires"]
    assert "network:tilecache.rainviewer.com" in MANIFEST["requires"]
    assert MANIFEST["requires"] == [
        "network:api.rainviewer.com",
        "network:tilecache.rainviewer.com",
    ]
    assert "network:*" not in MANIFEST["requires"]


def test_no_option_named_label() -> None:
    assert all(o["name"] != "label" for o in OPTIONS)


def test_no_em_dashes_anywhere_in_the_manifest() -> None:
    assert chr(0x2014) not in json.dumps(MANIFEST, ensure_ascii=False)
