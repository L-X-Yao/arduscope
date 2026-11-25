"""The release verifier has to reject a release that cannot be trusted."""

from __future__ import annotations

from tests import _gitrepo as gitrepo
from tests._repo import TOOLS_DIR

import re
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
    assert verify_release.check(_release(), TAG, COMMIT, COMMIT,
                                on_main=True) == []


@pytest.mark.parametrize("field,value,needle", [
    ("draft", True, "draft"),
    ("immutable", False, "immutable"),
    ("published_at", "", "published_at"),
    ("tag_name", "v0.13.1", "tag"),
])
def test_release_state_must_be_final(field, value, needle):
    failed = verify_release.check(_release(**{field: value}), TAG,
                                  COMMIT, COMMIT, on_main=True)
    assert any(needle in item for item in failed)


def test_a_different_tag_commit_fails():
    failed = verify_release.check(_release(), TAG, "b" * 40, COMMIT,
                                  on_main=True)
    assert any("tag points at" in item for item in failed)


@pytest.mark.parametrize("suffix", [".whl", ".tar.gz", "win64.zip"])
def test_each_asset_kind_is_required(suffix):
    release = _release()
    release["assets"] = [a for a in release["assets"]
                         if not a["name"].endswith(suffix)]
    failed = verify_release.check(release, TAG, COMMIT, COMMIT, on_main=True)
    assert any(suffix in item for item in failed)


def test_an_empty_asset_fails():
    release = _release()
    release["assets"][0]["size"] = 0
    failed = verify_release.check(release, TAG, COMMIT, COMMIT, on_main=True)
    assert any("empty" in item for item in failed)


def test_duplicate_asset_names_fail():
    release = _release()
    release["assets"].append(dict(release["assets"][0]))
    failed = verify_release.check(release, TAG, COMMIT, COMMIT, on_main=True)
    assert any("duplicate" in item for item in failed)


def test_a_tag_commit_off_main_fails():
    """A tag pushed from anywhere but main would verify on its own merits:
    right tag name, right commit, three assets."""
    failed = verify_release.check(_release(), TAG, COMMIT, COMMIT,
                                  on_main=False)
    assert failed == [f"tag commit {COMMIT} is not on origin/main"]


def test_a_wheel_from_another_version_fails_though_its_kind_is_there():
    """Each kind was checked for, never named: a 0.13.1 wheel under a 0.13.2
    release has the right suffix and the wrong version."""
    release = _release()
    release["assets"][0]["name"] = "ardupilot_scope-0.13.1-py3-none-any.whl"
    failed = verify_release.check(release, TAG, COMMIT, COMMIT, on_main=True)
    assert failed == ["asset 'ardupilot_scope-0.13.1-py3-none-any.whl' "
                      "does not name version 0.13.2"]


@pytest.mark.parametrize("name", [
    "arduscope-win64.zip",  # no version at all
    "release-notes.md",  # a file that is not a package of this release
])
def test_an_asset_that_names_no_version_fails(name):
    release = _release()
    release["assets"].append({"name": name, "size": 10})
    failed = verify_release.check(release, TAG, COMMIT, COMMIT, on_main=True)
    assert failed == [f"asset {name!r} does not name version 0.13.2"]


@pytest.mark.parametrize("version, name, expected", [
    ("0.13.2", "ardupilot_scope-0.13.2-py3-none-any.whl", True),
    ("0.13.2", "ardupilot_scope-0.13.2.tar.gz", True),
    ("0.13.2", "arduscope-v0.13.2-win64.zip", True),
    ("0.13.2", "ardupilot_scope-0.13.21-py3-none-any.whl", False),
    ("0.13.2", "ardupilot_scope-0.13.20.tar.gz", False),
    ("0.13.2", "ardupilot_scope-0.13.2.1.tar.gz", False),
    ("0.13.2", "ardupilot_scope-10.13.2.tar.gz", False),
    ("0.13.2", "ardupilot_scope-1.0.13.2.tar.gz", False),
])
def test_the_version_has_to_be_whole_in_the_name(version, name, expected):
    """0.13.2 is spelled inside 0.13.21, 10.13.2 and 0.13.2.1: a substring
    is not a version. The sdist's own dot is an extension's, not a fourth
    number, and has to pass."""
    assert verify_release._names_version(name, version) is expected


