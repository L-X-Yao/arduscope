"""The release verifier has to reject a release that cannot be trusted."""

from __future__ import annotations

from tests._repo import TOOLS_DIR

import json
import re
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(TOOLS_DIR))

import verify_release  # noqa: E402

pytestmark = pytest.mark.fast

TAG = "v0.13.2"
COMMIT = "a" * 40
WHEEL = "ardupilot_scope-0.13.2-py3-none-any.whl"
SDIST = "ardupilot_scope-0.13.2.tar.gz"
ZIP = "arduscope-v0.13.2-win64.zip"


def _release(**overrides):
    data = {
        "tag_name": TAG,
        "draft": False,
        "immutable": True,
        "published_at": "2026-11-22T00:00:00Z",
        "assets": [
            {"name": WHEEL, "size": 500},
            {"name": SDIST, "size": 450},
            {"name": ZIP, "size": 88_000_000},
        ],
    }
    data.update(overrides)
    return data


def _check(release, remote=COMMIT, local=COMMIT, on_main=True):
    return verify_release.check(release, TAG, remote, local, on_main=on_main)


def test_a_good_release_passes():
    assert _check(_release()) == []


def test_the_three_assets_are_known_by_name():
    """The sdist's dot is an extension's, not a fourth number."""
    assert verify_release.expected_assets("0.13.2") == {
        WHEEL: "wheel", SDIST: "sdist", ZIP: "windows"}


@pytest.mark.parametrize("field,value,needle", [
    ("draft", True, "draft"),
    ("immutable", False, "immutable"),
    ("published_at", "", "published_at"),
    ("tag_name", "v0.13.1", "tag"),
])
def test_release_state_must_be_final(field, value, needle):
    failed = _check(_release(**{field: value}))
    assert any(needle in item for item in failed)


def test_a_different_tag_commit_fails():
    failed = _check(_release(), remote="b" * 40)
    assert any("tag points at" in item for item in failed)


@pytest.mark.parametrize("name, kind", [(WHEEL, "wheel"), (SDIST, "sdist"),
                                        (ZIP, "windows")])
def test_each_asset_is_required_by_name(name, kind):
    release = _release()
    release["assets"] = [a for a in release["assets"] if a["name"] != name]
    assert _check(release) == [f"no {kind} asset ({name})"]


def test_an_empty_asset_fails():
    release = _release()
    release["assets"][0]["size"] = 0
    assert _check(release) == [f"wheel asset is empty: {WHEEL}"]


def test_duplicate_asset_names_fail():
    release = _release()
    release["assets"].append(dict(release["assets"][0]))
    assert _check(release) == ["release has duplicate asset names"]


@pytest.mark.parametrize("name", [
    # A pre-release, a post-release and a local version carry 0.13.2 inside
    # their names, and are other releases.
    "ardupilot_scope-0.13.2rc1-py3-none-any.whl",
    "ardupilot_scope-0.13.2.post1-py3-none-any.whl",
    "ardupilot_scope-0.13.2+local-py3-none-any.whl",
    # A neighbouring number, another tag, and the same name for the sdist.
    "ardupilot_scope-0.13.21-py3-none-any.whl",
    "ardupilot_scope-0.13.2-py2-none-any.whl",
    "ardupilot_scope-0.13.2.zip",
])
def test_a_wheel_whose_name_differs_in_anything_is_not_of_this_release(
        name):
    release = _release()
    release["assets"][0]["name"] = name
    assert _check(release) == [
        f"no wheel asset ({WHEEL})",
        f"asset {name!r} is not one of v0.13.2's three"]


def test_a_sdist_from_another_version_fails_though_its_kind_is_there():
    release = _release()
    release["assets"][1]["name"] = "ardupilot_scope-0.13.1.tar.gz"
    assert _check(release) == [
        f"no sdist asset ({SDIST})",
        "asset 'ardupilot_scope-0.13.1.tar.gz' is not one of v0.13.2's three"]


@pytest.mark.parametrize("name", [
    "release-notes.md",  # not a package of this release
    "checksums.txt",  # not a package of any release yet
    "arduscope-v0.13.2-win64.zip.sha256",  # not the zip under another suffix
])
def test_an_extra_asset_is_rejected(name):
    release = _release()
    release["assets"].append({"name": name, "size": 10})
    assert _check(release) == [
        f"asset {name!r} is not one of v0.13.2's three"]


def test_a_tag_commit_off_main_fails():
    """A tag pushed from anywhere but main would verify on its own merits:
    right tag name, right commit, three assets."""
    assert _check(_release(), on_main=False) == [
        f"tag commit {COMMIT} is not on main"]


