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
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(TOOLS_DIR))

import check_release_tag as crt  # noqa: E402
import version_source  # noqa: E402

pytestmark = pytest.mark.fast


@pytest.mark.parametrize("tag, version", [
    ("v0.14.4", "0.14.4"), ("v1.0.0", "1.0.0"), ("v0.0.1", "0.0.1"),
    ("v10.100.1000", "10.100.1000"),
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
    # Another way of writing the same number. setuptools normalises it on
    # the wheel and the sdist; the tag, which names the Windows package,
    # would keep the zero.
    "v01.14.0", "v0.014.4", "v0.14.04", "v00.0.0",
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


def _commit_file(work, text: str, tag: str) -> None:
    """A commit holding this version file, tagged and pushed, so that only
    what the file says is at issue."""
    (work / gitrepo.VERSION_FILE).write_text(text, encoding="utf-8")
    gitrepo.git(work, "add", str(gitrepo.VERSION_FILE))
    gitrepo.git(work, "commit", "-q", "-m", "version file")
    gitrepo.git(work, "tag", tag)
    gitrepo.git(work, "push", "-q", "origin", "main")


class TestReadingTheVersion:
    """__version__ comes from the tagged commit, and is read as code."""

    def test_the_working_tree_is_not_consulted(self, work):
        """The workflow happens to have the tagged commit checked out. Run
        from any other tree, the file on disk can say anything."""
        gitrepo.git(work, "tag", "v0.0.2")  # on a commit that says 0.0.1
        gitrepo.write_version(work, "0.0.2")  # uncommitted
        with pytest.raises(crt.TagError, match=(
                r"tag v0\.0\.2 releases 0\.0\.2, but "
                r"src/arduscope/__init__\.py in the tagged commit says "
                r"0\.0\.1")):
            crt.check("v0.0.2")

    def test_a_comment_that_spells_an_assignment_is_not_one(self, work):
        _commit_file(work, '# __version__ = "0.0.0"\n'
                           '__version__ = "0.0.2"\n', "v0.0.2")
        assert crt.check("v0.0.2") == "0.0.2"

    @pytest.mark.parametrize("text", [
        '__version__ = "0.0.2"\n__version__ = "0.0.3"\n',
        # Rebound under an if, and by an augmented assignment: the module
        # reports the later value, setuptools the first one it sees.
        '__version__ = "0.0.2"\nif True:\n    __version__ = "0.0.3"\n',
        '__version__ = "0.0.2"\n__version__ += "rc1"\n',
    ])
    def test_a_second_binding_of_the_name_is_refused(self, work, text):
        _commit_file(work, text, "v0.0.2")
        with pytest.raises(crt.TagError, match="binds __version__ 2 times"):
            crt.check("v0.0.2")

    @pytest.mark.parametrize("text", [
        "# __version__ = \"0.0.2\"\n",
        "from somewhere import __version__\n",
        "__version__, other = \"0.0.2\", 2\n",
    ])
    def test_a_file_with_no_plain_assignment_is_refused(self, work, text):
        _commit_file(work, text, "v0.0.2")
        with pytest.raises(crt.TagError, match="__version__"):
            crt.check("v0.0.2")

    def test_a_star_import_is_refused_before_the_tag_is_trusted(
            self, work):
        """What a star import binds is decided at run time: the reader
        cannot see whether __version__ is among it."""
        _commit_file(work, '__version__ = "0.0.2"\nfrom foo import *\n',
                     "v0.0.2")
        with pytest.raises(crt.TagError, match=r"imports \* at line 2"):
            crt.check("v0.0.2")

    def test_a_leading_zero_is_refused_whatever_the_file_says(self, work):
        """Name and file agree, and both are refused: 01.0.0 is 1.0.0 to
        setuptools, so the wheel would not carry the tag's version."""
        _commit_file(work, '__version__ = "01.0.0"\n', "v01.0.0")
        with pytest.raises(crt.TagError, match="leading zero"):
            crt.check("v01.0.0")

    def test_a_tag_whose_commit_has_no_version_file_is_refused(self, work):
        gitrepo.git(work, "rm", "-q", str(gitrepo.VERSION_FILE))
        gitrepo.git(work, "commit", "-q", "-m", "no version file")
        gitrepo.git(work, "tag", "v0.0.2")
        with pytest.raises(crt.TagError, match=(
                r"could not read src/arduscope/__init__\.py from tag v0\.0\.2")):
            crt.check("v0.0.2")


@pytest.mark.parametrize("source, expected", [
    (b'__version__ = "0.14.4"\n', "0.14.4"),
    ('# __version__ = "0.0.0"\n__version__ = "0.14.4"\n', "0.14.4"),
    ('"""A docstring in which __version__ = "0.0.0" is mentioned."""\n'
     '__version__ = "0.14.4"\n', "0.14.4"),
    ("# a comment that reads, not assigns: print(__version__)\n"
     '__version__ = "0.14.4"\n', "0.14.4"),
    ('__version__: str = "0.14.4"\n', "0.14.4"),
    ('__version__ = "0.14" ".4"\n', "0.14.4"),  # one string to Python
    ('__version__ = "0.14.4"  # a comment after it\n', "0.14.4"),
    ('__version__ = "0.14.4"\nprint(__version__)\n', "0.14.4"),  # a use
    # Imports that bind nothing this file names.
    ('__version__ = "0.14.4"\nimport foo\n', "0.14.4"),
    ('import foo\nfrom foo import bar\n__version__ = "0.14.4"\n', "0.14.4"),
    ('__version__ = "0.14.4"\nimport __version__ as v\n', "0.14.4"),
    ('__version__ = "0.14.4"\nfrom foo import __version__ as v\n',
     "0.14.4"),
])
def test_the_one_assignment_is_read_as_code(source, expected):
    assert version_source.source_version(source, "x.py") == expected


@pytest.mark.parametrize("source, message", [
    ("", "does not bind __version__"),
    ('# __version__ = "0.14.4"\n', "does not bind __version__"),
    ('print(__version__)\n', "does not bind __version__"),
    # Bound once, but not by a plain assignment of the one name at module
    # level.
    ('if True:\n    __version__ = "0.14.4"\n', "not by one plain assignment"),
    ('try:\n    __version__ = "0.14.4"\nexcept OSError:\n    pass\n',
     "not by one plain assignment"),
    ("__version__, other = \"0.14.4\", 2\n", "not by one plain assignment"),
    ("other, __version__ = 1, \"0.14.4\"\n", "not by one plain assignment"),
    ("__version__ = other = '0.14.4'\n", "not by one plain assignment"),
    ('with open("x") as __version__:\n    pass\n',
     "not by one plain assignment"),
    ('if (__version__ := "0.14.4"):\n    pass\n',
     "not by one plain assignment"),
    ("del __version__\n", "by deletion"),
    ("from somewhere import __version__\n", "by import"),
    ("from somewhere import thing as __version__\n", "by import"),
    ("import somewhere as __version__\n", "by import"),
    # An import that names no other name binds the one it names.
    ("import __version__\n", "by import"),
    ("import __version__.sub\n", "by import"),
    ('__version__ = "0.14.4"\nimport __version__\n', "2 times"),
    # A star import can bind anything, and does not say what.
    ('__version__ = "0.14.4"\nfrom foo import *\n',
     r"imports \* at line 2"),
    ("from foo import *\n", r"imports \* at line 1"),
    ("from . import *\n", r"imports \* at line 1"),
    ("try:\n    from foo import *\nexcept ImportError:\n    pass\n",
     r"imports \* at line 2"),
    ("def f(__version__):\n    return __version__\n", "as a parameter"),
    ("f = lambda __version__: __version__\n", "as a parameter"),
    ("try:\n    pass\nexcept NameError as __version__:\n    pass\n",
     "as an exception handler's name"),
    ("class __version__:\n    pass\n", "as a definition"),
    ("def __version__():\n    pass\n", "as a definition"),
    ("match x:\n    case __version__:\n        pass\n",
     "as a match pattern's capture"),
    # Bound more than once, in this module or in any scope inside it.
    ('__version__ = "0.14.4"\nif True:\n    __version__ = "0.14.5"\n',
     "binds __version__ 2 times \\(lines 1, 3\\)"),
    ('__version__ = "0.14.4"\n__version__ += "rc1"\n',
     "binds __version__ 2 times \\(lines 1, 2\\)"),
    ('__version__ = "0.14.4"\nfor __version__ in "12":\n    pass\n',
     "2 times"),
    ('__version__ = "0.14.4"\nother, __version__ = 1, "0.14.5"\n', "2 times"),
    # A plain assignment of something that is not a string.
    ("__version__ = 1\n", "is not a plain string"),
    ('__version__ = f"0.14.4"\n', "is not a plain string"),
    ('__version__ = "0.14" + ".4"\n', "is not a plain string"),
    ("__version__ = None\n", "is not a plain string"),
    ("__version__: str\n", "is not a plain string"),
    ("def (:\n", "does not parse"),
])
def test_anything_else_is_refused_rather_than_guessed(source, message):
    with pytest.raises(version_source.VersionSourceError, match=message):
        version_source.source_version(source, "x.py")


def test_the_tag_check_reads_through_the_shared_module():
    """The same reader as tools/release.py rewrites with."""
    assert crt.version_source is version_source


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
                r"tag v0\.0\.3 releases 0\.0\.3, but "
                r"src/arduscope/__init__\.py in the tagged commit says "
                r"0\.0\.2")):
            crt.check("v0.0.3")

    def test_a_tag_on_an_unpushed_commit_fails(self, work):
        """The workflow's own rule, as release.py's is for the run that made
        the tag: a tag pushed from a clone that never pushed main."""
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
        with pytest.raises(crt.TagError, match="could not read "
                                               r"src/arduscope/__init__\.py "
                                               r"from tag v0\.0\.2"):
            crt.check("v0.0.2")

    def test_no_origin_main_here_is_an_error_not_a_verdict(self, work):
        """A checkout that never fetched main cannot say the tag is off it."""
        tag = _release(work, "0.0.2")
        gitrepo.git(work, "update-ref", "-d", "refs/remotes/origin/main")
        with pytest.raises(crt.TagError, match="could not place"):
            crt.check(tag)

    def test_a_malformed_tag_is_refused_before_git_is_asked(self, work):
        with pytest.raises(crt.TagError, match="not vX.Y.Z"):
            crt.check("v0.14")


