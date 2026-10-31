"""Which agent is calling `kb`, from its environment where it says so (docs/design.md §4.10)."""

from collections.abc import Mapping


def detect_agent(env: Mapping[str, str], flag: str | None) -> dict[str, str] | None:
    """The agent's own environment wins over `--agent`; None when neither names it."""
    ai_agent = env.get("AI_AGENT", "")
    if ai_agent.startswith("claude-code_"):
        agent = {"name": "Claude Code", "from": "env"}
        for key, var in (("session", "CLAUDE_CODE_SESSION_ID"), ("entrypoint", "CLAUDE_CODE_ENTRYPOINT")):
            if env.get(var):
                agent[key] = env[var]
        return agent
    if flag:
        return {"name": flag, "from": "flag"}
    return None