class TestWhichMainIsAsked:
    """The repository named by --repo is the one whose main answers. The
    clone this runs in can be of another one, a fork, and its origin/main
    says nothing about it."""

    @pytest.mark.parametrize("status, expected", [
        ("ahead", True), ("identical", True),
        ("behind", False), ("diverged", False), (None, False),
    ])
    def test_main_containing_the_commit_is_read_from_the_comparison(
            self, monkeypatch, status, expected):
        """ahead: main has the commit behind its tip. identical: it is the
        tip. Anything else, no status included, is not a yes."""
        asked = []
        monkeypatch.setattr(verify_release, "_run", lambda cmd: (
            asked.append(cmd) or json.dumps({"status": status})))
        assert verify_release._on_main("someone/arduscope-fork",
                                       COMMIT) is expected
        assert asked == [["gh", "api",
                          f"repos/someone/arduscope-fork/compare/"
                          f"{COMMIT}...main"]]

    def test_a_fork_is_asked_about_its_own_main_not_the_local_origin(
            self, monkeypatch, capsys):
        asked = []

        def answer(cmd):
            asked.append(cmd)
            if cmd[0] == "git":
                return COMMIT + "\n"
            if "/compare/" in cmd[-1]:
                return json.dumps({"status": "diverged"})
            if "/git/ref/" in cmd[-1]:
                return json.dumps({"object": {"type": "commit",
                                              "sha": COMMIT}})
            return json.dumps(_release())

        monkeypatch.setattr(verify_release.subprocess, "run",
                            lambda cmd, **kwargs: SimpleNamespace(
                                returncode=0, stdout=answer(cmd), stderr=""))
        fork = "someone/arduscope-fork"
        assert verify_release.main([TAG, "--repo", fork]) == 1
        assert f"FAIL tag commit {COMMIT} is not on main" in \
            capsys.readouterr().err
        asked_github = [cmd for cmd in asked if cmd[0] == "gh"]
        assert len(asked_github) == 3
        assert all(cmd[2].startswith(f"repos/{fork}/") for cmd in asked_github)
        # The one git question is the local tag's commit, by its full name.
        assert [cmd for cmd in asked if cmd[0] == "git"] == [
            ["git", "rev-parse", "--verify", f"refs/tags/{TAG}^{{commit}}"]]


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


def test_the_verifier_s_checkout_has_the_tag():
    """verify_release.py reads the tag's commit from the local tag, which a
    checkout of one commit does not bring."""
    workflow = (TOOLS_DIR.parent / ".github" / "workflows" / "release-verify.yml"
                ).read_text()
    checkout = workflow.split("uses: actions/checkout", 1)[1].split(
        "\n      - ", 1)[0]
    assert re.search(r"fetch-depth: 0$", checkout, re.M)


def _published(monkeypatch, **overrides):
    """Everything verify_release.main() asks of git and GitHub, answered for
    a published release on the main of the repository asked about."""
    asked = []
    monkeypatch.setattr(verify_release, "_local_tag_commit",
                        lambda _tag: COMMIT)
    monkeypatch.setattr(verify_release, "_on_main",
                        lambda repo, commit: asked.append((repo, commit))
                        or True)
    monkeypatch.setattr(verify_release, "_release",
                        lambda _repo, _tag: _release(**overrides))
    monkeypatch.setattr(verify_release, "_tag_commit",
                        lambda _repo, _tag: COMMIT)
    return asked


def test_main_prints_a_pass(monkeypatch, capsys):
    asked = _published(monkeypatch)

    assert verify_release.main([TAG]) == 0
    out = capsys.readouterr().out
    assert "published and immutable" in out
    assert f"tag commit is on main of {verify_release.DEFAULT_REPO}" in out
    assert out.count("PASS ") == 6 and ZIP in out
    assert asked == [(verify_release.DEFAULT_REPO, COMMIT)]


def test_main_fails_on_a_bad_release(monkeypatch, capsys):
    _published(monkeypatch, draft=True)

    assert verify_release.main([TAG]) == 1
    assert "FAIL" in capsys.readouterr().err


def test_main_fails_on_a_tag_commit_off_main(monkeypatch, capsys):
    _published(monkeypatch)
    monkeypatch.setattr(verify_release, "_on_main",
                        lambda _repo, _commit: False)

    assert verify_release.main([TAG]) == 1
    assert "is not on main" in capsys.readouterr().err


def test_main_fails_on_an_asset_from_another_version(monkeypatch, capsys):
    release = _release()
    release["assets"][0]["name"] = "ardupilot_scope-0.13.2rc1-py3-none-any.whl"
    _published(monkeypatch, assets=release["assets"])

    assert verify_release.main([TAG]) == 1
    err = capsys.readouterr().err
    assert "is not one of v0.13.2's three" in err


def test_main_reports_a_gh_failure(monkeypatch, capsys):
    def fail(_tag):
        raise RuntimeError("gh is not authenticated")

    monkeypatch.setattr(verify_release, "_local_tag_commit", fail)

    assert verify_release.main([TAG]) == 1
    assert "not authenticated" in capsys.readouterr().err


def test_main_reports_a_repository_it_cannot_ask(monkeypatch, capsys):
    """A repository that cannot be compared is not a release off main: it
    is said as such."""
    _published(monkeypatch)

    def lost(repo, commit):
        raise RuntimeError(
            f"gh: Could not resolve to a Repository with the name '{repo}'")

    monkeypatch.setattr(verify_release, "_on_main", lost)

    assert verify_release.main([TAG]) == 1
    assert "Could not resolve" in capsys.readouterr().err
