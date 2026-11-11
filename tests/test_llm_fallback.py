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


def _diagnose(results, provider="anthropic", api_key="key", score=82,
              model="some-model", base_url=""):
    return generate_diagnosis(results, VEHICLE, score, 60.0,
                              provider=provider, api_key=api_key,
                              model=model, base_url=base_url)


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

    def test_missing_model_is_reported(self, capsys):
        card = _diagnose(_results("good"), model="")
        assert card.provider == "rule"
        note = t("ai.llm.fallback", provider="anthropic",
                 reason=t("ai.llm.no_model"))
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
        self.client: dict = {}
        self.messages = self            # client.messages.create(...)

    def Anthropic(self, **client):      # noqa: N802 - the SDK's name
        self.client = client
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
        assert ai_diagnosis._call_anthropic("sys", "user", "key", "m") == \
            "Looks fine"

    def test_text_blocks_are_joined_in_order(self, monkeypatch):
        self._install(monkeypatch, _reply(
            _thinking(), _text("One. "), _thinking(), _text("Two.")))
        assert ai_diagnosis._call_anthropic("sys", "user", "key", "m") == \
            "One. Two."

    def test_a_refusal_is_an_error_that_names_its_category(self, monkeypatch):
        self._install(monkeypatch, _reply(
            stop="refusal", stop_details=SimpleNamespace(category="cyber")))
        with pytest.raises(ValueError, match=r"declined.*cyber"):
            ai_diagnosis._call_anthropic("sys", "user", "key", "m")

    def test_a_refusal_without_details_is_still_an_error(self, monkeypatch):
        self._install(monkeypatch, _reply(stop="refusal"))
        with pytest.raises(ValueError, match="declined"):
            ai_diagnosis._call_anthropic("sys", "user", "key", "m")

    def test_a_reply_cut_at_max_tokens_is_an_error(self, monkeypatch):
        self._install(monkeypatch, _reply(
            _thinking(), _text("Half a sent"), stop="max_tokens"))
        with pytest.raises(ValueError, match="cut off"):
            ai_diagnosis._call_anthropic("sys", "user", "key", "m")

    def test_a_reply_with_no_text_is_an_error(self, monkeypatch):
        self._install(monkeypatch, _reply(_thinking()))
        with pytest.raises(ValueError, match="no text"):
            ai_diagnosis._call_anthropic("sys", "user", "key", "m")

    def test_the_request_leaves_room_for_thinking(self, monkeypatch):
        fake = self._install(monkeypatch, _reply(_text("ok")))
        ai_diagnosis._call_anthropic("sys", "user", "key", "m")
        assert fake.request["model"] == "m"
        assert fake.request["system"] == "sys"
        assert fake.request["messages"] == [
            {"role": "user", "content": "user"}]
        # 2000 was the cap when the first block was always text.
        assert fake.request["max_tokens"] >= 16000

    def test_the_address_goes_to_the_client_only_when_given(
            self, monkeypatch):
        fake = self._install(monkeypatch, _reply(_text("ok")))
        ai_diagnosis._call_anthropic("sys", "user", "key", "m")
        assert fake.client == {"api_key": "key"}

        ai_diagnosis._call_anthropic("sys", "user", "key", "m",
                                     "https://api.example.com")
        assert fake.client == {"api_key": "key",
                               "base_url": "https://api.example.com"}

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


class _FakeOpenAI:
    """Stands in for the openai module: one reply, and the request kept."""

    def __init__(self, reply):
        self.reply = reply
        self.request: dict = {}
        self.client: dict = {}
        self.chat = SimpleNamespace(completions=self)

    def OpenAI(self, **client):         # noqa: N802 - the SDK's name
        self.client = client
        return self

    def create(self, **request):
        self.request = request
        return self.reply


def _completion(content, finish: str = "stop") -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=finish, message=SimpleNamespace(content=content))])


