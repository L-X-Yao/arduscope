#!/usr/bin/env python3
"""
Release helper — bump version, date the CHANGELOG, tag, and push.

Run tools/release_gate.py on the commit first: this will not tag a commit
without a pass from it. Release notes go under `## [Unreleased]` as changes
land, and become the new version's section here.

Usage:
    python tools/release.py patch          # 0.3.1 -> 0.3.2
    python tools/release.py minor          # 0.3.1 -> 0.4.0
    python tools/release.py major          # 0.3.1 -> 1.0.0
    python tools/release.py --dry-run patch  # preview without changes
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


def _current_version() -> str:
    with open("__init__.py", encoding="utf-8") as f:
        m = re.search(r'__version__\s*=\s*"(.+?)"', f.read())
    if not m:
        sys.exit("Cannot parse __version__ from __init__.py")
    return m.group(1)


def _bump(version: str, part: str) -> str:
    major, minor, patch = (int(x) for x in version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _update_version_file(new_ver: str) -> None:
    path = "__init__.py"
    with open(path, encoding="utf-8") as f:
        content = f.read()
    content = re.sub(
        r'__version__\s*=\s*".+?"',
        f'__version__ = "{new_ver}"',
        content,
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _date_unreleased(content: str, new_ver: str, today: str) -> str:
    """Turn the `## [Unreleased]` section into this version's section.

    Notes come from whoever made the change. Generated from commit subjects
    they were not fit to publish, which is why v0.9.0 to v0.9.3 went out by
    hand. An empty section means nobody wrote any, so stop.
    """
    heading = "## [Unreleased]"
    start = content.find(heading)
    if start == -1:
        sys.exit("CHANGELOG.md has no [Unreleased] section to release.")
    end = content.find("\n## [", start + len(heading))
    body = content[start + len(heading):end if end != -1 else len(content)]
    if not body.strip():
        sys.exit("The [Unreleased] section of CHANGELOG.md is empty.")
    return content.replace(heading, f"## [{new_ver}] - {today}", 1)


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


def main() -> None:
    parser = argparse.ArgumentParser(description="ArduScope release helper")
    parser.add_argument("part", choices=["major", "minor", "patch"])
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--yes", action="store_true",
                        help="Push without asking (for headless runs)")
    args = parser.parse_args()

    old_ver = _current_version()
    new_ver = _bump(old_ver, args.part)
    tag = f"v{new_ver}"

    print(f"\n  {old_ver} -> {new_ver} ({args.part})\n")

    if args.dry_run:
        print("  [dry-run] No changes made.")
        return

    # Pre-flight checks. Staged content must be checked too: the
    # release commit stages only __init__.py/CHANGELOG.md but a bare
    # `git commit` sweeps EVERYTHING already staged into the release.
    unstaged = subprocess.run(
        ["git", "diff", "--quiet"], capture_output=True
    )
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], capture_output=True
    )
    if unstaged.returncode != 0 or staged.returncode != 0:
        sys.exit("Working tree or index is dirty. "
                 "Commit or stash changes first.")

    tag_exists = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
        capture_output=True,
    )
    if tag_exists.returncode == 0:
        sys.exit(f"Tag {tag} already exists (interrupted earlier run?). "
                 f"Delete it or bump differently.")

    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    if branch != "main":
        print(f"  Warning: releasing from '{branch}', not 'main'")

    commit = release_gate.head_commit()
    if not release_gate.passed(commit):
        sys.exit(f"No passing gate for {commit[:10]}. "
                 f"Run tools/release_gate.py on this commit first.")

    # 0. Clean dist/
    _clean_dist()

    # 1. Bump version
    _update_version_file(new_ver)
    print(f"  Updated __init__.py -> {new_ver}")

    # 2. Date the release notes
    _update_changelog(new_ver)
    print(f"  Dated the [Unreleased] section as {new_ver}")

    # 3. Commit + tag
    _run(["git", "add", "__init__.py", "CHANGELOG.md"])
    # "chore: " prefix — the commit-msg hook only accepts the conventional
    # type whitelist (feat|fix|...|chore|...); a bare "release:" is rejected.
    _run(["git", "commit", "-m", f"chore: release v{new_ver}"])
    _run(["git", "tag", tag])
    print(f"\n  Tagged {tag}")

    # 4. Push
    print(f"\n  Ready to push. This will trigger the GitHub Release workflow.")
    if args.yes or input("  Push to origin? [y/N] ").strip().lower() == "y":
        # Push only THIS tag — `--tags` would push every stray local
        # tag, and each one triggers the release workflow.
        _run(["git", "push", "origin", "main", tag])
        print(f"\n  Pushed. GitHub Actions will build the release.")
    else:
        print(f"\n  Skipped push. To push manually:")
        print(f"    git push origin main {tag}")


if __name__ == "__main__":
    main()
