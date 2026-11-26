#!/usr/bin/env python3
"""
Release helper — bump version, date the CHANGELOG, tag, and push.

Run tools/release_gate.py on the commit first: this will not tag a commit
without a pass from it. Release notes go under `## [Unreleased]` as changes
land, and become the new version's section here.

Nothing is written until every pre-flight check has passed, in this order:
on `main`; able to hear the answer about the push; a clean tree and index;
fetched, with `main` level with `origin/main` and the new tag absent here and
on origin; a passing gate; notes to date; a version file that can carry the
new version. `main` and the tag then leave in one atomic push, so a push that
is refused leaves both local.

The version is read, and rewritten, through tools/version_source.py: the one
assignment to __version__ in the file, as code. A comment that spells an
assignment is neither read nor touched, and neither is any other byte of the
file: it is read and written as bytes, so its line endings stay as they are.

Usage:
    python tools/release.py patch          # 0.3.1 -> 0.3.2
    python tools/release.py minor          # 0.3.1 -> 0.4.0
    python tools/release.py major          # 0.3.1 -> 1.0.0
    python tools/release.py --dry-run patch  # preview without changes
    python tools/release.py --yes patch    # push without asking
"""

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import release_gate  # noqa: E402
import version_source  # noqa: E402

# Commits named when main and origin/main are not level; the rest are counted.
MAX_LISTED = 5
# origin/main by its full name. "origin/main" on its own is also a tag
# someone could have made, and git takes a tag before a remote-tracking
# branch: a tag named that way, on a commit main has not pushed, reads as
# level with origin. Messages still say origin/main.
MAIN = "refs/remotes/origin/main"


class Refused(Exception):
    """A pre-flight refusal. Nothing has been written."""


# The one shape release.py can bump, written as tools/check_release_tag.py
# writes the tag: three numbers, none with a leading zero. The version file
# is read as code and may hold any string; a pre-release or build suffix has
# no numeric bump rule, and int() on it would end in a traceback after the
# checks.
_NUMBER = r"(?:0|[1-9][0-9]*)"
_CANONICAL_VERSION = re.compile(rf"^{_NUMBER}\.{_NUMBER}\.{_NUMBER}$")


def _version_bytes() -> bytes:
    """The version file as it is on the disk. Bytes, not text: in text mode
    a read folds CRLF and CR into LF, and a write on Windows unfolds them
    again, so the file would change outside the one literal."""
    with open(version_source.VERSION_FILE, "rb") as f:
        return f.read()


def _current_version() -> str:
    """What the one assignment to __version__ in the version file says."""
    try:
        return version_source.source_version(_version_bytes(),
                                             version_source.VERSION_FILE)
    except version_source.VersionSourceError as e:
        sys.exit(f"Cannot read the version from {version_source.VERSION_FILE}:"
                 f" {e}")
    except OSError as e:
        sys.exit(f"Cannot read {version_source.VERSION_FILE}: {e}")


def _bump(version: str, part: str) -> str:
    """The next version, or Refused when the current one is not X.Y.Z."""
    if not _CANONICAL_VERSION.fullmatch(version):
        raise Refused(
            f"{version_source.VERSION_FILE} holds {version!r}, which is not "
            f"X.Y.Z; release.py has no rule to bump it")
    major, minor, patch = (int(x) for x in version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _bumped_bytes(new_ver: str) -> bytes:
    """The version file with __version__ set to new_ver, and no other byte
    of it changed.

    A pre-flight check: when the file does not hold the one assignment to
    rewrite, the release is refused before anything is written.
    """
    try:
        return version_source.replaced(_version_bytes(), new_ver,
                                       version_source.VERSION_FILE)
    except version_source.VersionSourceError as e:
        raise Refused(f"{version_source.VERSION_FILE} cannot carry "
                      f"{new_ver}: {e}") from None
    except OSError as e:
        raise Refused(f"Cannot read {version_source.VERSION_FILE}: {e}") from None


def _update_version_file(source: bytes) -> None:
    with open(version_source.VERSION_FILE, "wb") as f:
        f.write(source)


def _unreleased_body(content: str) -> str:
    """The text under `## [Unreleased]`, or "" when the section is absent."""
    heading = "## [Unreleased]"
    start = content.find(heading)
    if start == -1:
        return ""
    end = content.find("\n## [", start + len(heading))
    return content[start + len(heading):end if end != -1 else len(content)]


def _unreleased_problem(content: str) -> str | None:
    """Why the release notes cannot be dated, or None when they can.

    Empty means no entry at all: a section holding only headings
    ("### Fixed") says nothing, so it counts as empty.
    """
    if "## [Unreleased]" not in content:
        return "CHANGELOG.md has no [Unreleased] section to release."
    body = _unreleased_body(content)
    if not any(line.strip() and not line.lstrip().startswith("#")
               for line in body.splitlines()):
        return "The [Unreleased] section of CHANGELOG.md is empty."
    return None


def _date_unreleased(content: str, new_ver: str, today: str) -> str:
    """Turn the `## [Unreleased]` section into this version's section.

    Notes come from whoever made the change. Generated from commit subjects
    they were not fit to publish, which is why v0.9.0 to v0.9.3 went out by
    hand. An empty section means nobody wrote any, so stop -- this check is
    the backstop; main() refuses before it writes anything.
    """
    problem = _unreleased_problem(content)
    if problem:
        sys.exit(problem)
    return content.replace("## [Unreleased]", f"## [{new_ver}] - {today}", 1)


def _update_changelog(new_ver: str) -> None:
    path = "CHANGELOG.md"
    with open(path, encoding="utf-8") as f:
        content = f.read()
    today = datetime.date.today().isoformat()
    with open(path, "w", encoding="utf-8") as f:
        f.write(_date_unreleased(content, new_ver, today))


def _clean_dist() -> None:
    dist = "dist"
    if os.path.isdir(dist):
        shutil.rmtree(dist)
        print(f"  Cleaned {dist}/")


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=check)


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """git with its output captured, so a refusal can quote it."""
    try:
        result = subprocess.run(["git", *args], capture_output=True,
                                text=True)
    except OSError as e:
        raise Refused(f"git {' '.join(args)} could not run: {e}") from None
    if check and result.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: "
                      f"{result.stderr.strip() or result.returncode}")
    return result


