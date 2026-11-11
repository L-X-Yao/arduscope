"""An LLM that cannot be used must be visible, and cannot soften a verdict."""

from __future__ import annotations

import sys

import pytest

from lib import ai_diagnosis
from lib.ai_diagnosis import generate_diagnosis
from lib.i18n import t
from lib.results import AnalysisResult
from lib.vehicle_detect import VehicleInfo

VEHICLE = VehicleInfo(vehicle_type="ArduCopter", board_name="b",
                      firmware_version="V4.6.3", frame_class=1, motor_count=4,
                      imu_count=2, is_quadplane=False)


def _results(*statuses: str) -> dict[str, AnalysisResult]:
    return {f"k{i}": AnalysisResult(title=f"k{i}", status=s, summary="s",
                                    evidence_source="src")
            for i, s in enumerate(statuses)}


def _diagnose(results, provider="anthropic", api_key="key", score=82):
    return generate_diagnosis(results, VEHICLE, score, 60.0,
                              provider=provider, api_key=api_key)


class TestFallback:
    @pytest.fixture(autouse=True)
    def no_env_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def test_rule_engine_by_default_has_no_note(self, capsys):
        card = _diagnose(_results("good"), provider="rule", api_key="")
        assert card.provider == "rule"
        assert not capsys.readouterr().err

    def test_missing_key_is_reported(self, capsys):
        card = _diagnose(_results("good"), api_key="")
        assert card.provider == "rule"
        reason = t("ai.llm.no_key", env="ANTHROPIC_API_KEY")
        note = t("ai.llm.fallback", provider="anthropic", reason=reason)
        assert card.details[0] == note
        assert note in capsys.readouterr().err

    def test_missing_package_is_reported(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "anthropic", None)
        card = _diagnose(_results("good"))
        assert card.provider == "rule"
        assert t("ai.llm.no_package", package="anthropic") in card.details[0]

    def test_api_error_is_reported(self, monkeypatch):
        def boom(*args, **kwargs):
            raise ConnectionError("refused")

        monkeypatch.setattr(ai_diagnosis, "_call_openai", boom)
        card = _diagnose(_results("good"), provider="openai")
        assert card.provider == "rule"
        assert "ConnectionError: refused" in card.details[0]


class TestLlmCardStatus:
    @pytest.fixture(autouse=True)
    def reply(self, monkeypatch):
        monkeypatch.setattr(ai_diagnosis, "_call_anthropic",
                            lambda *args, **kwargs: "All looks fine\n- ok")

    def test_critical_stays_critical_whatever_the_score(self):
        card = _diagnose(_results("good", "good", "critical"), score=82)
        assert card.provider == "anthropic"
        assert card.status == "critical"
        assert card.evidence_source

    def test_warning_stays_warning(self):
        assert _diagnose(_results("good", "warning")).status == "warning"

    def test_healthy_log_is_good_at_any_score(self):
        assert _diagnose(_results("good"), score=40).status == "good"