class TestMainByItsFullName:
    """git takes refs/tags/<name> before refs/remotes/<name>. Asked for
    "origin/main", a tag of that name answers for the branch."""

    def test_main_is_a_remote_tracking_ref_by_its_full_name(self):
        assert crt.MAIN_REF == "refs/remotes/origin/main"

    def test_a_local_tag_named_origin_main_cannot_vouch_for_an_off_main_tag(
            self, work):
        tag = _release(work, "0.0.2", push=False)
        gitrepo.git(work, "tag", "origin/main", tag)
        with pytest.raises(crt.TagError, match="not on origin/main"):
            crt.check(tag)

    def test_the_ancestry_question_names_the_full_ref(self, work, monkeypatch):
        tag = _release(work, "0.0.2")
        asked = []

        def git(command, **kwargs):
            asked.append(command)
            if command[1] == "show":
                return SimpleNamespace(
                    returncode=0,
                    stdout=(work / gitrepo.VERSION_FILE).read_bytes(),
                    stderr=b"")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(crt.subprocess, "run", git)
        assert crt.check(tag) == "0.0.2"
        assert asked == [
            ["git", "show",
             "refs/tags/v0.0.2^{commit}:src/arduscope/__init__.py"],
            ["git", "merge-base", "--is-ancestor",
             "refs/tags/v0.0.2^{commit}", "refs/remotes/origin/main"],
        ]

    def test_a_main_that_is_not_a_full_ref_name_is_refused(self, work):
        tag = _release(work, "0.0.2")
        with pytest.raises(crt.TagError, match="not a full ref name"):
            crt.check(tag, "origin/main")
        assert crt.main(["--tag", tag, "--main-ref", "origin/main"]) == 1


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

    @pytest.mark.parametrize("tag", ["v0.14", "v01.0.2", "0.0.2", "v0.0.3"])
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


