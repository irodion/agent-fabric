"""The wiki's git repo and ingest's worktree (docs/design.md §4.6, §4.7).

The worktree stays on a detached HEAD, and a branch only moves by `publish`, so nothing here keeps a branch checked
out: deleting `ingest/<date>` by hand always works, which is how a whole batch is rejected."""

import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

MAIN = "main"
TIMEOUT = 120.0  # a commit runs any hooks the machine's git config sets
SKELETON = {
    "index.md": "# Index\n",
    "log.md": "# Log\n",
    ".gitattributes": "log.md merge=union\n",
}


class GitError(Exception):
    """A git command failed; the message says which, and why."""


class Git:
    def __init__(self, folder: Path, env: Mapping[str, str], *, timeout: float = TIMEOUT) -> None:
        self.folder = folder
        self.env = env
        self.timeout = timeout

    def __call__(self, *args: str) -> str:
        done = self._run(*args)
        if done.returncode != 0:
            raise GitError(f"git {' '.join(args)} in {self.folder}: {done.stderr.strip()}")
        return done.stdout

    def ok(self, *args: str) -> bool:
        return self._run(*args).returncode == 0

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=self.folder,
                env=dict(self.env),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise GitError(f"git {args[0]} in {self.folder} timed out after {self.timeout:g} seconds") from e
        except (OSError, ValueError) as e:  # ValueError: a NUL in an argument
            raise GitError(f"can't run git in {self.folder}: {e}") from e


def ready(folder: Path, env: Mapping[str, str]) -> bool:
    """Whether the wiki's repo exists with its first commit: a first run can die between `init` and that commit."""
    return (folder / ".git").exists() and Git(folder, env).ok("rev-parse", "-q", "--verify", f"refs/heads/{MAIN}")


def ensure_wiki(folder: Path, env: Mapping[str, str], agents: str) -> Git:
    """The wiki's repo, created on first use with its skeleton and the `agents` text as its `AGENTS.md`. A repo with
    no commits yet is a first run that died, so it's finished; one with other commits is someone's, and left alone."""
    git = Git(folder, env)
    if ready(folder, env):
        return git
    if (folder / ".git").exists():
        if git.ok("rev-parse", "-q", "--verify", "HEAD"):
            raise GitError(f"{folder} is a git repo with no {MAIN} branch; move it, or give it one")
    else:
        folder.mkdir(parents=True, exist_ok=True)
        if any(folder.iterdir()):
            raise GitError(f"{folder} holds files but isn't a git repo; move them, or make it the wiki's repo")
        git("init", "-q", "-b", MAIN)
    for name, text in {**SKELETON, "AGENTS.md": agents}.items():
        (folder / name).write_text(text, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "wiki: start")
    return git


def open_worktree(wiki: Git, folder: Path, start: str) -> Git:
    """The worktree at `start`, with anything a run that died left behind dropped. It holds nothing of its own, so one
    that no longer works, such as after the wiki was restored from its bundle, is made again."""
    tree = Git(folder, wiki.env)
    if folder.exists() and not ((folder / ".git").exists() and tree.ok("rev-parse", "--git-dir")):
        shutil.rmtree(folder)
    wiki("worktree", "prune")
    if not folder.exists():
        wiki("worktree", "add", "-q", "--detach", str(folder), start)
    tree("checkout", "-q", "--force", "--detach", start)
    tree("clean", "-qfdx")
    return tree


def publish(wiki: Git, tree: Git, branch: str, message: str) -> None:
    """Commit whatever changed in the worktree, and move the branch there. It leaves alone a branch someone has
    checked out, since moving it would turn their checkout into a reversal of this work, or one that moved since the
    worktree started from it, since that would lose their commit."""
    ref = f"refs/heads/{branch}"
    if f"branch {ref}" in wiki("worktree", "list", "--porcelain").splitlines():
        raise GitError(f"{branch} is checked out; switch to {MAIN} so that kb ingest can add to it, and run it again")
    started = tree("rev-parse", "HEAD").strip()
    tree("add", "-A")
    if not tree.ok("diff", "--cached", "--quiet"):
        tree("commit", "-q", "-m", message)
    old = started if wiki.ok("rev-parse", "-q", "--verify", ref) else ""
    if not wiki.ok("update-ref", ref, tree("rev-parse", "HEAD").strip(), old):
        raise GitError(f"{branch} moved during this run, so its work wasn't added; run kb ingest again")
