"""tools/check_release_tag.py: what release.yml builds has to be a release.

The workflow starts on any push of a `v*` tag and builds what the tag points
at, under the tag's name. A malformed tag, one whose commit is not on main,
or one whose version is not the tagged commit's used to go on to a draft,
and a draft like that cannot be published: releases are immutable, so the
tag is spent. The ancestry checks run against a real repository here, so
the answers are git's own.
"""

from __future__ import annotations

from tests import _gitrepo as gitrepo
from tests._repo import TOOLS_DIR

import os
import re
import subprocess
import sys

import pytest

sys.path.insert(0, str(TOOLS_DIR))

import check_release_tag as crt  # noqa: E402

pytestmark = pytest.mark.fast


@pytest.mark.parametrize("tag, version", [
    ("v0.14.4", "0.14.4"), ("v1.0.0", "1.0.0"), ("v0.0.1", "0.0.1"),
])
def test_a_release_tag_names_its_version(tag, version):
    assert crt.tag_version(tag) == version


@pytest.mark.parametrize("tag", [
    "0.14.4",  # no v
    "v0.14",  # two numbers
    "v0.14.4.1",  # four
    "v0.14.4-rc1", "v0.14.4rc1",  # a pre-release
    "V0.14.4",  # an upper-case v
    "v0.14.4\n",  # a trailing newline, which a bare $ lets through
    "v0.14.4 ", " v0.14.4", "v0.14.a", "vv0.14.4", "",
])
def test_any_other_tag_is_not_a_release(tag):
    with pytest.raises(crt.TagError, match="not vX.Y.Z"):
        crt.tag_version(tag)


@pytest.fixture
def work(tmp_path, monkeypatch):
    """A clone on main whose version file says 0.0.1, under a bare origin."""
    gitrepo.hermetic(monkeypatch)
    work, _origin = gitrepo.repository(tmp_path, version="0.0.1")
    monkeypatch.chdir(work)
    return work


def _release(work, version: str, *, push=True, annotated=False) -> str:
    """Bump the version file, commit, and tag: the tag release.py makes."""
    gitrepo.write_version(work, version)
    gitrepo.git(work, "commit", "-q", "-am", f"chore: release v{version}")
    if push:
        gitrepo.git(work, "push", "-q", "origin", "main")
    tag = f"v{version}"
    gitrepo.git(work, "tag", *(["-a", "-m", tag] if annotated else []), tag)
    return tag


class TestTheTaggedCommit:
    """The tag has to agree with the commit it points at, and that commit
    has to be on main."""

    def test_a_tag_on_main_whose_version_matches_passes(self, work):
        assert crt.check(_release(work, "0.0.2")) == "0.0.2"

    def test_an_annotated_tag_is_read_by_its_commit(self, work):
        assert crt.check(_release(work, "0.0.2", annotated=True)) == "0.0.2"

    def test_a_tag_whose_version_is_not_the_commits_fails(self, work):
        """Tagged by hand, or moved: the name and the code disagree."""
        _release(work, "0.0.2")
        gitrepo.git(work, "tag", "v0.0.3")
        with pytest.raises(crt.TagError, match=(
                r"tag v0.0.3 releases 0\.0\.3, but "
                r"src/arduscope/__init__\.py says 0\.0\.2")):
            crt.check("v0.0.3")

    def test_a_tag_on_an_unpushed_commit_fails(self, work):
        """Level with origin is the release's own rule; this one is the
        workflow's, and a tag made from a clone that never pushed meets it
        here."""
        tag = _release(work, "0.0.2", push=False)
        with pytest.raises(crt.TagError, match="not on origin/main"):
            crt.check(tag)

    def test_a_tag_on_a_branch_fails(self, work):
        """Checked out where it was made, as the workflow checks a tag out:
        name and version agree, and main has never seen the commit."""
        gitrepo.git(work, "checkout", "-q", "-b", "side")
        tag = _release(work, "0.0.2", push=False)
        with pytest.raises(crt.TagError, match="not on origin/main"):
            crt.check(tag)

    def test_a_tag_that_does_not_exist_here_is_an_error_not_a_verdict(
            self, work):
        """The working tree can say 0.0.2 without any tag naming it."""
        gitrepo.write_version(work, "0.0.2")
        with pytest.raises(crt.TagError, match="could not place "
                                               r"refs/tags/v0\.0\.2"):
            crt.check("v0.0.2")

    def test_no_origin_main_here_is_an_error_not_a_verdict(self, work):
        """A checkout that never fetched main cannot say the tag is off it."""
        tag = _release(work, "0.0.2")
        gitrepo.git(work, "update-ref", "-d", "refs/remotes/origin/main")
        with pytest.raises(crt.TagError, match="could not place"):
            crt.check(tag)

    def test_a_version_file_with_no_version_fails(self, work, tmp_path):
        (work / gitrepo.VERSION_FILE).write_text("# nothing\n",
                                                 encoding="utf-8")
        with pytest.raises(crt.TagError, match="no __version__"):
            crt.check("v0.0.2")

    def test_a_missing_version_file_fails(self, work, monkeypatch):
        monkeypatch.chdir(work.parent)
        with pytest.raises(OSError):
            crt.check("v0.0.2")

    def test_a_malformed_tag_is_refused_before_git_is_asked(self, work):
        with pytest.raises(crt.TagError, match="not vX.Y.Z"):
            crt.check("v0.14")


