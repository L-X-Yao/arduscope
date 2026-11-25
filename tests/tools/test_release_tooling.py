"""
Regression tests for the release tooling and the commit-msg hook that
gates it.

release.py used to commit with a bare "release: v..." subject, which the
commit-msg hook rejects (release is not in the conventional-type
whitelist) — so the script died mid-run. It now also refuses a commit
without a pass from tools/release_gate.py, and releases the notes written
under "## [Unreleased]" rather than generating them.

It also released from any branch, asked about the push once the commit and
the tag existed (a run with no terminal to answer left both unpushed), and
did not look at origin before making the tag. The pre-flight is tested at
two levels: main() with git answered from a table, for the order of the
questions and for what is written when one is refused; and the questions
put to real git, against a bare origin under tmp_path, because a mock only
repeats what a test already believes about git's answers.
"""

from __future__ import annotations

from tests import _gitrepo as gitrepo
from tests._repo import REPO_ROOT

import builtins
import hashlib
import importlib.util
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.fast

REPO = REPO_ROOT
HOOK = REPO / "tools" / "commit-msg-hook.sh"


def _run_hook(subject: str, tmp_path) -> int:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(subject + "\n")
    return subprocess.run(
        ["bash", str(HOOK), str(msg)], capture_output=True
    ).returncode


def _load_release():
    spec = importlib.util.spec_from_file_location(
        "release_mod", REPO / "tools" / "release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # top-level only; main() is __name__-guarded
    return mod


class TestCommitMsgHook:

    def test_accepts_release_commit(self, tmp_path):
        # The subject release.py now emits must pass the hook.
        assert _run_hook("chore: release v0.5.0", tmp_path) == 0

    def test_rejects_bare_release_prefix(self, tmp_path):
        # The subject release.py *used* to emit — proves the bug was real
        # (and that this test would have caught it).
        assert _run_hook("release: v0.5.0", tmp_path) != 0


class TestReleasePy:

    def test_commit_uses_chore_prefix(self):
        src = (REPO / "tools" / "release.py").read_text()
        assert 'f"chore: release v{new_ver}"' in src
        assert 'f"release: v{new_ver}"' not in src

    CHANGELOG = (
        "# Changelog\n\n"
        "## [Unreleased]\n\n### Fixed\n- new\n\n"
        "## [0.4.0] - 2026-07-15\n\n### Fixed\n- old, see `[Unreleased]`\n"
    )

    def test_unreleased_becomes_the_version(self):
        out = _load_release()._date_unreleased(
            self.CHANGELOG, "0.5.0", "2026-08-01")
        assert "## [Unreleased]" not in out
        assert out.index("## [0.5.0] - 2026-08-01") < out.index("- new")
        assert out.index("- new") < out.index("## [0.4.0]")

    def test_older_sections_stay_as_they_are(self):
        out = _load_release()._date_unreleased(
            self.CHANGELOG, "0.5.0", "2026-08-01")
        assert "- old, see `[Unreleased]`" in out

    def test_no_unreleased_section_stops_the_release(self):
        content = "# Changelog\n\n## [0.4.0] - 2026-07-15\n\n- old\n"
        with pytest.raises(SystemExit):
            _load_release()._date_unreleased(content, "0.5.0", "2026-08-01")

    def test_an_empty_section_stops_the_release(self):
        content = "# Changelog\n\n## [Unreleased]\n\n## [0.4.0] - 2026-07-15\n"
        with pytest.raises(SystemExit):
            _load_release()._date_unreleased(content, "0.5.0", "2026-08-01")

    def test_a_section_with_only_headings_stops_the_release(self):
        """A section that only announces categories says nothing; date it
        and the release notes vanish."""
        content = ("# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n"
                   "## [0.4.0] - 2026-07-15\n")
        mod = _load_release()
        assert mod._unreleased_problem(content) is not None
        with pytest.raises(SystemExit):
            mod._date_unreleased(content, "0.5.0", "2026-08-01")

    def test_an_entry_under_a_heading_passes_the_check(self):
        content = ("# Changelog\n\n## [Unreleased]\n\n### Fixed\n- new\n\n"
                   "## [0.4.0] - 2026-07-15\n")
        assert _load_release()._unreleased_problem(content) is None

    def test_an_empty_section_is_refused_before_anything_is_written(
            self, tmp_path, monkeypatch):
        """The version file used to be written first: the refusal then left
        lib/__init__.py dirty, with no release to show for it. dist/ counts
        as written too -- a release wipes it."""
        (tmp_path / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n## [0.4.0] - 2026-07-15\n",
            encoding="utf-8")
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "sentinel").write_text("keep", encoding="utf-8")
        mod = _load_release()
        calls = []
        monkeypatch.setattr(sys, "argv", ["release.py", "patch"])
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod.subprocess, "run", _clean_repo)
        monkeypatch.setattr(mod.release_gate, "head_commit", lambda: "abc")
        monkeypatch.setattr(mod.release_gate, "passed", lambda commit: True)
        monkeypatch.setattr(mod, "_current_version", lambda: "0.9.3")
        monkeypatch.setattr(mod, "_clean_dist", lambda: calls.append("dist"))
        monkeypatch.setattr(mod, "_update_version_file", calls.append)
        monkeypatch.setattr(mod, "_update_changelog", calls.append)
        # A terminal to answer from, and an origin that is level with main.
        monkeypatch.setattr(mod, "_stdin_is_a_terminal", lambda: True)
        monkeypatch.setattr(mod, "_require_publishable", lambda tag: None)
        with pytest.raises(SystemExit, match="empty"):
            mod.main()
        assert not calls
        assert (dist / "sentinel").exists()

    def test_dry_run_with_empty_notes_still_only_previews(
            self, tmp_path, monkeypatch, capsys):
        changelog = tmp_path / "CHANGELOG.md"
        content = "# Changelog\n\n## [Unreleased]\n\n## [0.4.0] - 2026-07-15\n"
        changelog.write_text(content, encoding="utf-8")
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "sentinel").write_text("keep", encoding="utf-8")
        mod = _load_release()
        asked = []
        monkeypatch.setattr(sys, "argv",
                            ["release.py", "patch", "--dry-run"])
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_current_version", lambda: "0.9.3")
        monkeypatch.setattr(mod.subprocess, "run", asked.append)
        assert mod.main() is None
        assert "[dry-run]" in capsys.readouterr().out
        assert asked == []  # not even asked what branch this is on
        assert changelog.read_text(encoding="utf-8") == content
        assert (dist / "sentinel").exists()

    def test_no_passing_gate_stops_the_release(self, monkeypatch):
        """Before the version moves or anything is committed."""
        mod = _load_release()
        calls = []
        monkeypatch.setattr(sys, "argv", ["release.py", "patch"])
        monkeypatch.setattr(mod.subprocess, "run", _clean_repo)
        monkeypatch.setattr(mod.release_gate, "head_commit", lambda: "abc")
        monkeypatch.setattr(mod.release_gate, "passed", lambda commit: False)
        monkeypatch.setattr(mod, "_current_version", lambda: "0.9.3")
        monkeypatch.setattr(mod, "_update_version_file", calls.append)
        monkeypatch.setattr(mod, "_stdin_is_a_terminal", lambda: True)
        monkeypatch.setattr(mod, "_require_publishable", lambda tag: None)
        with pytest.raises(SystemExit, match="No passing gate"):
            mod.main()
        assert not calls


