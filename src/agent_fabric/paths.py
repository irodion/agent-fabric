"""Where agent-fabric keeps its files (docs/design.md §5), and how paths are shown."""

from collections.abc import Mapping
from pathlib import Path


def inbox_dir(env: Mapping[str, str]) -> Path:
    data_home = Path(env.get("XDG_DATA_HOME") or "")
    if not data_home.is_absolute():  # the XDG spec says to ignore a relative value
        data_home = Path(env["HOME"]) / ".local" / "share"
    return data_home / "agent-fabric" / "inbox"


def tilde(path: Path, env: Mapping[str, str]) -> str:
    """`/home/me/code/x` → `~/code/x`, so notes and output don't carry the user name."""
    home = Path(env["HOME"])
    return "~/" + str(path.relative_to(home)) if path.is_relative_to(home) else str(path)