def _commits(count: int) -> str:
    return f"{count} commit{'s' if count != 1 else ''}"


def _require_main() -> None:
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != "main":
        raise Refused(
            f"This is on '{branch}', not 'main'. A release is cut from main "
            f"only, and no flag lifts that: merge there first.")


def _stdin_is_a_terminal() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (OSError, ValueError):
        return False


def _require_confirmable(yes: bool) -> None:
    """The push is asked about once the commit and the tag exist.

    A run that cannot hear the answer has to stop before them: unanswered,
    both stayed behind, local and unpushed.
    """
    if not yes and not _stdin_is_a_terminal():
        raise Refused(
            "stdin is not a terminal and --yes was not given. The push is "
            "asked about after the commit and the tag. Run this from a "
            "terminal, or pass --yes.")


def _require_clean_tree() -> None:
    # Staged content must be checked too: the release commit stages only
    # src/arduscope/__init__.py and CHANGELOG.md, but a bare `git commit`
    # sweeps EVERYTHING already staged into the release.
    unstaged = _git("diff", "--quiet", check=False)
    staged = _git("diff", "--cached", "--quiet", check=False)
    for result in (unstaged, staged):
        if result.returncode not in (0, 1):
            raise Refused(f"Could not check the tree: "
                          f"{result.stderr.strip() or result.returncode}")
    if unstaged.returncode or staged.returncode:
        raise Refused("Working tree or index is dirty. "
                      "Commit or stash changes first.")


def _remote_has(tag: str) -> bool:
    """Whether origin has the tag. Failing to ask is not an answer."""
    result = _git("ls-remote", "--exit-code", "--tags", "origin",
                  f"refs/tags/{tag}", check=False)
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False
    raise Refused(f"Could not ask origin about tag {tag}: "
                  f"{result.stderr.strip() or result.returncode}")


def _out_of_step(behind: int, ahead: int) -> str:
    """Why main and origin/main are not level, naming the commits."""
    here = _git("log", "--oneline", f"-{MAX_LISTED}",
                f"{MAIN}..HEAD").stdout.rstrip()
    there = _git("log", "--oneline", f"-{MAX_LISTED}",
                 f"HEAD..{MAIN}").stdout.rstrip()
    if ahead and behind:
        head = (f"main and origin/main have diverged: {_commits(ahead)} "
                f"here, {_commits(behind)} on origin")
    elif ahead:
        head = f"main is {_commits(ahead)} ahead of origin/main"
    else:
        head = f"main is {_commits(behind)} behind origin/main"
    listed = "\n".join(part for part in (here, there) if part)
    return (f"{head}:\n{listed}\n"
            f"A release is tagged on the commit origin/main holds. Push "
            f"what belongs there and pull what is missing first.")


def _require_one_push_url() -> None:
    """An atomic push is atomic for each push URL on its own.

    An origin with several would take main and the tag on one of them and
    could refuse them on another, with nothing here the wiser.
    """
    urls = _git("remote", "get-url", "--push", "--all",
                "origin").stdout.splitlines()
    if len(urls) != 1:
        raise Refused(
            f"origin has {len(urls)} push URLs "
            f"({', '.join(urls) or 'none'}). An atomic push is atomic for "
            f"each one on its own, so main and the tag could land on one and "
            f"not on another. Release from a clone whose origin pushes to "
            f"one place.")


