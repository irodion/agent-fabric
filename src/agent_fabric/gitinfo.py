"""Git facts for a folder: project id (docs/design.md §4.4), branch and commit."""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitFacts:
    project: str | None = None
    branch: str | None = None
    commit: str | None = None
    root: Path | None = None


def _git(cwd: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = done.stdout.strip()
    return out if done.returncode == 0 and out else None


def normalise_remote(url: str) -> str:
    """`git@github.com:me/repo.git` and `https://user@github.com:443/me/repo` → `github.com/me/repo`.

    Only a URL with a scheme can carry a port; in the scp-like form `host:path`, a number after the colon is
    the first path segment (`git@gitlab.com:12345/repo`)."""
    url = url.strip()
    if scheme := re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", url):
        rest = re.sub(r"^[^@/]+@", "", url[scheme.end() :])  # user
        rest = re.sub(r"^([^/:]+):\d+(?=/|$)", r"\1", rest)  # port
    else:
        rest = re.sub(r"^[^@/:]+@", "", url)  # user
        rest = re.sub(r"^([^/:]+):", r"\1/", rest, count=1)  # scp-like host:path
    host, _, path = rest.partition("/")
    return re.sub(r"(\.git)?/*$", "", f"{host.lower()}/{path}" if path else host.lower())


def git_facts(cwd: Path) -> GitFacts:
    if _git(cwd, "rev-parse", "--is-inside-work-tree") != "true":
        return GitFacts()
    remote = _git(cwd, "remote", "get-url", "origin")
    if remote:
        project: str | None = normalise_remote(remote)
    else:
        common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
        project = str(Path(common).parent) if common else None
    branch = _git(cwd, "symbolic-ref", "--short", "-q", "HEAD")
    commit = _git(cwd, "rev-parse", "--short", "HEAD")
    root = _git(cwd, "rev-parse", "--show-toplevel")
    return GitFacts(project=project, branch=branch, commit=commit, root=Path(root) if root else None)
