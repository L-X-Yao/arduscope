"""An HTML report opens on a laptop in the field with no network."""

from __future__ import annotations

from tests._repo import REPO_ROOT

import base64
import hashlib
import re

import plotly.offline
import pytest

from lib.html_report import HtmlReportGenerator
from lib.i18n import t
from lib.results import AnalysisResult
from lib.vehicle_detect import VehicleInfo

_VEHICLE = VehicleInfo("ArduCopter", "CubeOrange", "V4.6.3", 1, 4, 2, False)


def _report(gps_track=None) -> str:
    result = AnalysisResult(title="Summary", status="good", summary="ok")
    return HtmlReportGenerator(_VEHICLE, [result],
                               gps_track=gps_track).generate()


@pytest.mark.fast
def test_plotly_is_inline_and_loads_from_nowhere():
    """The CDN tag asked for plotly-<python package version>.min.js, a file
    that does not exist, so every report stalled on a 403 before falling
    back to the copy it embedded anyway."""
    html = _report()

    embedded = plotly.offline.get_plotlyjs() in html
    assert embedded
    assert not re.search(r'<script[^>]*\bsrc=', html)
    no_fallback_tag = 'id="plotly-fb"' not in html
    assert no_fallback_tag


_TRACK = {"lat": [34.10, 34.11, 34.12], "lng": [108.90, 108.91, 108.92],
          "alt": [400.0, 410.0, 420.0], "time_s": [0.0, 1.0, 2.0]}
_VENDOR = REPO_ROOT / "lib" / "vendor" / "leaflet"

# Published with Leaflet 1.9.4: integrity_hash_uglified and integrity_hash_css
# in docs/_config.yml on the v1 branch of github.com/Leaflet/Leaflet.
_PUBLISHED_SRI = {
    "leaflet.js": "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=",
    "leaflet.css": "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=",
}


class TestLeaflet:
    """The map loaded Leaflet from unpkg, so it had no code without a
    network and said so only after the panel was opened."""

    @pytest.mark.fast
    @pytest.mark.parametrize("name", sorted(_PUBLISHED_SRI))
    def test_vendored_file_matches_published_hash(self, name):
        digest = hashlib.sha256((_VENDOR / name).read_bytes()).digest()
        sri = "sha256-" + base64.b64encode(digest).decode()
        assert sri == _PUBLISHED_SRI[name]

    @pytest.mark.fast
    def test_license_is_kept(self):
        text = (_VENDOR / "LICENSE").read_text(encoding="utf-8")
        assert "BSD 2-Clause" in text and "Agafonkin" in text

    @pytest.mark.fast
    def test_vendored_files_are_package_data(self):
        tomllib = pytest.importorskip("tomllib")
        root = _VENDOR.parent.parent.parent
        with open(root / "pyproject.toml", "rb") as f:
            package_data = tomllib.load(f)["tool"]["setuptools"]["package-data"]
        shipped = {p.name for pattern in package_data["lib"]
                   for p in (root / "lib").glob(pattern)}
        assert {"leaflet.js", "leaflet.css", "LICENSE"} <= shipped

    @pytest.mark.fast
    def test_report_embeds_leaflet(self):
        html = _report(_TRACK)

        embedded = [(_VENDOR / name).read_text(encoding="utf-8") in html
                    for name in ("leaflet.js", "leaflet.css")]
        assert embedded == [True, True]
        assert not re.search(r'<script[^>]*\bsrc=', html)
        assert not re.search(r'<link[^>]*\bhref=', html)

    @pytest.mark.fast
    def test_no_track_no_leaflet(self):
        leaflet = (_VENDOR / "leaflet.js").read_text(encoding="utf-8")
        embedded = leaflet in _report()
        assert not embedded

    @pytest.mark.fast
    def test_missing_tiles_are_announced(self):
        """Tiles still need a network. Without one the track is drawn on a
        blank panel, which must not read as a broken map."""
        html = _report(_TRACK)

        notice = re.search(
            r'<div id="map-tile-notice"[^>]*>(.*?)</div>', html, re.S)
        assert notice is not None
        assert notice.group(1).strip() == t("report.map_tiles_offline")
        handled = "'tileerror'" in html
        assert handled
