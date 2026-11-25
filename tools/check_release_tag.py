#!/usr/bin/env python3
"""Check a release tag before release.yml builds anything from it.

The workflow starts on any push of a `v*` tag and builds whatever the tag
points at, under the tag's name. A malformed tag, one whose commit is not on
origin/main, or one whose version is not the tagged commit's would still
make a draft, and a draft like that cannot be published. The workflow runs
this in its first job, so that nothing is built from such a tag:

    python3 tools/check_release_tag.py [--tag vX.Y.Z] [--main-ref REF]

The tag defaults to GITHUB_REF_NAME and main to the remote-tracking ref
refs/remotes/origin/main, by its full name. When it passes, `version=X.Y.Z`
goes to the file named by GITHUB_OUTPUT, for the Windows package name and
the release notes, and to standard output.

Exit 0 = the tag may be built; 1 = it may not, and why.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys

# vX.Y.Z with each number written the one way. 01.14.0 and 1.14.0 are one
# version to setuptools, which names the wheel and the sdist by the
# normalised form, and two to the tag, which names the Windows package.
NUMBER = r"(?:0|[1-9][0-9]*)"
TAG = re.compile(rf"v{NUMBER}\.{NUMBER}\.{NUMBER}")
# By its full name: "origin/main" on its own is also a tag someone could
# have made, and git takes a tag before a remote-tracking branch.
MAIN_REF = "refs/remotes/origin/main"
VERSION_FILE = "src/arduscope/__init__.py"


class TagError(Exception):
    """Why the tag may not be built."""


def tag_version(tag: str) -> str:
    """X.Y.Z from vX.Y.Z: three numbers, none with a leading zero, and
    nothing else."""
    if not TAG.fullmatch(tag):
        raise TagError(f"tag {tag!r} is not vX.Y.Z with three numbers, none "
                       f"with a leading zero")
    return tag[1:]


def version_assignment(source: bytes | str, where: str) -> ast.expr:
    """The value of the one assignment to __version__ at module level.

    Read as code, with ast. A comment that spells an assignment is not one,
    and a second assignment is not hidden by the first. None, two, or a
    value that is not a plain string are refused rather than guessed at.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as e:
        raise TagError(f"{where} does not parse: {e}") from None
    values = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == "__version__"
               for target in targets):
            values.append(node.value)
    if len(values) != 1:
        raise TagError(f"{where} assigns __version__ {len(values)} times at "
                       f"module level, not once")
    value = values[0]
    if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
        raise TagError(f"__version__ in {where} is not a plain string")
    return value


def source_version(source: bytes | str, where: str) -> str:
    """What the one assignment to __version__ in the source says."""
    return version_assignment(source, where).value


def tagged_version(tag: str, path: str = VERSION_FILE) -> str:
    """__version__ as the tagged commit holds it.

    Read from the tag's own commit with git show, not from a working tree.
    The workflow happens to have that commit checked out; a check that
    depended on it would say something else run from any other tree.
    """
    shown = subprocess.run(
        ["git", "show", f"refs/tags/{tag}^{{commit}}:{path}"],
        capture_output=True)
    if shown.returncode != 0:
        reason = shown.stderr.decode("utf-8", "replace").strip().splitlines()
        raise TagError(f"could not read {path} from tag {tag}: "
                       f"{reason[-1] if reason else shown.returncode}")
    return source_version(shown.stdout, f"{tag}:{path}")


def on_main(commitish: str, main_ref: str = MAIN_REF) -> bool:
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


def check(tag: str, main_ref: str = MAIN_REF) -> str:
    """The version the tag releases, or TagError saying why it may not."""
    if not main_ref.startswith("refs/"):
        raise TagError(f"{main_ref!r} is not a full ref name (refs/...): a "
                       f"tag with that name would answer for the branch")
    version = tag_version(tag)
    tagged = tagged_version(tag)
    if tagged != version:
        raise TagError(
            f"tag {tag} releases {version}, but {VERSION_FILE} in the tagged "
            f"commit says {tagged}: the tagged commit is not that release")
    if not on_main(f"refs/tags/{tag}^{{commit}}", main_ref):
        raise TagError(f"tag {tag} points at a commit that is not on "
                       f"origin/main")
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
    parser.add_argument("--main-ref", default=MAIN_REF,
                        help="default: refs/remotes/origin/main, by full name")
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