class TestOnMain:
    """_on_main against a real repository: the exit codes are git's own."""

    @pytest.fixture
    def work(self, tmp_path, monkeypatch):
        gitrepo.hermetic(monkeypatch)
        work, _origin = gitrepo.repository(tmp_path)
        monkeypatch.chdir(work)
        return work

    def test_a_commit_main_holds_is_on_main(self, work):
        head = gitrepo.git(work, "rev-parse", "HEAD")
        assert verify_release._on_main(head)

    def test_a_commit_main_does_not_hold_is_not(self, work):
        gitrepo.commit(work, "made elsewhere")
        head = gitrepo.git(work, "rev-parse", "HEAD")
        assert not verify_release._on_main(head)

    def test_a_missing_main_ref_is_an_error_not_an_answer(self, work):
        with pytest.raises(RuntimeError, match="origin/nope"):
            verify_release._on_main("HEAD", "origin/nope")


def test_the_verifier_s_checkout_sees_main():
    """verify_release.py places the tag commit against origin/main: a
    checkout of one commit has no such ref."""
    workflow = (TOOLS_DIR.parent / ".github" / "workflows" / "release-verify.yml"
                ).read_text()
    checkout = workflow.split("uses: actions/checkout", 1)[1].split(
        "\n      - ", 1)[0]
    assert re.search(r"fetch-depth: 0$", checkout, re.M)


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


def _published(monkeypatch, **overrides):
    """Everything verify_release.main() asks of git and GitHub, answered for
    a published release on main."""
    monkeypatch.setattr(verify_release, "_local_tag_commit",
                        lambda _tag: COMMIT)
    monkeypatch.setattr(verify_release, "_on_main", lambda _commit: True)
    monkeypatch.setattr(verify_release, "_release",
                        lambda _repo, _tag: _release(**overrides))
    monkeypatch.setattr(verify_release, "_tag_commit",
                        lambda _repo, _tag: COMMIT)


def test_main_prints_a_pass(monkeypatch, capsys):
    _published(monkeypatch)

    assert verify_release.main([TAG]) == 0
    out = capsys.readouterr().out
    assert "published and immutable" in out
    assert "is on origin/main" in out
    assert "win64.zip" in out


def test_main_fails_on_a_bad_release(monkeypatch, capsys):
    _published(monkeypatch, draft=True)

    assert verify_release.main([TAG]) == 1
    assert "FAIL" in capsys.readouterr().err


def test_main_fails_on_a_tag_commit_off_main(monkeypatch, capsys):
    _published(monkeypatch)
    monkeypatch.setattr(verify_release, "_on_main", lambda _commit: False)

    assert verify_release.main([TAG]) == 1
    assert "not on origin/main" in capsys.readouterr().err


def test_main_fails_on_an_asset_from_another_version(monkeypatch, capsys):
    release = _release()
    release["assets"][0]["name"] = "ardupilot_scope-0.13.1-py3-none-any.whl"
    _published(monkeypatch, assets=release["assets"])

    assert verify_release.main([TAG]) == 1
    assert "does not name version 0.13.2" in capsys.readouterr().err


def test_main_reports_a_gh_failure(monkeypatch, capsys):
    def fail(_tag):
        raise RuntimeError("gh is not authenticated")

    monkeypatch.setattr(verify_release, "_local_tag_commit", fail)

    assert verify_release.main([TAG]) == 1
    assert "not authenticated" in capsys.readouterr().err


def test_main_reports_a_main_that_cannot_be_found(monkeypatch, capsys):
    """No origin/main here is not a release off main: it is said as such."""
    _published(monkeypatch)

    def lost(commit):
        raise RuntimeError(
            f"could not place {commit[:10]} against origin/main")

    monkeypatch.setattr(verify_release, "_on_main", lost)

    assert verify_release.main([TAG]) == 1
    assert f"could not place {COMMIT[:10]} against origin/main" in \
        capsys.readouterr().err