def _require_publishable(tag: str) -> None:
    """main here is the commit origin has, and the tag is new everywhere.

    origin is asked rather than assumed. A push does not check this by
    itself: a tag already on origin that matches the local one is no change
    to git, so main would go out under a release that was made before.
    """
    _require_one_push_url()
    # --no-tags: this must not bring tags here; the local tag check below
    # is about what this machine made.
    _git("fetch", "--no-tags", "origin", "main")
    if _git("rev-parse", "-q", "--verify", MAIN,
            check=False).returncode != 0:
        raise Refused(
            "origin/main is not there after the fetch. Is origin the "
            "repository releases go to, and does it have a main branch?")
    behind, ahead = (int(count) for count in _git(
        "rev-list", "--left-right", "--count", f"{MAIN}...HEAD"
    ).stdout.split())
    if behind or ahead:
        raise Refused(_out_of_step(behind, ahead))
    local = _git("rev-parse", "-q", "--verify", f"refs/tags/{tag}",
                 check=False).returncode == 0
    if _remote_has(tag):
        raise Refused(
            f"Tag {tag} already exists on origin{' and here' if local else ''}"
            f". Tags are append-only: a tag on origin is not moved or "
            f"reused. Bump differently.")
    if local:
        raise Refused(
            f"Tag {tag} exists here but not on origin: an interrupted "
            f"earlier run, most likely. It is local only, so it may be "
            f"deleted and this release made again (git tag -d {tag}). A tag "
            f"on origin may not be: tags are append-only.")
    print(f"  main is level with origin/main; {tag} is new here and on origin")


def _require_gate_pass() -> None:
    commit = release_gate.head_commit()
    if not release_gate.passed(commit):
        raise Refused(f"No passing gate for {commit[:10]}. "
                      f"Run tools/release_gate.py on this commit first.")


def _require_notes() -> None:
    # The notes have to exist before anything is written: this used to bump
    # src/arduscope/__init__.py first and only then find [Unreleased] empty,
    # refusing the release but leaving a dirty version file behind.
    with open("CHANGELOG.md", encoding="utf-8") as f:
        problem = _unreleased_problem(f.read())
    if problem:
        raise Refused(problem)


def _ask(question: str) -> str:
    """input(), with a closed stdin or Ctrl-C read as "no".

    By now the commit and the tag exist. A traceback here would end the run
    without the instruction to push them by hand.
    """
    try:
        return input(question).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _push_by_hand(tag: str) -> None:
    print("\n  To push by hand:")
    print(f"    git push --atomic origin main {tag}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ArduScope release helper")
    parser.add_argument("part", choices=["major", "minor", "patch"])
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--yes", action="store_true",
                        help="Push without asking (for headless runs)")
    args = parser.parse_args()

    old_ver = _current_version()
    try:
        new_ver = _bump(old_ver, args.part)
    except Refused as e:
        sys.exit(str(e))
    tag = f"v{new_ver}"

    print(f"\n  {old_ver} -> {new_ver} ({args.part})\n")

    if args.dry_run:
        print("  [dry-run] No changes made.")
        return

    # Pre-flight checks, the cheapest and most local first. Nothing is
    # written until every one of them has passed.
    try:
        _require_main()
        _require_confirmable(args.yes)
        _require_clean_tree()
        _require_publishable(tag)
        _require_gate_pass()
        _require_notes()
        bumped = _bumped_bytes(new_ver)
    except Refused as e:
        sys.exit(str(e))

    # 0. Clean dist/
    _clean_dist()

    # 1. Bump version
    _update_version_file(bumped)
    print(f"  Updated src/arduscope/__init__.py -> {new_ver}")

    # 2. Date the release notes
    _update_changelog(new_ver)
    print(f"  Dated the [Unreleased] section as {new_ver}")

    # 3. Commit + tag
    _run(["git", "add", "src/arduscope/__init__.py", "CHANGELOG.md"])
    # "chore: " prefix — the commit-msg hook only accepts the conventional
    # type whitelist (feat|fix|...|chore|...); a bare "release:" is rejected.
    _run(["git", "commit", "-m", f"chore: release v{new_ver}"])
    _run(["git", "tag", tag])
    print(f"\n  Tagged {tag}")

    # 4. Push
    print(f"\n  Ready to push. This will trigger the GitHub Release workflow.")
    if args.yes or _ask("  Push to origin? [y/N] ") == "y":
        # main and the tag in one atomic push, or neither: a push main
        # refuses must not leave the tag behind, since the tag alone starts
        # the release workflow. Only THIS tag — `--tags` would push every
        # stray local tag, and each one triggers the workflow.
        try:
            _run(["git", "push", "--atomic", "origin", "main", tag])
        except subprocess.CalledProcessError as e:
            print(f"\n  Push failed (git exited {e.returncode}). The commit "
                  f"and the tag are local.")
            _push_by_hand(tag)
            sys.exit(1)
        print(f"\n  Pushed. GitHub Actions will build a draft release. When it")
        print(f"  is there, check its Windows package, then publish it:")
        print(f"    python3 tools/win_package_check.py --release {tag}")
        print(f"    gh release edit {tag} --draft=false")
    else:
        print(f"\n  Skipped push.")
        _push_by_hand(tag)


if __name__ == "__main__":
    main()
