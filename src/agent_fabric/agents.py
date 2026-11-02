"""Which agent is calling `kb`: its own variable, then its hooks' marker, then what it says (docs/design.md §4.10)."""

from collections.abc import Iterable, Mapping


def _from_env(env: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """The agents whose variables are set, with what links the note to their session."""
    found: dict[str, dict[str, str]] = {}
    if env.get("AI_AGENT", "").startswith("claude-code_"):
        variables = (("session", "CLAUDE_CODE_SESSION_ID"), ("entrypoint", "CLAUDE_CODE_ENTRYPOINT"))
        found["Claude Code"] = {key: env[var] for key, var in variables if env.get(var)}
    if env.get("CODEX_THREAD_ID"):  # set on every shell command Codex runs
        found["Codex"] = {"session": env["CODEX_THREAD_ID"]}
    return found


# `--agent` values (lowercased) that name a tool. Not "claude": Cursor running a Claude model might say that.
TOOLS = {
    "claude code": "Claude Code",
    "codex": "Codex",
    "codex cli": "Codex",
    "copilot cli": "Copilot CLI",
    "copilot": "Copilot CLI",
    "github copilot cli": "Copilot CLI",
    "cursor cli": "Cursor CLI",
    "cursor": "Cursor CLI",
    "cursor agent": "Cursor CLI",
}


# The agents kb knows no variable of. By their names in `hooks.EVENTS`: their hooks' markers say who is at work.
KNOWN_BY_MARKER = {"copilot": "Copilot CLI", "cursor": "Cursor CLI"}


def detect_agent(env: Mapping[str, str], flag: str | None, at_work: Iterable[str] = ()) -> dict[str, str] | None:
    """The agent from its own variable, else the hooks' markers (`at_work`), else `--agent`; None when none can tell.

    An agent started by another inherits its variables, so `--agent` naming a tool wins over them, and with two
    agents' variables set `kb` needs it. A flag that names no tool (a model name, say) loses to the environment.
    Without a variable this can't be Claude Code or Codex, whose variables are always set, so only the others' markers
    count. A flag that contradicts them loses: models have named the wrong tool of their own accord."""
    found = _from_env(env)
    tool = TOOLS.get((flag or "").strip().lower())
    if not found:
        marked = {KNOWN_BY_MARKER[agent] for agent in at_work if agent in KNOWN_BY_MARKER}
        if len(marked) == 1:
            return {"name": marked.pop(), "from": "hook"}
        if marked:  # both at work here: only a flag naming one of them can tell which
            return {"name": tool, "from": "flag"} if tool and tool in marked else None
    if tool:
        return {"name": tool, "from": "env", **found[tool]} if tool in found else {"name": tool, "from": "flag"}
    if len(found) == 1:
        name, details = next(iter(found.items()))
        return {"name": name, "from": "env", **details}
    if flag:  # names no tool; with two agents' variables set, it's still the best word on which one this is
        return {"name": flag, "from": "flag"}
    return None
