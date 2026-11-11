"""flight_report refuses arguments it cannot honour, before reading the log."""

from __future__ import annotations

import sys
from unittest import mock

import pytest

import flight_report
from lib.i18n import set_locale
from tests.binlog import flight_log


@pytest.fixture(autouse=True)
def _restore_locale():
    yield
    set_locale("zh")


def _main(*argv: str) -> None:
    with mock.patch.object(sys, "argv", ["flight_report.py", *argv]):
        flight_report.main()


@pytest.fixture
def log_path(tmp_path):
    return flight_log(duration_s=8.0).save(tmp_path / "flight.BIN")


class TestAnalyzerKeys:
    """A typo used to give an empty report with exit code 0."""

    @pytest.mark.parametrize("option", ["--only", "--skip"])
    def test_unknown_key_is_refused_by_name(self, option, log_path, tmp_path,
                                            capsys):
        out = tmp_path / "out.html"

        with pytest.raises(SystemExit) as stop:
            _main(log_path, option, "summary,sumary", "--no-open",
                  "--no-fleet-record", "--lang", "en", "-o", str(out))

        assert stop.value.code == 1
        err = capsys.readouterr().err
        assert "sumary" in err
        assert "vibration" in err      # the valid keys are listed
        assert not out.exists()

    def test_valid_keys_still_run(self, log_path, tmp_path):
        out = tmp_path / "out.html"

        _main(log_path, "--only", " summary , attitude ", "--skip", "motor",
              "--no-open", "--no-fleet-record", "--no-ai", "-o", str(out))

        assert out.exists()


class TestThresholdsFile:
    """--thresholds used to ignore a missing file, bad JSON and bad keys,
    so a run could silently use the default limits."""

    @pytest.mark.parametrize("content, named", [
        (None, "custom.json"),
        ("{not json", "custom.json"),
        ('["vibe_warn"]', "custom.json"),
        ('{"vibe_wran": 20}', "vibe_wran"),
        ('{"vibe_warn": "high"}', "vibe_warn"),
        ('{"vibe_warn": true}', "vibe_warn"),
        ('{"vibe_warn": NaN}', "vibe_warn"),
        ('{"gps_nsats_warn": 7.5}', "gps_nsats_warn"),
    ])
    def test_unusable_file_is_refused(self, content, named, tmp_path, capsys,
                                      log_path):
        path = tmp_path / "custom.json"
        if content is not None:
            path.write_text(content, encoding="utf-8")
        out = tmp_path / "out.html"

        with pytest.raises(SystemExit) as stop:
            _main(log_path, "--thresholds", str(path), "--no-open",
                  "--no-fleet-record", "--lang", "en", "-o", str(out))

        assert stop.value.code == 1
        assert named in capsys.readouterr().err
        assert not out.exists()

    @pytest.mark.fast
    def test_valid_file_is_applied(self, tmp_path):
        from lib.thresholds import Thresholds
        path = tmp_path / "custom.json"
        path.write_text('{"vibe_warn": 20.0, "gps_nsats_warn": 8}',
                        encoding="utf-8")
        thresholds = Thresholds()
        assert thresholds.vibe_warn != 20.0

        thresholds.load_overrides(str(path))

        assert thresholds.vibe_warn == 20.0
        assert thresholds.gps_nsats_warn == 8
        assert isinstance(thresholds.gps_nsats_warn, int)


