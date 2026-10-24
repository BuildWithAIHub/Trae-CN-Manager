"""Distribution icon and high-DPI layout regressions; synthetic UI only."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import struct
import xml.etree.ElementTree as ET

from scripts.build_installer import MANIFEST


def test_generated_icon_contains_small_and_large_png_resolutions():
    data = (Path(__file__).resolve().parents[1] / "assets" / "app.ico").read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data)
    assert (reserved, kind, count) == (0, 1, 7)
    sizes = []
    for index in range(count):
        width, height, _, _, planes, bits, length, offset = struct.unpack_from("<BBBBHHII", data, 6 + index * 16)
        sizes.append(width or 256)
        assert width == height and (planes, bits) == (1, 32)
        assert data[offset:offset + 8] == b"\x89PNG\r\n\x1a\n"
        assert offset + length <= len(data)
    assert sizes == [16, 24, 32, 48, 64, 128, 256]


def test_setup_manifest_declares_per_monitor_awareness_without_elevation():
    tree = ET.fromstring(MANIFEST)
    values = {node.tag.rsplit("}", 1)[-1]: node for node in tree.iter()}
    assert values["dpiAwareness"].text == "PerMonitorV2"
    assert values["dpiAware"].text == "true/pm"
    assert values["requestedExecutionLevel"].attrib["level"] == "asInvoker"


def test_brand_is_text_free_and_matches_editable_svg():
    from trae_manager.brand import SVG
    assert "<text" not in SVG
    assert "#b0f1ca" in SVG
    assert (Path(__file__).resolve().parents[1] / "assets" / "app.svg").read_text(encoding="utf-8") == SVG
