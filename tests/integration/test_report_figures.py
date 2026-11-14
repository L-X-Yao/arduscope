"""Charts and sections keep what the analyzers set."""

from __future__ import annotations

import re

import plotly.graph_objects as go
import pytest

from lib.html_report import HtmlReportGenerator, downsample_figure
from lib.results import AnalysisResult
from lib.vehicle_detect import VehicleInfo

_VEHICLE = VehicleInfo("ArduCopter", "CubeOrange", "V4.6.3", 1, 4, 2, False)


class TestAxisRanges:
    """Forcing autorange on every axis threw away ranges analyzers set on
    purpose: the 0-500 Hz FFT window, the 0-360 wind direction axis."""

    @pytest.mark.fast
    def test_fixed_ranges_survive(self):
        fig = go.Figure(go.Scatter(x=list(range(100)), y=list(range(100))))
        fig.update_xaxes(range=[0, 500])
        fig.update_layout(yaxis=dict(range=[0, 360]),
                          yaxis2=dict(range=[-1, 1], overlaying="y"))

        downsample_figure(fig, max_points=10)

        assert len(fig.data[0].y) == 10
        assert tuple(fig.layout.xaxis.range) == (0, 500)
        assert tuple(fig.layout.yaxis.range) == (0, 360)
        assert tuple(fig.layout.yaxis2.range) == (-1, 1)
        assert fig.layout.xaxis.autorange is None

    @pytest.mark.fast
    def test_webgl_trace_keeps_range_when_converted(self):
        fig = go.Figure(go.Scattergl(x=[0, 1, 2], y=[0, 1, 2]))
        fig.update_layout(yaxis=dict(range=[0, 360]))

        downsample_figure(fig)

        assert fig.data[0].type == "scatter"
        assert tuple(fig.layout.yaxis.range) == (0, 360)


class TestUnequalArrays:
    """A trace with a time axis longer than its values: thinning it indexed
    past the end of the shorter array, and the report was not written."""

    @pytest.mark.fast
    @pytest.mark.parametrize("x_len, y_len", [(103, 100), (100, 103)])
    def test_trace_is_cut_to_its_shorter_array(self, x_len, y_len):
        fig = go.Figure(go.Scatter(x=list(range(x_len)),
                                   y=list(range(y_len))))

        downsample_figure(fig, max_points=10)

        assert len(fig.data[0].x) == len(fig.data[0].y) == 10
        assert max(fig.data[0].x) == 99

    @pytest.mark.fast
    def test_short_trace_is_left_as_it_is(self):
        fig = go.Figure(go.Scatter(x=list(range(8)), y=list(range(5))))

        downsample_figure(fig, max_points=10)

        assert (len(fig.data[0].x), len(fig.data[0].y)) == (8, 5)


class TestSectionAnchors:
    """With sub-results on the first card, index * 100 + j made sub-result
    ids equal the ids of the following cards, so the table of contents
    jumped to the wrong section."""

    @pytest.mark.fast
    def test_every_section_id_is_unique(self):
        subs = [AnalysisResult(title=f"Axis {i}", status="good")
                for i in range(3)]
        results = [AnalysisResult(title="PID", status="good", sub_results=subs)]
        results += [AnalysisResult(title=f"Card {i}", status="good")
                    for i in range(3)]

        html = HtmlReportGenerator(_VEHICLE, results).generate()

        ids = re.findall(r'<section id="(section-[^"]+)"', html)
        assert len(ids) == 7
        assert len(set(ids)) == 7

    @pytest.mark.fast
    def test_toc_links_reach_top_level_cards(self):
        subs = [AnalysisResult(title="Axis", status="good")]
        results = [AnalysisResult(title="PID", status="good", sub_results=subs),
                   AnalysisResult(title="Motor", status="good")]

        html = HtmlReportGenerator(_VEHICLE, results).generate()

        target = re.search(r'<a href="#(section-\d+)">[^<]*<span[^>]*></span>'
                           r'Motor</a>', html).group(1)
        card = re.search(rf'<section id="{target}"[^>]*>.*?<h3>([^<]*)', html,
                         re.S)
        assert card.group(1).startswith("Motor")