class TestApiKey:
    """A key in argv can be read by every process on the machine (ps)."""

    def test_key_on_the_command_line_is_refused(self, log_path, tmp_path,
                                                capsys):
        out = tmp_path / "out.html"

        with pytest.raises(SystemExit) as stop:
            _main(log_path, "--ai-provider", "anthropic",
                  "--ai-key", "sk-secret", "--no-open", "--no-fleet-record",
                  "--lang", "en", "-o", str(out))

        assert stop.value.code == 1
        err = capsys.readouterr().err
        assert "ANTHROPIC_API_KEY" in err
        assert "sk-secret" not in err
        assert not out.exists()

    def test_option_is_gone_from_help(self, capsys):
        with pytest.raises(SystemExit):
            _main("--help")

        assert "--ai-key" not in capsys.readouterr().out

    def test_environment_key_reaches_the_provider(self, log_path, tmp_path,
                                                  monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
        seen = []

        def fake_call(system, user, api_key, *args, **kwargs):
            seen.append(api_key)
            return "All clear."

        monkeypatch.setattr("lib.ai_diagnosis._call_anthropic", fake_call)

        _main(log_path, "--ai-provider", "anthropic", "--ai-model", "m",
              "--only", "summary", "--no-open", "--no-fleet-record", "-o",
              str(tmp_path / "out.html"))

        assert seen == ["sk-from-env"]


class TestLlmEndpoint:
    """No model name is built in, and an option that does nothing is refused."""

    @pytest.fixture(autouse=True)
    def keys(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-o")

    @pytest.fixture
    def calls(self, monkeypatch):
        seen = []

        def fake_call(system, user, api_key, model, base_url):
            seen.append((api_key, model, base_url))
            return "All clear."

        monkeypatch.setattr("lib.ai_diagnosis._call_anthropic", fake_call)
        monkeypatch.setattr("lib.ai_diagnosis._call_openai", fake_call)
        return seen

    @pytest.mark.parametrize("provider", ["anthropic", "openai"])
    def test_a_provider_without_a_model_is_refused(
            self, provider, log_path, tmp_path, capsys, calls):
        out = tmp_path / "out.html"

        with pytest.raises(SystemExit) as stop:
            _main(log_path, "--ai-provider", provider, "--no-open",
                  "--no-fleet-record", "--lang", "en", "-o", str(out))

        assert stop.value.code == 1
        err = capsys.readouterr().err
        assert "--ai-model" in err
        assert provider in err
        assert calls == []
        assert not out.exists()

    @pytest.mark.parametrize("option, value", [
        ("--ai-model", "m"), ("--ai-base-url", "https://api.example.com/v1")])
    def test_an_option_without_a_provider_is_refused(
            self, option, value, log_path, tmp_path, capsys):
        out = tmp_path / "out.html"

        with pytest.raises(SystemExit) as stop:
            _main(log_path, option, value, "--no-open", "--no-fleet-record",
                  "--lang", "en", "-o", str(out))

        assert stop.value.code == 1
        assert option in capsys.readouterr().err
        assert not out.exists()

    @pytest.mark.parametrize("url", ["api.example.com/v1", "ftp://x", ""])
    def test_an_address_that_is_not_http_is_refused(
            self, url, log_path, tmp_path, capsys, calls):
        out = tmp_path / "out.html"

        with pytest.raises(SystemExit) as stop:
            _main(log_path, "--ai-provider", "openai", "--ai-model", "m",
                  "--ai-base-url", url, "--no-open", "--no-fleet-record",
                  "--lang", "en", "-o", str(out))

        assert stop.value.code == 1
        assert "--ai-base-url" in capsys.readouterr().err
        assert calls == []
        assert not out.exists()

    def test_model_and_address_reach_the_call(self, log_path, tmp_path,
                                              calls):
        _main(log_path, "--ai-provider", "openai", "--ai-model", "some-model",
              "--ai-base-url", "https://api.example.com/v1",
              "--only", "summary", "--no-open", "--no-fleet-record", "-o",
              str(tmp_path / "out.html"))

        assert calls == [("sk-o", "some-model", "https://api.example.com/v1")]

    def test_without_an_address_the_provider_keeps_its_own(
            self, log_path, tmp_path, calls):
        _main(log_path, "--ai-provider", "anthropic", "--ai-model", "m",
              "--only", "summary", "--no-open", "--no-fleet-record", "-o",
              str(tmp_path / "out.html"))

        assert calls == [("sk-a", "m", "")]
