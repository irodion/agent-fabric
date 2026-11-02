"""Which agent is calling `kb`: its own variable, then its hooks' marker, then what it says (docs/design.md §4.10)."""

from collections.abc import Callable, Iterable, Mapping


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


class AgentUnknown(ValueError):
    """`kb` can't tell which agent this is; the message says what to add."""


def detect_agent(env: Mapping[str, str], flag: str | None, at_work: Callable[[], Iterable[str]]) -> dict[str, str]:
    """The agent from its own variable, else the hooks' markers (`at_work`, read only then), else `--agent`.

    An agent started by another inherits its variables, so `--agent` naming a tool wins over them, and with two
    agents' variables set `kb` needs it. A flag that names no tool (a model name, say) loses to the environment.

    Without a variable this can't be Claude Code or Codex, whose variables are always set, so only the others' markers
    count, and they beat a flag naming those two or no tool: models have named the wrong tool of their own accord. A
    flag naming Copilot CLI or Cursor CLI is taken at its word, since one's finished session is no proof against the
    other, whose marker may not exist yet."""
    found = _from_env(env)
    tool = TOOLS.get((flag or "").strip().lower())
    if not found and tool not in KNOWN_BY_MARKER.values():
        marked = {KNOWN_BY_MARKER[agent] for agent in at_work() if agent in KNOWN_BY_MARKER}
        if len(marked) == 1:
            return {"name": marked.pop(), "from": "hook"}
        if marked:
            raise AgentUnknown(
                "kb can't tell which agent you are: Copilot CLI and Cursor CLI have both run in this project lately; "
                "add --agent with the one you are"
            )
    if tool:
        return {"name": tool, "from": "env", **found[tool]} if tool in found else {"name": tool, "from": "flag"}
    if len(found) == 1:
        name, details = next(iter(found.items()))
        return {"name": name, "from": "env", **details}
    if flag:  # names no tool; with two agents' variables set, it's still the best word on which one this is
        return {"name": flag, "from": "flag"}
    raise AgentUnknown(
        "kb can't tell which agent you are; add --agent with your tool's name "
        "(Claude Code, Codex, Copilot CLI, Cursor CLI), not your model's"
    )