class TestOpenAiReply:
    """Many services speak this API, and they do not all answer alike."""

    @pytest.fixture(autouse=True)
    def no_env_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    @staticmethod
    def _install(monkeypatch, reply) -> _FakeOpenAI:
        fake = _FakeOpenAI(reply)
        monkeypatch.setitem(sys.modules, "openai", fake)
        return fake

    def test_the_content_is_returned(self, monkeypatch):
        self._install(monkeypatch, _completion("Looks fine\n"))
        assert ai_diagnosis._call_openai("sys", "user", "key", "m") == \
            "Looks fine"

    def test_a_reply_spent_on_reasoning_is_an_error(self, monkeypatch):
        self._install(monkeypatch, _completion(None, finish="length"))
        with pytest.raises(ValueError, match="cut off"):
            ai_diagnosis._call_openai("sys", "user", "key", "m")

    def test_a_cut_reply_with_text_is_an_error_too(self, monkeypatch):
        self._install(monkeypatch, _completion("Half a sent", "length"))
        with pytest.raises(ValueError, match="cut off"):
            ai_diagnosis._call_openai("sys", "user", "key", "m")

    def test_a_filtered_reply_is_an_error(self, monkeypatch):
        self._install(monkeypatch, _completion(None, "content_filter"))
        with pytest.raises(ValueError, match="filtered"):
            ai_diagnosis._call_openai("sys", "user", "key", "m")

    @pytest.mark.parametrize("content", [None, "", "  \n"])
    def test_a_reply_with_no_text_is_an_error(self, monkeypatch, content):
        self._install(monkeypatch, _completion(content))
        with pytest.raises(ValueError, match="no text"):
            ai_diagnosis._call_openai("sys", "user", "key", "m")

    def test_a_reply_with_no_choices_is_an_error(self, monkeypatch):
        self._install(monkeypatch, SimpleNamespace(choices=[]))
        with pytest.raises(ValueError, match="no choices"):
            ai_diagnosis._call_openai("sys", "user", "key", "m")

    def test_the_request_names_the_model_and_no_ceiling(self, monkeypatch):
        fake = self._install(monkeypatch, _completion("ok"))
        ai_diagnosis._call_openai("sys", "user", "key", "m")
        assert fake.request == {
            "model": "m",
            "messages": [{"role": "system", "content": "sys"},
                         {"role": "user", "content": "user"}],
        }

    def test_the_address_goes_to_the_client_only_when_given(
            self, monkeypatch):
        fake = self._install(monkeypatch, _completion("ok"))
        ai_diagnosis._call_openai("sys", "user", "key", "m")
        assert fake.client == {"api_key": "key"}

        ai_diagnosis._call_openai("sys", "user", "key", "m",
                                  "https://api.example.com/v1")
        assert fake.client == {"api_key": "key",
                               "base_url": "https://api.example.com/v1"}

    def test_the_card_names_the_model_and_the_api(self, monkeypatch):
        self._install(monkeypatch, _completion("All looks fine\n- ok"))
        card = _diagnose(_results("good"), provider="openai",
                         model="some-model",
                         base_url="https://api.example.com/v1")
        assert card.provider == "openai"
        assert card.summary == "All looks fine"
        assert "some-model" in card.details[0]
        assert "openai" in card.details[0]

    def test_a_model_name_cannot_put_markup_on_the_card(self, monkeypatch):
        self._install(monkeypatch, _completion("fine"))
        card = _diagnose(_results("good"), provider="openai",
                         model="<script>x</script>")
        assert "<script>" not in card.details[0]
        assert "&lt;script&gt;" in card.details[0]

    def test_a_warning_card_says_which_model_is_not_a_source(
            self, monkeypatch):
        self._install(monkeypatch, _completion("something is off"))
        card = _diagnose(_results("good", "warning"), provider="openai",
                         model="some-model")
        assert card.status == "warning"
        assert "some-model" in card.evidence_source


class TestLlmCardText:
    """What the card shows of a reply, whatever shape the model gave it."""

    @staticmethod
    def _card(monkeypatch, reply: str, score: int = 82):
        monkeypatch.setattr(ai_diagnosis, "_call_anthropic",
                            lambda *args, **kwargs: reply)
        return _diagnose(_results("good"), score=score)

    def test_the_summary_is_the_first_line_that_says_something(
            self, monkeypatch):
        card = self._card(monkeypatch,
                          "## Overall assessment\n\nHealthy, one item to tune."
                          "\n- notch")
        assert card.provider == "anthropic"
        assert card.summary == "Healthy, one item to tune."

    @pytest.mark.parametrize("first, shown", [
        ("**Overall:** healthy", "Overall: healthy"),
        ("- Healthy flight", "Healthy flight"),
        ("> Healthy flight", "Healthy flight"),
        ("---\nHealthy flight", "Healthy flight"),
    ])
    def test_list_and_emphasis_marks_are_not_part_of_it(
            self, monkeypatch, first, shown):
        assert self._card(monkeypatch, first).summary == shown

    def test_a_reply_of_headings_only_gets_the_score(self, monkeypatch):
        card = self._card(monkeypatch, "# Report\n## Findings", score=77)
        assert card.summary == t("ai.llm.fallback_summary", score=77)

    def test_every_heading_level_is_shown_as_a_heading(self, monkeypatch):
        card = self._card(monkeypatch, "fine\n# One\n## Two\n### Three")
        assert card.details[2:] == ["<strong>━━ One ━━</strong>",
                                    "<strong>━━ Two ━━</strong>",
                                    "<strong>Three</strong>"]

    def test_markup_in_a_reply_is_escaped(self, monkeypatch):
        card = self._card(monkeypatch, "fine\n## <script>a</script>\n"
                                       "- <img src=x>\n<i>text</i>")
        assert card.details[2:] == [
            "<strong>━━ &lt;script&gt;a&lt;/script&gt; ━━</strong>",
            "&lt;img src=x&gt;", "&lt;i&gt;text&lt;/i&gt;"]


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
