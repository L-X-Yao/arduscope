"""An LLM that cannot be used must be visible, and cannot soften a verdict."""

from __future__ import annotations

import sys
from types import SimpleNamespace

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


class _FakeAnthropic:
    """Stands in for the anthropic module: one reply, and the request kept."""

    def __init__(self, reply):
        self.reply = reply
        self.request: dict = {}
        self.messages = self            # client.messages.create(...)

    def Anthropic(self, api_key=None):  # noqa: N802 - the SDK's name
        return self

    def create(self, **request):
        self.request = request
        return self.reply


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _thinking() -> SimpleNamespace:
    """A thinking block as the API returns it: no text attribute."""
    return SimpleNamespace(type="thinking", thinking="", signature="sig")


def _reply(*blocks, stop: str = "end_turn", **extra) -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), stop_reason=stop, **extra)


class TestAnthropicReply:
    """A reply is a list of blocks, and the first one is not always text."""

    @pytest.fixture(autouse=True)
    def no_env_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    @staticmethod
    def _install(monkeypatch, reply) -> _FakeAnthropic:
        fake = _FakeAnthropic(reply)
        monkeypatch.setitem(sys.modules, "anthropic", fake)
        return fake

    def test_text_after_a_thinking_block_is_returned(self, monkeypatch):
        assert not hasattr(_thinking(), "text")
        self._install(monkeypatch, _reply(_thinking(), _text("Looks fine")))
        assert ai_diagnosis._call_anthropic("sys", "user", "key") == \
            "Looks fine"

    def test_text_blocks_are_joined_in_order(self, monkeypatch):
        self._install(monkeypatch, _reply(
            _thinking(), _text("One. "), _thinking(), _text("Two.")))
        assert ai_diagnosis._call_anthropic("sys", "user", "key") == \
            "One. Two."

    def test_a_refusal_is_an_error_that_names_its_category(self, monkeypatch):
        self._install(monkeypatch, _reply(
            stop="refusal", stop_details=SimpleNamespace(category="cyber")))
        with pytest.raises(ValueError, match=r"declined.*cyber"):
            ai_diagnosis._call_anthropic("sys", "user", "key")

    def test_a_refusal_without_details_is_still_an_error(self, monkeypatch):
        self._install(monkeypatch, _reply(stop="refusal"))
        with pytest.raises(ValueError, match="declined"):
            ai_diagnosis._call_anthropic("sys", "user", "key")

    def test_a_reply_cut_at_max_tokens_is_an_error(self, monkeypatch):
        self._install(monkeypatch, _reply(
            _thinking(), _text("Half a sent"), stop="max_tokens"))
        with pytest.raises(ValueError, match="cut off"):
            ai_diagnosis._call_anthropic("sys", "user", "key")

    def test_a_reply_with_no_text_is_an_error(self, monkeypatch):
        self._install(monkeypatch, _reply(_thinking()))
        with pytest.raises(ValueError, match="no text"):
            ai_diagnosis._call_anthropic("sys", "user", "key")

    def test_the_request_leaves_room_for_thinking(self, monkeypatch):
        fake = self._install(monkeypatch, _reply(_text("ok")))
        ai_diagnosis._call_anthropic("sys", "user", "key")
        assert fake.request["system"] == "sys"
        assert fake.request["messages"] == [
            {"role": "user", "content": "user"}]
        # 2000 was the cap when the first block was always text.
        assert fake.request["max_tokens"] >= 16000

    def test_the_card_is_the_llms_when_it_thought_first(self, monkeypatch):
        self._install(monkeypatch, _reply(
            _thinking(), _text("All looks fine\n- ok")))
        card = _diagnose(_results("good"))
        assert card.provider == "anthropic"
        assert card.summary == "All looks fine"

    def test_a_refusal_gives_the_rule_engine_and_says_why(
            self, monkeypatch, capsys):
        self._install(monkeypatch, _reply(stop="refusal"))
        card = _diagnose(_results("good"))
        assert card.provider == "rule"
        assert "declined" in card.details[0]
        assert "declined" in capsys.readouterr().err


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
