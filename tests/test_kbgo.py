"""Seam: `kb` as a process, for the commands it hands to `kb-go`, the part of kb written in Go (docs/design.md §6)."""

import subprocess
import sys
from pathlib import Path

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


def test_kbs_help_names_mcp(tmp_path: Path) -> None:
    done = subprocess.run([*KB, "--help"], capture_output=True, text=True, check=False, env={"HOME": str(tmp_path)})
    assert done.returncode == 0
    assert "mcp" in done.stdout
