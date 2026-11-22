#!/usr/bin/env python3
"""Verify a published ArduScope release without opening a browser.

Checks the release through the GitHub API and the local tag:

- the release is published, not a draft, and immutable;
- its tag is the one asked for, and points at the local tag's commit;
- it carries a wheel, an sdist and the Windows zip, all non-empty.

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
KINDS = (
    ("wheel", ".whl"),
    ("sdist", ".tar.gz"),
    ("windows", "win64.zip"),
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


def check(release: dict, tag: str, remote_commit: str,
          local_commit: str) -> list[str]:
    """Return the failed checks, empty when the release is good."""
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

    assets = release.get("assets") or []
    if not assets:
        failed.append("release has no assets")
    names = [a.get("name", "") for a in assets]
    for kind, suffix in KINDS:
        matches = [a for a in assets if a.get("name", "").endswith(suffix)]
        if not matches:
            failed.append(f"no {kind} asset ({suffix})")
        for asset in matches:
            if not asset.get("size"):
                failed.append(f"{kind} asset is empty: {asset.get('name')}")
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
        release = _release(args.repo, args.tag)
        remote_commit = _tag_commit(args.repo, args.tag)
    except (RuntimeError, OSError, KeyError, json.JSONDecodeError) as e:
        print(f"verify_release: {e}", file=sys.stderr)
        return 1

    failed = check(release, args.tag, remote_commit, local_commit)
    if failed:
        for item in failed:
            print(f"FAIL {item}", file=sys.stderr)
        return 1

    print(f"PASS {args.tag} is published and immutable")
    print(f"PASS tag commit {local_commit[:10]}")
    for asset in sorted(release["assets"], key=lambda a: a.get("name", "")):
        print(f"PASS {asset['name']} ({asset['size']} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