def _clean_repo(command, **kwargs):
    """git on a clean tree, on main, where the new tag does not exist."""
    return SimpleNamespace(returncode=int("--verify" in command), stdout="main")


# What main() asks git, by name. A run's trace is compared to these.
BRANCH = "git rev-parse --abbrev-ref HEAD"
UNSTAGED = "git diff --quiet"
STAGED = "git diff --cached --quiet"
URLS = "git remote get-url --push --all origin"
FETCH = "git fetch --no-tags origin main"
HAS_MAIN = "git rev-parse -q --verify refs/remotes/origin/main"
COUNT = "git rev-list --left-right --count refs/remotes/origin/main...HEAD"
HAS_TAG = "git rev-parse -q --verify refs/tags/v0.9.4"
ASKS_TAG = "git ls-remote --exit-code --tags origin refs/tags/v0.9.4"
LISTED = ["git log --oneline -5 refs/remotes/origin/main..HEAD",
          "git log --oneline -5 HEAD..refs/remotes/origin/main"]
ADD = "git add src/arduscope/__init__.py CHANGELOG.md"
COMMIT = "git commit -m chore: release v0.9.4"
TAG = "git tag v0.9.4"
PUSH = "git push --atomic origin main v0.9.4"
TREE = [BRANCH, UNSTAGED, STAGED]
ORIGIN = [URLS, FETCH, HAS_MAIN, COUNT]
NEW_TAG = [HAS_TAG, ASKS_TAG]
PREFLIGHT = TREE + ORIGIN + NEW_TAG
CHECKED = PREFLIGHT + ["gate"]
# git on a clean tree, on main, level with origin/main, the tag new on both.
LEVEL = {URLS: "git@example.invalid:owner/repo.git", BRANCH: "main",
         COUNT: "0\t0", HAS_TAG: 1, ASKS_TAG: 2}


