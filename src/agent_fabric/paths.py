"""Where agent-fabric keeps its files (docs/design.md §5), and how paths are shown."""

from collections.abc import Mapping
from pathlib import Path


def _data_home(env: Mapping[str, str]) -> Path:
    data_home = Path(env.get("XDG_DATA_HOME") or "")
    if not data_home.is_absolute():  # the XDG spec says to ignore a relative value
        data_home = Path(env["HOME"]) / ".local" / "share"
    return data_home


def inbox_dir(env: Mapping[str, str]) -> Path:
    return _data_home(env) / "agent-fabric" / "inbox"


def wiki_dir(env: Mapping[str, str]) -> Path:
    """This machine's wiki repo (docs/design.md §4.6)."""
    return Path(env["HOME"]) / "wiki"


def ingest_worktree(env: Mapping[str, str]) -> Path:
    """Where `kb ingest` works, so it never touches the wiki's own checkout (docs/design.md §4.7)."""
    return _data_home(env) / "agent-fabric" / "ingest-wt"


def uv_tool_dir(env: Mapping[str, str]) -> Path:
    """Where `uv tool install` puts each tool's own folder, as `uv tool dir` prints it."""
    tool_dir = Path(env.get("UV_TOOL_DIR") or "")
    return tool_dir if tool_dir.is_absolute() else _data_home(env) / "uv" / "tools"


def config_file(env: Mapping[str, str]) -> Path:
    """kb's own settings (docs/design.md §5)."""
    config_home = Path(env.get("XDG_CONFIG_HOME") or "")
    if not config_home.is_absolute():  # the XDG spec says to ignore a relative value
        config_home = Path(env["HOME"]) / ".config"
    return config_home / "agent-fabric" / "config.toml"


def units_dir(env: Mapping[str, str]) -> Path:
    """Where systemd's user manager looks for a user's own units. Not under kb's XDG_CONFIG_HOME: the manager has its
    own environment, which a shell's export doesn't reach, so it reads ~/.config unless set up otherwise."""
    return Path(env["HOME"]) / ".config" / "systemd" / "user"


def hooks_dir(env: Mapping[str, str]) -> Path:
    """The hooks' markers, one per agent session (docs/design.md §4.10)."""
    return inbox_dir(env).parent / "hooks"


def tilde(path: Path, env: Mapping[str, str]) -> str:
    """`/home/me/code/x` → `~/code/x`, so notes and output don't carry the user name."""
    home = Path(env["HOME"])
    return "~/" + str(path.relative_to(home)) if path.is_relative_to(home) else str(path)


def expand(path: str, env: Mapping[str, str]) -> Path:
    """`~/x` → x in the home folder (from `env`, like `tilde`); any other path as it is."""
    if path == "~" or path.startswith("~/"):
        return Path(env["HOME"]) / path[2:]
    return Path(path)
