"""The release verifier has to reject a release that cannot be trusted."""

from __future__ import annotations

from tests._repo import TOOLS_DIR

import sys

import pytest

sys.path.insert(0, str(TOOLS_DIR))

import verify_release  # noqa: E402

pytestmark = pytest.mark.fast

TAG = "v0.13.2"
COMMIT = "a" * 40


def _release(**overrides):
    data = {
        "tag_name": TAG,
        "draft": False,
        "immutable": True,
        "published_at": "2026-11-22T00:00:00Z",
        "assets": [
            {"name": "ardupilot_scope-0.13.2-py3-none-any.whl", "size": 500},
            {"name": "ardupilot_scope-0.13.2.tar.gz", "size": 450},
            {"name": "arduscope-v0.13.2-win64.zip", "size": 88_000_000},
        ],
    }
    data.update(overrides)
    return data


def test_a_good_release_passes():
    assert verify_release.check(_release(), TAG, COMMIT, COMMIT) == []


@pytest.mark.parametrize("field,value,needle", [
    ("draft", True, "draft"),
    ("immutable", False, "immutable"),
    ("published_at", "", "published_at"),
    ("tag_name", "v0.13.1", "tag"),
])
def test_release_state_must_be_final(field, value, needle):
    failed = verify_release.check(_release(**{field: value}), TAG,
                                  COMMIT, COMMIT)
    assert any(needle in item for item in failed)


def test_a_different_tag_commit_fails():
    failed = verify_release.check(_release(), TAG, "b" * 40, COMMIT)
    assert any("tag points at" in item for item in failed)


@pytest.mark.parametrize("suffix", [".whl", ".tar.gz", "win64.zip"])
def test_each_asset_kind_is_required(suffix):
    release = _release()
    release["assets"] = [a for a in release["assets"]
                         if not a["name"].endswith(suffix)]
    failed = verify_release.check(release, TAG, COMMIT, COMMIT)
    assert any(suffix in item for item in failed)


def test_an_empty_asset_fails():
    release = _release()
    release["assets"][0]["size"] = 0
    failed = verify_release.check(release, TAG, COMMIT, COMMIT)
    assert any("empty" in item for item in failed)


def test_duplicate_asset_names_fail():
    release = _release()
    release["assets"].append(dict(release["assets"][0]))
    failed = verify_release.check(release, TAG, COMMIT, COMMIT)
    assert any("duplicate" in item for item in failed)


def test_annotated_tag_is_peeled_to_its_commit(monkeypatch):
    replies = {
        "git/ref/tags/v0.13.2":
            '{"object": {"type": "tag", "sha": "tag-object"}}',
        "git/tags/tag-object":
            '{"object": {"type": "commit", "sha": "%s"}}' % COMMIT,
    }

    def fake_run(cmd):
        for key, value in replies.items():
            if cmd[-1].endswith(key):
                return value
        raise AssertionError(cmd)

    monkeypatch.setattr(verify_release, "_run", fake_run)
    assert verify_release._tag_commit("repo", TAG) == COMMIT


def test_main_prints_a_pass(monkeypatch, capsys):
    monkeypatch.setattr(verify_release, "_local_tag_commit",
                        lambda _tag: COMMIT)
    monkeypatch.setattr(verify_release, "_release",
                        lambda _repo, _tag: _release())
    monkeypatch.setattr(verify_release, "_tag_commit",
                        lambda _repo, _tag: COMMIT)

    assert verify_release.main([TAG]) == 0
    out = capsys.readouterr().out
    assert "published and immutable" in out
    assert "win64.zip" in out


def test_main_fails_on_a_bad_release(monkeypatch, capsys):
    monkeypatch.setattr(verify_release, "_local_tag_commit",
                        lambda _tag: COMMIT)
    monkeypatch.setattr(verify_release, "_release",
                        lambda _repo, _tag: _release(draft=True))
    monkeypatch.setattr(verify_release, "_tag_commit",
                        lambda _repo, _tag: COMMIT)

    assert verify_release.main([TAG]) == 1
    assert "FAIL" in capsys.readouterr().err


def test_main_reports_a_gh_failure(monkeypatch, capsys):
    def fail(_tag):
        raise RuntimeError("gh is not authenticated")

    monkeypatch.setattr(verify_release, "_local_tag_commit", fail)

    assert verify_release.main([TAG]) == 1
    assert "not authenticated" in capsys.readouterr().err