class TestTheStepOutput:
    def test_the_version_goes_to_github_output_and_to_the_log(
            self, tmp_path, capsys):
        output = tmp_path / "output"
        crt.emit("0.14.4", str(output))
        crt.emit("0.14.5", str(output))  # a step appends to the file
        assert output.read_text(encoding="utf-8") == (
            "version=0.14.4\nversion=0.14.5\n")
        assert capsys.readouterr().out == "version=0.14.4\nversion=0.14.5\n"

    def test_without_an_output_file_the_version_is_printed(self, capsys):
        crt.emit("0.14.4", None)
        assert capsys.readouterr().out == "version=0.14.4\n"


class TestMain:
    def test_the_tag_comes_from_the_event(self, work, monkeypatch, capsys):
        tag = _release(work, "0.0.2")
        monkeypatch.setenv("GITHUB_REF_NAME", tag)
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        assert crt.main([]) == 0
        assert capsys.readouterr().out == "version=0.0.2\n"

    def test_the_version_is_written_to_the_step_output(self, work, monkeypatch,
                                                       tmp_path):
        tag = _release(work, "0.0.2")
        output = tmp_path / "output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        assert crt.main(["--tag", tag]) == 0
        assert output.read_text(encoding="utf-8") == "version=0.0.2\n"

    @pytest.mark.parametrize("tag", ["v0.14", "0.0.2", "v0.0.3"])
    def test_a_tag_that_cannot_be_built_fails(self, work, monkeypatch, capsys,
                                              tag):
        _release(work, "0.0.2")
        assert crt.main(["--tag", tag]) == 1
        out, err = capsys.readouterr()
        assert err.startswith("FAIL ") and out == ""

    def test_nothing_names_the_tag(self, work, monkeypatch, capsys):
        monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
        assert crt.main([]) == 1
        assert "no tag to check" in capsys.readouterr().err

    def test_a_bad_tag_writes_nothing_to_the_step_output(self, work, monkeypatch,
                                                         tmp_path):
        """An empty output is what the workflow reads as a version."""
        _release(work, "0.0.2")
        output = tmp_path / "output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        assert crt.main(["--tag", "v0.0.3"]) == 1
        assert not output.exists()


class TestTheWorkflowStep:
    """release.yml reads: the check cannot be dropped from it, or moved
    behind the build, unnoticed."""

    WORKFLOW = (TOOLS_DIR.parent / ".github" / "workflows" / "release.yml"
                ).read_text()

    def test_the_tag_is_checked_before_anything_is_built(self):
        """A malformed tag, one off main, or one whose version is not the
        tagged commit's stops here: no wheel, no zip, no draft."""
        workflow = self.WORKFLOW
        checked = workflow.index("tools/check_release_tag.py")
        for later in ("python -m build", "build_windows_package.py",
                      "softprops/action-gh-release"):
            assert checked < workflow.index(later), later

    def test_the_check_sees_main(self):
        """It places the tagged commit against origin/main, which takes the
        whole history and a fetch of main, before it runs."""
        build_job = self.WORKFLOW.split("\n  build:", 1)[1]
        checkout, _ = build_job.split("tools/check_release_tag.py", 1)
        assert re.search(r"fetch-depth: 0$", checkout, re.M)
        assert "git fetch origin main" in checkout

    def test_the_version_the_later_steps_read_comes_from_the_check(self):
        """The zip and the notes read steps.version.outputs.version. Only
        the check writes it now: it strips the v once it has agreed with
        the tag, and writes nothing when it has not."""
        assert re.search(
            r"id: version\n\s+run: python3 tools/check_release_tag.py",
            self.WORKFLOW)
        assert self.WORKFLOW.count("id: version\n") == 1
        assert "GITHUB_REF#" not in self.WORKFLOW


class TestAsAProcess:
    """The workflow runs it as a command: exit status and streams matter."""

    def _run(self, work, *args):
        env = {**os.environ, "GITHUB_OUTPUT": str(work / "output")}
        env.pop("GITHUB_REF_NAME", None)
        return subprocess.run(
            [sys.executable, str(TOOLS_DIR / "check_release_tag.py"), *args],
            cwd=str(work), env=env, capture_output=True, text=True)

    def test_a_good_tag_exits_zero_and_names_the_version(self, work):
        _release(work, "0.0.2")
        done = self._run(work, "--tag", "v0.0.2")
        assert done.returncode == 0, done.stderr
        assert done.stdout == "version=0.0.2\n"
        assert (work / "output").read_text(encoding="utf-8") == (
            "version=0.0.2\n")

    def test_a_bad_tag_exits_one_with_the_reason_on_stderr(self, work):
        _release(work, "0.0.2")
        done = self._run(work, "--tag", "v0.0.3")
        assert done.returncode == 1
        assert done.stdout == "" and not (work / "output").exists()
        assert "FAIL tag v0.0.3 releases 0.0.3" in done.stderr