class TestTheWorkflow:
    """release.yml reads: the check has its own job, ahead of everything
    that builds, and cannot be dropped from it or moved behind the build
    unnoticed."""

    WORKFLOW = (TOOLS_DIR.parent / ".github" / "workflows" / "release.yml"
                ).read_text()

    @classmethod
    def job(cls, name: str, until: str) -> str:
        return cls.WORKFLOW.split(f"\n  {name}:", 1)[1].split(f"\n  {until}:",
                                                              1)[0]

    def test_the_tag_is_checked_in_the_first_job(self):
        """Before the reusable CI, which builds a wheel, and before build,
        which makes the zip and the draft."""
        workflow = self.WORKFLOW
        checked = workflow.index("tools/check_release_tag.py")
        jobs = workflow.index("\njobs:")
        validate = workflow.index("\n  validate:")
        ci = workflow.index("\n  ci:")
        build = workflow.index("\n  build:")
        assert jobs < validate < checked < ci < build
        for built in ("python -m build", "build_windows_package.py",
                      "softprops/action-gh-release"):
            assert build < workflow.index(built), built

    def test_nothing_in_the_validating_job_builds(self):
        """Not even the reusable CI, which builds a wheel of its own."""
        validate = self.job("validate", "ci")
        for built in ("pip install", "python -m build",
                      "build_windows_package.py", "setup-python",
                      "uses: ./.github/workflows"):
            assert built not in validate, built
        assert "permissions" not in validate  # the workflow's read is enough

    def test_the_check_sees_main(self):
        """It places the tagged commit against origin/main, which takes the
        whole history and a fetch of main, before it runs."""
        validate = self.job("validate", "ci")
        checkout, _ = validate.split("tools/check_release_tag.py", 1)
        assert re.search(r"fetch-depth: 0$", checkout, re.M)
        assert "git fetch origin main" in checkout

    def test_the_version_leaves_the_validating_job_as_its_output(self):
        validate = self.job("validate", "ci")
        assert re.search(
            r"^    outputs:\n      version: \$\{\{ steps\.tag\.outputs\.version \}\}$",
            validate, re.M)
        assert re.search(r"^        id: tag\n        run: python3 "
                         r"tools/check_release_tag.py$", validate, re.M)

    def test_both_later_jobs_need_it(self):
        assert re.search(r"^    needs: validate$", self.job("ci", "build"),
                         re.M)
        assert re.search(r"^    needs: \[validate, ci\]$",
                         self.job("build", "\0"), re.M)

    def test_build_reads_the_version_it_confirmed(self):
        """The zip and the notes take the version from the validating job:
        a step of build's own no longer strips it off the tag."""
        build = self.job("build", "\0")
        assert build.count("${{ needs.validate.outputs.version }}") == 2
        assert "GITHUB_REF#" not in self.WORKFLOW
        assert "steps.version.outputs" not in self.WORKFLOW


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
        gitrepo.git(work, "tag", "v0.0.3")  # on the commit that says 0.0.2
        done = self._run(work, "--tag", "v0.0.3")
        assert done.returncode == 1
        assert done.stdout == "" and not (work / "output").exists()
        assert ("FAIL tag v0.0.3 releases 0.0.3, but "
                "src/arduscope/__init__.py in the tagged commit says 0.0.2"
                in done.stderr)
