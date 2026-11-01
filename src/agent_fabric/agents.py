"""Which agent is calling `kb`, from its environment where it says so (docs/design.md §4.10)."""

from collections.abc import Mapping


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


def detect_agent(env: Mapping[str, str], flag: str | None) -> dict[str, str] | None:
    """The agent from its environment or `--agent`; None when neither can tell.

    An agent started by another inherits its variables, so `--agent` naming a tool wins over them, and with two
    agents' variables set `kb` needs it. A flag that names no tool (a model name, say) loses to the environment."""
    found = _from_env(env)
    tool = TOOLS.get((flag or "").strip().lower())
    if tool:
        return {"name": tool, "from": "env", **found[tool]} if tool in found else {"name": tool, "from": "flag"}
    if len(found) == 1:
        name, details = next(iter(found.items()))
        return {"name": name, "from": "env", **details}
    if flag:  # names no tool; with two agents' variables set, it's still the best word on which one this is
        return {"name": flag, "from": "flag"}
    return None
