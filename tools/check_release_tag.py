#!/usr/bin/env python3
"""Check a release tag before release.yml builds anything from it.

The workflow starts on any push of a `v*` tag and builds whatever the tag
points at, under the tag's name. A malformed tag, one whose commit is not on
origin/main, or one whose version is not the tagged commit's would still
make a draft, and a draft like that cannot be published. This stops first:

    python3 tools/check_release_tag.py [--tag vX.Y.Z] [--main-ref REF]

The tag defaults to GITHUB_REF_NAME. When it passes, `version=X.Y.Z` goes to
the file named by GITHUB_OUTPUT, for the Windows package name and the release
notes, and to standard output.

Exit 0 = the tag may be built; 1 = it may not, and why.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
VERSION_FILE = "src/arduscope/__init__.py"


class TagError(Exception):
    """Why the tag may not be built."""


def tag_version(tag: str) -> str:
    """X.Y.Z from vX.Y.Z: three numbers, and nothing else."""
    if not TAG.fullmatch(tag):
        raise TagError(f"tag {tag!r} is not vX.Y.Z with three numbers")
    return tag[1:]


def file_version(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        found = re.search(r'__version__\s*=\s*"(.+?)"', f.read())
    if not found:
        raise TagError(f"no __version__ in {path}")
    return found.group(1)


def on_main(commitish: str, main_ref: str) -> bool:
    """Whether main_ref contains the commit. merge-base --is-ancestor answers
    0 or 1; anything else means it could not answer, which is not a "no"."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commitish, main_ref],
        capture_output=True, text=True)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise TagError(
        f"could not place {commitish} against {main_ref}: "
        f"{result.stderr.strip() or result.returncode}")


def check(tag: str, main_ref: str = "origin/main",
          version_file: str = VERSION_FILE) -> str:
    """The version the tag releases, or TagError saying why it may not."""
    version = tag_version(tag)
    tagged = file_version(version_file)
    if tagged != version:
        raise TagError(
            f"tag {tag} releases {version}, but {version_file} says "
            f"{tagged}: the tagged commit is not that release")
    if not on_main(f"refs/tags/{tag}^{{commit}}", main_ref):
        raise TagError(f"tag {tag} points at a commit that is not on "
                       f"{main_ref}")
    return version


def emit(version: str, output_file: str | None) -> None:
    line = f"version={version}"
    if output_file:
        with open(output_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check a release tag before it is built")
    parser.add_argument("--tag", default=os.environ.get("GITHUB_REF_NAME"),
                        help="default: GITHUB_REF_NAME")
    parser.add_argument("--main-ref", default="origin/main")
    args = parser.parse_args(argv)

    if not args.tag:
        print("FAIL no tag to check: pass --tag or set GITHUB_REF_NAME",
              file=sys.stderr)
        return 1
    try:
        version = check(args.tag, args.main_ref)
    except (TagError, OSError) as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1
    emit(version, os.environ.get("GITHUB_OUTPUT"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