class _Git:
    """subprocess.run standing in for git.

    `answers` maps a fragment of a command to git's answer: a return code
    (and no output), or a line of output (and return code 0). A command
    nothing names succeeds silently, as `git add` does. Every command is
    appended to `trace` in order, and raises under check=True as
    subprocess.run would.
    """

    def __init__(self, trace, **answers):
        self.trace = trace
        self.answers = answers

    def __call__(self, command, **kwargs):
        self.trace.append(" ".join(command))
        text = " ".join(command)
        code, out = 0, ""
        for fragment, answer in self.answers.items():
            if fragment in text:
                code, out = ((answer, "") if isinstance(answer, int)
                             else (0, answer))
                break
        if kwargs.get("check") and code:
            raise subprocess.CalledProcessError(code, command)
        return SimpleNamespace(returncode=code, stdout=out,
                               stderr=f"git refused: {text}" if code else "")


class _Run:
    """One call of main(), in a directory that holds a version file (0.9.3)
    and a dated changelog, with git answered from a table.

    `trace` is every command git was asked, in order, with "gate" where the
    gate record was read. `untouched` says whether the version file, the
    changelog and dist/ are as they were.
    """

    def __init__(self, tmp_path, monkeypatch, gate=True, notes=True,
                 **answers):
        self.monkeypatch = monkeypatch
        self.version_file = tmp_path / "src" / "arduscope" / "__init__.py"
        self.version_file.parent.mkdir(parents=True)
        self.version_file.write_text('__version__ = "0.9.3"\n',
                                     encoding="utf-8")
        self.changelog = tmp_path / "CHANGELOG.md"
        body = "### Changed\n- new\n" if notes else ""
        self.changelog.write_text(
            f"# Changelog\n\n## [Unreleased]\n\n{body}\n"
            f"## [0.9.2] - 2026-07-01\n", encoding="utf-8")
        self.before = self._files()
        self.dist = tmp_path / "dist"
        self.dist.mkdir()
        (self.dist / "sentinel").write_text("keep", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        self.trace = []
        self.mod = _load_release()
        monkeypatch.setattr(self.mod.subprocess, "run",
                            _Git(self.trace, **{**LEVEL, **answers}))
        monkeypatch.setattr(self.mod.release_gate, "head_commit",
                            lambda: "abc")

        def passed(commit):
            self.trace.append("gate")
            return gate

        monkeypatch.setattr(self.mod.release_gate, "passed", passed)

    def _files(self):
        return (self.version_file.read_text(encoding="utf-8"),
                self.changelog.read_text(encoding="utf-8"))

    @property
    def untouched(self) -> bool:
        """Version file, changelog and dist/ as they were."""
        return (self._files() == self.before
                and (self.dist / "sentinel").exists())

    def __call__(self, *argv, terminal=True, prompt=None):
        """main() with these arguments, a patch release unless one is
        named. `prompt` answers the question about the push; without one,
        being asked is a failure of the test."""
        if not {"patch", "minor", "major"} & set(argv):
            argv = ("patch", *argv)
        monkeypatch = self.monkeypatch
        monkeypatch.setattr(self.mod, "_stdin_is_a_terminal",
                            lambda: terminal)
        monkeypatch.setattr(builtins, "input", prompt or (
            lambda question: pytest.fail(f"asked: {question}")))
        monkeypatch.setattr(sys, "argv", ["release.py", *argv])
        self.mod.main()


class TestMainAsksInOrder:
    """Nothing is written until every pre-flight check has passed, and they
    are asked in order: branch, terminal, tree, origin, gate, notes."""

    def test_a_passing_run_asks_everything_then_commits_tags_and_pushes(
            self, tmp_path, monkeypatch):
        run = _Run(tmp_path, monkeypatch)
        run("patch", "--yes", terminal=False)  # headless: --yes, no question
        assert run.trace == CHECKED + [ADD, COMMIT, TAG, PUSH]
        assert not run.untouched
        assert run.version_file.read_text(
            encoding="utf-8") == '__version__ = "0.9.4"\n'
        assert "## [0.9.4] - " in run.changelog.read_text(encoding="utf-8")
        assert not run.dist.exists()

    @pytest.mark.parametrize("answers, argv, terminal, message, asked", [
        # Not on main: a branch of its own, or a detached HEAD. Nothing
        # else is asked.
        ({BRANCH: "release/rules"}, (), True, "on 'release/rules', not 'main'",
         [BRANCH]),
        ({BRANCH: "HEAD"}, (), True, "on 'HEAD', not 'main'", [BRANCH]),
        # No terminal to answer from, and no --yes.
        ({}, (), False, "stdin is not a terminal", [BRANCH]),
        # A dirty tree, unstaged or staged.
        ({UNSTAGED: 1}, (), True, "is dirty", TREE),
        ({STAGED: 1}, (), True, "is dirty", TREE),
        # origin cannot be reached, or is more than one place to push to.
        ({URLS: "git@a/repo.git\ngit@b/repo.git"}, (), True,
         "origin has 2 push URLs", TREE + [URLS]),
        ({FETCH: 128}, (), True, "git fetch --no-tags origin main failed",
         TREE + [URLS, FETCH]),
        # No origin/main after the fetch.
        ({HAS_MAIN: 1}, (), True, "origin/main is not there after the fetch",
         TREE + [URLS, FETCH, HAS_MAIN]),
        # Not level with origin/main: ahead, behind, both.
        ({COUNT: "0\t1"}, (), True, "1 commit ahead of origin/main",
         TREE + ORIGIN + LISTED),
        ({COUNT: "2\t0"}, (), True, "2 commits behind origin/main",
         TREE + ORIGIN + LISTED),
        ({COUNT: "1\t1"}, (), True, "have diverged", TREE + ORIGIN + LISTED),
        # The tag is already spent: here only, on origin, or origin cannot
        # say.
        ({HAS_TAG: 0}, (), True, "exists here but not on origin", PREFLIGHT),
        ({ASKS_TAG: 0}, (), True,
         "already exists on origin. Tags are append-only", PREFLIGHT),
        ({ASKS_TAG: 128}, (), True, "Could not ask origin", PREFLIGHT),
    ])
    def test_a_refusal_writes_nothing_and_stops_at_its_check(
            self, tmp_path, monkeypatch, answers, argv, terminal, message,
            asked):
        run = _Run(tmp_path, monkeypatch, **answers)
        with pytest.raises(SystemExit, match=message):
            run(*argv, terminal=terminal)
        assert run.trace == asked
        assert run.untouched

    def test_the_gate_is_asked_after_origin(self, tmp_path, monkeypatch):
        run = _Run(tmp_path, monkeypatch, gate=False, notes=False)
        with pytest.raises(SystemExit, match="No passing gate"):
            run()
        assert run.trace == CHECKED
        assert run.untouched

    def test_the_notes_are_asked_after_the_gate(self, tmp_path, monkeypatch):
        run = _Run(tmp_path, monkeypatch, notes=False)
        with pytest.raises(SystemExit, match="empty"):
            run()
        assert run.trace == CHECKED
        assert run.untouched


class TestThePush:
    """The push is asked about once the commit and the tag exist."""

    TAGGED = CHECKED + [ADD, COMMIT, TAG]

    @pytest.mark.parametrize("closed", [EOFError, KeyboardInterrupt])
    def test_a_closed_prompt_prints_the_push_to_run_by_hand(
            self, tmp_path, monkeypatch, capsys, closed):
        """Ctrl-D and Ctrl-C used to end the run with a traceback, and
        nothing that said the commit and the tag were waiting."""
        def nobody(question):
            raise closed()

        run = _Run(tmp_path, monkeypatch)
        run(prompt=nobody)
        assert run.trace == self.TAGGED
        out = capsys.readouterr().out
        assert "Skipped push" in out and PUSH in out

    def test_no_skips_the_push(self, tmp_path, monkeypatch, capsys):
        run = _Run(tmp_path, monkeypatch)
        run(prompt=lambda question: "n")
        assert run.trace == self.TAGGED
        out = capsys.readouterr().out
        assert "Skipped push" in out and PUSH in out

    def test_yes_pushes(self, tmp_path, monkeypatch, capsys):
        run = _Run(tmp_path, monkeypatch)
        run(prompt=lambda question: "Y")
        assert run.trace == self.TAGGED + [PUSH]
        assert "Pushed" in capsys.readouterr().out

    def test_a_refused_push_fails_and_leaves_the_instruction(
            self, tmp_path, monkeypatch, capsys):
        run = _Run(tmp_path, monkeypatch, **{"git push": 1})
        with pytest.raises(SystemExit) as stopped:
            run("--yes")
        assert stopped.value.code == 1
        assert run.trace == self.TAGGED + [PUSH]
        out = capsys.readouterr().out
        assert "Push failed" in out and PUSH in out


@pytest.mark.parametrize("code, expected", [(0, True), (2, False)])
def test_whether_origin_has_the_tag_is_read_from_ls_remote(monkeypatch, code,
                                                           expected):
    """git ls-remote --exit-code answers 0 for a ref that is there and 2 for
    one that is not."""
    mod = _load_release()
    monkeypatch.setattr(mod.subprocess, "run", lambda command, **kwargs: (
        SimpleNamespace(returncode=code, stdout="", stderr="")))
    assert mod._remote_has("v0.0.2") is expected


def test_an_ls_remote_that_could_not_ask_is_not_an_answer(monkeypatch):
    mod = _load_release()
    monkeypatch.setattr(mod.subprocess, "run", lambda command, **kwargs: (
        SimpleNamespace(returncode=128, stdout="", stderr="fatal: no route")))
    with pytest.raises(mod.Refused, match="no route"):
        mod._remote_has("v0.0.2")


class TestAgainstOrigin:
    """The questions put to origin, asked of a real bare one: the answers
    are git's own, not a table's."""

    @pytest.fixture
    def release(self, tmp_path, monkeypatch):
        gitrepo.hermetic(monkeypatch)
        work, origin = gitrepo.repository(tmp_path)
        monkeypatch.chdir(work)
        return SimpleNamespace(work=work, origin=origin, mod=_load_release())

    def another_clone(self, release, name="other"):
        """A second clone of the same origin: what another machine holds."""
        other = release.work.parent / name
        gitrepo.git(release.work.parent, "clone", "-q", str(release.origin),
                    str(other))
        return other

    def test_main_level_with_origin_and_a_new_tag_pass(self, release, capsys):
        release.mod._require_publishable("v0.0.2")
        assert "v0.0.2 is new here and on origin" in capsys.readouterr().out

    def test_an_unpushed_commit_is_ahead_and_named(self, release):
        gitrepo.commit(release.work, "chore: local work")
        with pytest.raises(release.mod.Refused, match=(
                r"1 commit ahead of origin/main:\n[0-9a-f]+ chore: local work")):
            release.mod._require_publishable("v0.0.2")

    def test_a_commit_only_on_origin_is_behind_once_fetched(self, release):
        """origin/main in this clone is stale until the preflight fetches."""
        other = self.another_clone(release)
        gitrepo.commit(other, "chore: pushed from elsewhere")
        gitrepo.git(other, "push", "-q", "origin", "main")
        head = gitrepo.git(release.work, "rev-parse", "HEAD")
        assert gitrepo.git(release.work, "rev-parse", "origin/main") == head
        with pytest.raises(release.mod.Refused, match=(
                r"1 commit behind origin/main:\n"
                r"[0-9a-f]+ chore: pushed from elsewhere")):
            release.mod._require_publishable("v0.0.2")
        assert gitrepo.git(release.work, "rev-parse", "origin/main") != head

    def test_divergence_names_both_sides(self, release):
        gitrepo.commit(release.work, "chore: local work")
        other = self.another_clone(release)
        gitrepo.commit(other, "chore: pushed from elsewhere")
        gitrepo.git(other, "push", "-q", "origin", "main")
        with pytest.raises(release.mod.Refused, match=(
                r"have diverged: 1 commit here, 1 commit on origin:\n"
                r"[0-9a-f]+ chore: local work\n"
                r"[0-9a-f]+ chore: pushed from elsewhere")):
            release.mod._require_publishable("v0.0.2")

    def test_a_tag_made_here_is_local_only(self, release):
        gitrepo.git(release.work, "tag", "v0.0.2")
        with pytest.raises(release.mod.Refused,
                           match="exists here but not on origin") as refused:
            release.mod._require_publishable("v0.0.2")
        assert "git tag -d v0.0.2" in str(refused.value)
        assert "Bump differently" not in str(refused.value)

    def test_a_tag_on_both_sides_is_said_to_be_on_both(self, release):
        gitrepo.git(release.work, "tag", "v0.0.2")
        gitrepo.git(release.work, "push", "-q", "origin", "v0.0.2")
        with pytest.raises(release.mod.Refused, match=(
                r"already exists on origin and here\. "
                r"Tags are append-only")) as refused:
            release.mod._require_publishable("v0.0.2")
        assert "Bump differently" in str(refused.value)

    def test_a_tag_on_origin_alone_is_append_only(self, release):
        """Not here any more, as on a machine that never made it."""
        gitrepo.git(release.work, "tag", "v0.0.2")
        gitrepo.git(release.work, "push", "-q", "origin", "v0.0.2")
        gitrepo.git(release.work, "tag", "-d", "v0.0.2")
        with pytest.raises(release.mod.Refused, match=(
                r"already exists on origin\. Tags are append-only")):
            release.mod._require_publishable("v0.0.2")

    def test_the_push_alone_would_not_refuse_a_spent_tag(self, release):
        """Why the tag is asked about first. To git a tag that is already
        on origin, pointing where the local one does, is no change: an
        atomic push takes main and leaves the tag, so a release commit goes
        out under no new tag and starts no workflow."""
        gitrepo.git(release.work, "tag", "v0.0.2")
        gitrepo.git(release.work, "push", "-q", "origin", "v0.0.2")
        gitrepo.commit(release.work, "chore: another commit")
        gitrepo.git(release.work, "push", "--atomic", "origin", "main",
                    "v0.0.2")
        remote = gitrepo.git(release.origin, "rev-parse", "main")
        assert remote == gitrepo.git(release.work, "rev-parse", "HEAD")

    def test_an_origin_that_cannot_be_reached_is_an_error_not_an_absent_tag(
            self, release):
        nowhere = str(release.work.parent / "nowhere.git")
        gitrepo.git(release.work, "remote", "set-url", "origin", nowhere)
        with pytest.raises(release.mod.Refused,
                           match="git fetch --no-tags origin main failed"):
            release.mod._require_publishable("v0.0.2")
        with pytest.raises(release.mod.Refused, match="Could not ask origin"):
            release.mod._remote_has("v0.0.2")

    def test_no_origin_main_after_the_fetch(self, release):
        """A remote with no fetch refspec brings main to FETCH_HEAD only."""
        gitrepo.git(release.work, "config", "--unset-all",
                    "remote.origin.fetch")
        gitrepo.git(release.work, "update-ref", "-d",
                    "refs/remotes/origin/main")
        with pytest.raises(release.mod.Refused,
                           match="origin/main is not there after the fetch"):
            release.mod._require_publishable("v0.0.2")

    def test_a_local_tag_named_origin_main_cannot_make_an_ahead_main_look_level(
            self, release):
        """git takes refs/tags/<name> before refs/remotes/<name>. A tag of
        that name, on the unpushed commit, is what the short name reads as:
        level, with nothing to push."""
        gitrepo.commit(release.work, "chore: local work")
        gitrepo.git(release.work, "tag", "origin/main")
        with pytest.raises(release.mod.Refused, match=(
                r"1 commit ahead of origin/main:\n[0-9a-f]+ chore: local work")):
            release.mod._require_publishable("v0.0.2")

    def test_an_origin_with_two_push_urls_is_refused_before_it_is_asked(
            self, release):
        """An atomic push is atomic for each URL on its own: main and the tag
        could land on one and not on the other."""
        for name in ("second", "third"):
            gitrepo.git(release.work, "remote", "set-url", "--add", "--push",
                        "origin", str(release.work.parent / f"{name}.git"))
        with pytest.raises(release.mod.Refused,
                           match="origin has 2 push URLs") as refused:
            release.mod._require_publishable("v0.0.2")
        assert "second.git" in str(refused.value)
        assert "third.git" in str(refused.value)

    def test_an_origin_with_one_push_url_of_its_own_passes(self, release):
        """A push URL of its own is not a second place: with one set, the
        fetch URL stops counting as somewhere to push to."""
        gitrepo.git(release.work, "remote", "set-url", "--push", "origin",
                    str(release.origin))
        release.mod._require_publishable("v0.0.2")


class TestAReleaseReachesOrigin:
    """main() end to end on a real repository, against a bare origin."""

    @pytest.fixture
    def release(self, tmp_path, monkeypatch):
        gitrepo.hermetic(monkeypatch)
        work, origin = gitrepo.repository(tmp_path, version="0.0.1")
        (work / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n### Changed\n- new\n",
            encoding="utf-8")
        monkeypatch.chdir(work)
        mod = _load_release()
        monkeypatch.setattr(mod, "_stdin_is_a_terminal", lambda: False)
        monkeypatch.setattr(mod.release_gate, "head_commit",
                            lambda: gitrepo.git(work, "rev-parse", "HEAD"))
        monkeypatch.setattr(mod.release_gate, "passed", lambda commit: True)
        monkeypatch.setattr(sys, "argv", ["release.py", "patch", "--yes"])
        return SimpleNamespace(work=work, origin=origin, mod=mod)

    @staticmethod
    def _remote(origin) -> dict:
        return {ref: sha for sha, ref in (line.split() for line in
                                          gitrepo.git(origin,
                                                      "show-ref").splitlines())}

    def test_main_and_the_tag_arrive_together(self, release, capsys):
        release.mod.main()
        head = gitrepo.git(release.work, "rev-parse", "HEAD")
        assert self._remote(release.origin) == {"refs/heads/main": head,
                                                "refs/tags/v0.0.2": head}
        assert gitrepo.git(release.work, "log", "-1", "--format=%s") == \
            "chore: release v0.0.2"
        assert gitrepo.git(release.work, "status", "--porcelain") == ""
        assert PUSH.replace("0.9.4", "0.0.2") in capsys.readouterr().out

    def test_a_declined_push_leaves_a_state_the_next_run_names(
            self, release, monkeypatch, capsys):
        """The commit and the tag stay local, and the run after that
        refuses. It used to bump once more, on top of an unreleased version.
        """
        monkeypatch.setattr(release.mod, "_stdin_is_a_terminal", lambda: True)
        monkeypatch.setattr(builtins, "input", lambda question: "n")
        monkeypatch.setattr(sys, "argv", ["release.py", "patch"])
        release.mod.main()
        first = gitrepo.git(release.work, "rev-parse", "HEAD~1")
        assert self._remote(release.origin) == {"refs/heads/main": first}
        assert gitrepo.git(release.work, "rev-parse", "v0.0.2") == \
            gitrepo.git(release.work, "rev-parse", "HEAD")
        assert "git push --atomic origin main v0.0.2" in \
            capsys.readouterr().out

        with pytest.raises(SystemExit,
                           match="1 commit ahead of origin/main"):
            release.mod.main()
        assert gitrepo.git(release.work, "log", "-1", "--format=%s") == \
            "chore: release v0.0.2"


def _load_build():
    spec = importlib.util.spec_from_file_location(
        "bwp_mod", REPO / "build_windows_package.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestWindowsBuildTooling:
    """.pyc bytecode is minor-version-locked; compiling with an interpreter
    that does not match the bundled embeddable ships a package that dies
    with 'bad magic number' on Windows. _get_compiler must fail loudly
    rather than fall back to a mismatched interpreter."""

    def test_get_compiler_rejects_missing_version(self):
        mod = _load_build()
        # No interpreter provides Python 3.99 -> must raise, not silently
        # return the host interpreter and ship a bad-magic-number .pyc.
        with pytest.raises(RuntimeError, match="magic number"):
            mod._get_compiler("3.99.0", "/tmp")

    def test_get_compiler_accepts_host_version(self):
        import sys
        mod = _load_build()
        host_full = "%d.%d.0" % sys.version_info[:2]
        # The host's own minor version always has a matching interpreter.
        assert mod._get_compiler(host_full, "/tmp")


REQUIREMENTS = REPO / "win64-requirements.txt"


class TestWindowsPackagePins:
    """Up to 0.9.9 the package took whatever pip found that day, with
    --no-deps, on an embeddable whose download nothing checked. 0.9.10
    pins the Python by its SHA-256 and each library by its wheel's."""

    def test_a_download_that_is_not_the_listed_file_stops_the_build(self):
        mod = _load_build()
        with pytest.raises(RuntimeError, match="python.org lists"):
            mod.check_embed(b"not the embeddable", mod.DEFAULT_PYTHON)

    def test_a_python_with_no_hash_on_record_stops_the_build(self):
        with pytest.raises(RuntimeError, match="No SHA-256 on record"):
            _load_build().check_embed(b"", "3.99.0")

    def test_the_listed_file_passes(self, monkeypatch):
        mod = _load_build()
        monkeypatch.setitem(mod.EMBED_SHA256, "9.9.9",
                            hashlib.sha256(b"zip").hexdigest())
        mod.check_embed(b"zip", "9.9.9")

    def test_the_default_python_has_a_hash(self):
        mod = _load_build()
        assert re.fullmatch(r"[0-9a-f]{64}", mod.EMBED_SHA256[mod.DEFAULT_PYTHON])

    def test_every_pin_carries_a_hash(self):
        text = REQUIREMENTS.read_text().replace("\\\n", " ")
        entries = [line for line in text.splitlines()
                   if line.strip() and not line.startswith("#")]
        pins = _load_build().read_requirements(str(REQUIREMENTS))
        assert len(entries) == len(pins) >= 6
        for entry in entries:
            assert re.match(r"[A-Za-z0-9._-]+==[^\s]+ ", entry), entry
            assert re.search(r"--hash=sha256:[0-9a-f]{64}\b", entry), entry

    def test_the_pins_meet_pyproject(self):
        tomllib = pytest.importorskip("tomllib")
        mod = _load_build()
        with open(REPO / "pyproject.toml", "rb") as f:
            pyproject = tomllib.load(f)
        pins = mod.read_requirements(str(REQUIREMENTS))
        assert mod.check_requirements(pins, pyproject) == []

    def test_a_pin_that_misses_pyproject_is_reported(self):
        pyproject = {"project": {
            "dependencies": ["numpy>=1.24,<3", "plotly>=6.0.0"],
            "optional-dependencies": {"web": ["bottle>=0.13"]}}}
        problems = _load_build().check_requirements(
            {"numpy": "3.0.0", "plotly": "6.1"}, pyproject)
        assert problems == ["numpy==3.0.0 does not satisfy numpy>=1.24,<3",
                            "bottle is not pinned"]

    @staticmethod
    def _site(tmp_path, libraries):
        for folder, requires in libraries.items():
            info = tmp_path / f"{folder}.dist-info"
            info.mkdir()
            (info / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: x\n"
                + "".join(f"Requires-Dist: {r}\n" for r in requires))
        return str(tmp_path)

    def test_a_dependency_left_out_stops_the_build(self, tmp_path):
        site = self._site(tmp_path, {
            "plotly-7.1.0": ["narwhals>=1.15.1", "packaging",
                             'numpy>=1.22; extra == "express"'],
            "packaging-26.3": [],
            # Both left out on purpose, see _NOT_BUNDLED.
            "pymavlink-2.4.50": ["lxml", "fastcrc"],
            "Some_Lib-1.0": ['colorama; sys_platform == "win32"',
                             'uvloop; sys_platform == "linux"',
                             'tomli; python_version < "3.11"'],
        })
        assert _load_build().check_closure(site, "3.14.10") == [
            "plotly needs narwhals, which is not pinned",
            "some-lib needs colorama, which is not pinned",
        ]


class TestReleaseWorkflow:
    WORKFLOW = (REPO / ".github" / "workflows" / "release.yml").read_text()

    def test_the_release_is_a_draft(self):
        step = self.WORKFLOW.split("softprops/action-gh-release", 1)[1]
        assert re.search(r"^\s+draft: true$", step, re.M)

    def test_the_runner_python_is_the_bundled_minor(self):
        """.pyc compiled by another minor version does not load."""
        build_job = self.WORKFLOW.split("\n  build:", 1)[1]
        runner = re.search(r'python-version: "([\d.]+)"', build_job).group(1)
        default = _load_build().DEFAULT_PYTHON
        assert runner == ".".join(default.split(".")[:2])

    def test_the_version_is_named_in_one_place(self):
        assert "--python-version" not in self.WORKFLOW

    def test_only_the_build_job_can_write(self):
        """A job without a permissions block of its own takes the
        workflow's, or else the repository default: CodeQL flagged ci."""
        head, jobs = self.WORKFLOW.split("\njobs:", 1)
        assert re.search(r"^permissions:\n  contents: read$", head, re.M)
        ci_job, build_job = jobs.split("\n  build:", 1)
        assert "write" not in ci_job
        assert re.search(r"^      contents: write$", build_job, re.M)

    def test_actions_from_outside_github_are_named_by_commit(self):
        """A tag can be moved to other code; a commit cannot."""
        workflows = (REPO / ".github" / "workflows").glob("*.yml")
        uses = [m.group(1) for path in workflows
                for m in re.finditer(r"uses:\s*(\S+)", path.read_text())]
        outside = [u for u in uses
                   if not u.startswith(("./", "actions/", "github/"))]
        assert outside, "the release action is expected here"
        for action in outside:
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", action), action


def _load_check():
    spec = importlib.util.spec_from_file_location(
        "wpc_mod", REPO / "tools" / "win_package_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPackageCheck:
    SITE = "arduscope/python/Lib/site-packages/"

    def test_libraries_come_from_their_dist_info(self):
        names = [self.SITE + "numpy-2.5.4.dist-info/METADATA",
                 self.SITE + "numpy/__init__.py",
                 self.SITE + "pip-26.0.dist-info/METADATA",
                 self.SITE + "PyYAML-6.0.dist-info/RECORD"]
        assert _load_check().bundled_libraries(names) == {"numpy": "2.5.4"}

    def test_a_library_off_its_pin_is_named(self):
        differ = _load_check().pin_differences(
            {"numpy": "2.5.4", "plotly": "7.0.0"},
            {"numpy": "2.5.4", "plotly": "7.1.0", "bottle": "0.13.4"})
        assert differ == ["bottle: missing, pinned 0.13.4",
                          "plotly: 7.0.0, pinned 7.1.0"]


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "gate_mod", REPO / "tools" / "release_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestGateEnvironment:
    """The gate ran on whatever the local environment held, a numpy and a
    plotly older than the ones the Windows package ships, and its record did
    not say so."""

    PINS = _load_build().read_requirements(str(REQUIREMENTS))

    def test_the_record_lists_every_pinned_library(self):
        env = _load_gate().environment(version=lambda name: self.PINS[name])
        assert env["libraries"] == self.PINS
        assert env["off_the_windows_pins"] == []
        assert re.fullmatch(r"\d+\.\d+\.\d+\S*", env["python"])

    def test_a_library_off_its_pin_is_named(self):
        older = {**self.PINS, "numpy": "2.4.6"}
        env = _load_gate().environment(version=lambda name: older[name])
        assert env["off_the_windows_pins"] == ["numpy"]

    def test_a_missing_library_is_named(self):
        from importlib import metadata

        def version(name):
            if name == "bottle":
                raise metadata.PackageNotFoundError(name)
            return self.PINS[name]

        env = _load_gate().environment(version=version)
        assert env["libraries"]["bottle"] is None
        assert env["off_the_windows_pins"] == ["bottle"]
