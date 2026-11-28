"""Seam: `kb` as a process, for the commands it hands to `kb-go`, the part of kb written in Go (docs/design.md §6)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import Kb

KB = [sys.executable, "-c", "from agent_fabric.cli import main; main()"]


def test_kb_mcp_runs_kb_go_in_its_place(tmp_path: Path) -> None:
    """`kb-go mcp` takes over the process, its stdin and stdout, which carry the MCP session."""
    stub = tmp_path / "kb-go"
    stub.write_text('#!/bin/sh\necho "kb-go $*"\nread -r line\necho "$line"\n')
    stub.chmod(0o755)
    done = subprocess.run(
        [*KB, "mcp"],
        input="ping\n",
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": str(tmp_path), "HOME": "/"},
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, "kb-go mcp\nping\n", "")


def test_kb_mcp_without_kb_go_says_to_install_it(tmp_path: Path) -> None:
    done = subprocess.run(
        [*KB, "mcp"], capture_output=True, text=True, check=False, env={"PATH": str(tmp_path), "HOME": "/"}
    )
    assert done.returncode == 1
    assert done.stdout == ""
    assert "kb-go" in done.stderr
    assert "scripts/install.sh" in done.stderr


def test_kb_mcp_runs_the_kb_go_beside_it_before_one_on_path(tmp_path: Path) -> None:
    """Where scripts/install.sh puts it: an agent may start `kb` by its path, with a PATH that lacks its folder."""
    installed, elsewhere = tmp_path / "bin", tmp_path / "elsewhere"
    installed.mkdir()
    elsewhere.mkdir()
    kb = installed / "kb"
    kb.write_text(f"#!{sys.executable}\nfrom agent_fabric.cli import main\nmain()\n")
    kb.chmod(0o755)
    for folder in (installed, elsewhere):
        stub = folder / "kb-go"
        stub.write_text(f'#!/bin/sh\necho "{folder.name}"\n')
        stub.chmod(0o755)
    done = subprocess.run(
        [str(kb), "mcp"], capture_output=True, text=True, check=False, env={"PATH": str(elsewhere), "HOME": "/"}
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, "bin\n", "")


def test_kbs_help_names_the_commands_kb_go_serves(tmp_path: Path) -> None:
    done = subprocess.run([*KB, "--help"], capture_output=True, text=True, check=False, env={"HOME": str(tmp_path)})
    assert done.returncode == 0
    for command in ("mcp", "show", "search", "sessions", "catchup", "reindex"):
        assert command in done.stdout
    assert "paths" not in done.stdout  # kb-go's, not the user's


def test_kb_show_hands_its_path_to_kb_go(tmp_path: Path) -> None:
    stub = tmp_path / "kb-go"
    stub.write_text('#!/bin/sh\necho "kb-go $*"\n')
    stub.chmod(0o755)
    done = subprocess.run(
        [*KB, "show", "wiki/projects/a.md"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": str(tmp_path), "HOME": "/"},
    )
    assert (done.returncode, done.stdout) == (0, "kb-go show wiki/projects/a.md\n")


def test_kb_paths_names_the_folders_a_page_is_read_in_for_the_current_folders_workspace(kb: Kb) -> None:
    """What kb-go reads a workspace through, so that which workspace a folder is in has one reader (§4.11, §6): its
    local digests first, then its wiki."""
    result = kb("paths")
    assert result.exit_code == 0, result.stderr
    data = kb.home / ".local" / "share" / "agent-fabric"
    assert json.loads(result.stdout) == {
        "pages": [str(data / "local"), str(kb.home / "wiki")],
        "index": str(data / "index.db"),
    }


def test_kb_paths_in_a_registered_root_names_the_roots_own(kb: Kb) -> None:
    root = kb.home / "work"
    kb.cwd = root / "app"
    kb.cwd.mkdir(parents=True)
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f'[[workspace]]\nroot = "{root}"\n')
    paths = json.loads(kb("paths").stdout)
    local, wiki = paths["pages"]
    data = Path(local).parent
    assert data.parent == kb.home / ".local" / "share" / "agent-fabric" / "workspaces"
    assert data.name.startswith("work-")
    assert wiki == str(root / ".fabric" / "wiki")
    assert paths["index"] == str(data / "index.db")  # out of the root's .fabric/, which an agent there can write


def test_kb_paths_with_a_config_it_cant_read_says_so(kb: Kb) -> None:
    config = kb.home / ".config" / "agent-fabric" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[[workspace]\n")
    result = kb("paths")
    assert (result.exit_code, result.stdout) == (1, "")
    assert "config.toml" in result.stderr


def test_kb_search_hands_its_words_and_options_to_kb_go(tmp_path: Path) -> None:
    stub = tmp_path / "kb-go"
    stub.write_text('#!/bin/sh\nfor arg in "$@"; do echo "[$arg]"; done\n')
    stub.chmod(0o755)
    done = subprocess.run(
        [*KB, "search", "--kind", "page", "two words"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": str(tmp_path), "HOME": "/"},
    )
    assert (done.returncode, done.stdout) == (0, "[search]\n[--kind]\n[page]\n[two words]\n")


@pytest.mark.parametrize(
    ("project", "sessions", "notes", "page"),
    [
        (
            "github.com/me/app",
            "raw/sessions/github.com/me/app/",
            "raw/notes/github.com/me/app/",
            "wiki/projects/github.com/me/app.md",
        ),
        # A project with no remote is named by its folder: its digests and notes are where kb wrote them.
        ("~/code/a b", "raw/sessions/unknown/code/a-b/", "raw/notes/code/a b/", "wiki/projects/code/a b.md"),
        ("machine", "raw/sessions/machine/", "raw/notes/machine/", "wiki/machine.md"),
    ],
)
def test_kb_paths_for_a_project_names_where_kb_writes_its_pages(
    kb: Kb, project: str, sessions: str, notes: str, page: str
) -> None:
    result = kb("paths", "--project", project)
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["project"] == {"session": sessions, "note": notes, "page": page}


def test_kb_paths_for_a_folder_names_its_projects_pages(kb: Kb) -> None:
    """As `kb inbox --project` takes it: a folder is the project kb note would file under there."""
    paths = json.loads(kb("paths", "--project", ".").stdout)
    assert paths["project"]["note"] == "raw/notes/code/scratch/"
