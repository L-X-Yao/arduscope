"""Template thresholds are labelled, and a templated report says so."""

from __future__ import annotations

import os
import re

import pytest

from lib.html_report import HtmlReportGenerator
from lib.i18n import set_locale, t
from lib.pipeline import compute_health_score, run_analysis
from lib.templates import _TEMPLATE_DEFS, get_template
from lib.thresholds import Thresholds
from tests.binlog import flight_log

_TEMPLATES_PY = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "lib", "templates.py")
_OVERRIDES = [(d["name"], k, v) for d in _TEMPLATE_DEFS
              for k, v in d["thresholds"].items()]


class TestThresholdOverrides:
    @pytest.mark.fast
    def test_all_sixteen_are_still_there(self):
        assert len(_OVERRIDES) == 16

    @pytest.mark.fast
    @pytest.mark.parametrize("template, key, value", _OVERRIDES)
    def test_override_is_a_real_field_and_only_tightens(self, template, key,
                                                        value):
        """A template may be stricter than the cited default, never
        looser. Every field used here warns above its limit."""
        assert hasattr(Thresholds(), key)
        assert value < getattr(Thresholds(), key)

    @pytest.mark.fast
    def test_every_override_is_labelled_class_e_with_a_reason(self):
        """CLAUDE.md invariant 4: no threshold without a source label.
        These have no published source, which is what class E declares."""
        with open(_TEMPLATES_PY, encoding="utf-8") as f:
            lines = f.read().splitlines()
        labelled = [re.match(r'\s+"(\w+)": [\d.]+,\s+# E: (\S.{10,})$', line)
                    for line in lines]
        numeric = [line for line in lines
                   if re.match(r'\s+"\w+": [\d.]+,', line)]

        assert len(numeric) == len(_OVERRIDES)
        assert sum(1 for m in labelled if m) == len(_OVERRIDES)


class TestReportedTemplate:
    """A finding quotes the default limit in its citation, so a report that
    hides the template contradicts its own evidence."""

    @pytest.fixture(scope="class")
    @classmethod
    def log_path(cls, tmp_path_factory):
        path = tmp_path_factory.mktemp("tpl") / "flight.BIN"
        return flight_log(duration_s=12.0).save(path)

    @pytest.fixture(autouse=True)
    def _english(self):
        set_locale("en")
        yield
        set_locale("zh")

    def test_report_names_template_and_changes(self, log_path):
        template = get_template("copter-factory")

        pipeline = run_analysis(log_path, template=template, include_gps=False)

        notice = pipeline.results[0]
        assert notice.title == t("template.notice_title",
                                 name=template.display_name)
        assert notice.status == "info"
        assert notice.action_level == "ok"
        assert t("template.notice_change", name="vibe_warn", default=15.0,
                 value=10.0) in notice.details

        html = HtmlReportGenerator(pipeline.vehicle_info,
                                   pipeline.results).generate()
        named = template.display_name in html
        assert named

    def test_template_without_thresholds_is_still_named(self, log_path):
        template = get_template("battery-aging")
        assert not template.thresholds

        pipeline = run_analysis(log_path, template=template, include_gps=False)

        assert template.display_name in pipeline.results[0].title

    def test_notice_does_not_move_the_score(self, log_path):
        pipeline = run_analysis(log_path, template=get_template("tuning"),
                                include_gps=False)

        assert pipeline.health_score == compute_health_score(
            list(pipeline.results_by_key.values()))
        assert len(pipeline.results) == len(pipeline.results_by_key) + 1

    def test_no_template_no_notice(self, log_path):
        pipeline = run_analysis(log_path, include_gps=False)

        assert len(pipeline.results) == len(pipeline.results_by_key)
