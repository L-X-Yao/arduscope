#!/usr/bin/env python3
"""Verify a published ArduScope release without opening a browser.

Checks the release through the GitHub API and the local tag:

- the release is published, not a draft, and immutable;
- its tag is the one asked for, and points at the local tag's commit;
- that commit is on the main of the repository asked about;
- it carries the wheel, the sdist and the Windows zip, each by name and
  non-empty, and nothing else.

The Windows package check is a separate, real-Windows step; this script only
checks the release record and its assets.

Usage:
    python3 tools/verify_release.py v0.13.2
    python3 tools/verify_release.py v0.13.2 --repo L-X-Yao/arduscope
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

DEFAULT_REPO = "L-X-Yao/arduscope"
MAIN_BRANCH = "main"
# A release's three assets by name. Anything else is not of this release:
# not an extra file, and not a wheel for 0.13.2rc1 or 0.13.2.post1, which
# carry the version as a substring.
ASSETS = (
    ("wheel", "ardupilot_scope-{version}-py3-none-any.whl"),
    ("sdist", "ardupilot_scope-{version}.tar.gz"),
    ("windows", "arduscope-v{version}-win64.zip"),
)


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{cmd[0]} failed")
    return result.stdout


def _release(repo: str, tag: str) -> dict:
    return json.loads(_run(["gh", "api",
                            f"repos/{repo}/releases/tags/{tag}"]))


def _tag_commit(repo: str, tag: str) -> str:
    ref = json.loads(_run(["gh", "api", f"repos/{repo}/git/ref/tags/{tag}"]))
    obj = ref["object"]
    if obj.get("type") == "tag":
        # An annotated tag's ref points at the tag object, not the commit.
        # release.py makes lightweight tags, but peel one if it is annotated.
        tag_obj = json.loads(_run(
            ["gh", "api", f"repos/{repo}/git/tags/{obj['sha']}"]))
        return tag_obj["object"]["sha"]
    return obj["sha"]


def _local_tag_commit(tag: str) -> str:
    return _run(["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"]).strip()


def _on_main(repo: str, commit: str) -> bool:
    """Whether the repository's main contains the commit, asked of GitHub.

    repos/<repo>/compare/<commit>...main takes the commit as the base and
    main as the head: "ahead" means main has it behind its tip, and
    "identical" that it is the tip. Anything else, the repository not
    having the commit included, is not a yes.

    The repository asked about with --repo is the one that answers. A local
    origin/main belongs to whatever repository was cloned, which with a fork
    is another one.
    """
    compare = json.loads(_run(["gh", "api",
                               f"repos/{repo}/compare/{commit}...{MAIN_BRANCH}"]))
    return compare.get("status") in ("ahead", "identical")


def expected_assets(version: str) -> dict[str, str]:
    """Asset name by kind for this version."""
    return {template.format(version=version): kind for kind, template in ASSETS}


def check(release: dict, tag: str, remote_commit: str, local_commit: str, *,
          on_main: bool) -> list[str]:
    """Return the failed checks, empty when the release is good.

    on_main says whether main of the repository asked about contains the tag
    commit; it is not optional, since a default would let a caller forget
    to ask.
    """
    failed: list[str] = []
    if release.get("draft") is not False:
        failed.append("release is not published (draft is not false)")
    if release.get("immutable") is not True:
        failed.append("release is not immutable")
    if not release.get("published_at"):
        failed.append("release has no published_at")
    if release.get("tag_name") != tag:
        failed.append(f"release tag is {release.get('tag_name')!r}, not {tag!r}")
    if remote_commit != local_commit:
        failed.append(
            f"tag points at {remote_commit}, local tag at {local_commit}")
    if not on_main:
        failed.append(f"tag commit {local_commit} is not on main")

    assets = release.get("assets") or []
    if not assets:
        failed.append("release has no assets")
    expected = expected_assets(tag.removeprefix("v"))
    names = [a.get("name", "") for a in assets]
    for name, kind in expected.items():
        if name not in names:
            failed.append(f"no {kind} asset ({name})")
    for asset in assets:
        name = asset.get("name", "")
        kind = expected.get(name)
        if kind is None:
            failed.append(f"asset {name!r} is not one of v"
                          f"{tag.removeprefix('v')}'s three")
        elif not asset.get("size"):
            failed.append(f"{kind} asset is empty: {name}")
    if len(names) != len(set(names)):
        failed.append("release has duplicate asset names")
    return failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tag", help="release tag, for example v0.13.2")
    parser.add_argument("--repo", default=DEFAULT_REPO,
                        help=f"GitHub repository (default {DEFAULT_REPO})")
    args = parser.parse_args(argv)

    try:
        local_commit = _local_tag_commit(args.tag)
        on_main = _on_main(args.repo, local_commit)
        release = _release(args.repo, args.tag)
        remote_commit = _tag_commit(args.repo, args.tag)
    except (RuntimeError, OSError, KeyError, json.JSONDecodeError) as e:
        print(f"verify_release: {e}", file=sys.stderr)
        return 1

    failed = check(release, args.tag, remote_commit, local_commit,
                   on_main=on_main)
    if failed:
        for item in failed:
            print(f"FAIL {item}", file=sys.stderr)
        return 1

    print(f"PASS {args.tag} is published and immutable")
    print(f"PASS tag commit {local_commit[:10]}")
    print(f"PASS tag commit is on main of {args.repo}")
    for name in expected_assets(args.tag.removeprefix("v")):
        asset = next(a for a in release["assets"] if a["name"] == name)
        print(f"PASS {name} ({asset['size']} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
