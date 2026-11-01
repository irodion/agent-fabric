"""`kb doctor`'s report: one TOON document (spec 4.4) after AXI's principles (docs/design.md §4.10).

An agent reads it as often as a person does, and setup steps are always the person's: an agent's sandbox can't
change the agents' settings."""

import re
from dataclasses import dataclass

FIELDS = ("status", "check", "path", "detail")
NUMERIC = re.compile(r"^[+-]?[0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?$", re.IGNORECASE)
ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}
RUN_FIX = "In your own terminal, not in an agent's session, run kb doctor --fix (or rerun scripts/install.sh)"
HELP = (
    "These steps are for the user: an agent's sandbox can't change the agents' settings. "
    "Agents: explain them to the user in plain words."
)


@dataclass(frozen=True)
class Row:
    """One check: `ok`, `fixed`, `missing` or `outdated` (which `kb doctor --fix` fixes), or `problem` (by hand)."""

    status: str
    check: str  # inbox, skill, sandbox, hooks
    path: str  # as shown, with ~
    detail: str
    step: str | None = None  # what the user does for this row, where kb can't


def value(text: str) -> str:
    """A string as TOON writes it: bare where the spec allows, quoted and escaped otherwise (spec 4.4 §7)."""
    quote = (
        text == ""
        or text != text.strip(" \t")
        or text in ("true", "false", "null")
        or NUMERIC.match(text) is not None
        or any(char in text for char in ':"\\[]{},')
        or any(ord(char) < 0x20 for char in text)
        or text.startswith(("-", "#"))
    )
    if not quote:
        return text
    escaped = "".join(ESCAPES.get(char, f"\\u{ord(char):04x}" if ord(char) < 0x20 else char) for char in text)
    return f'"{escaped}"'


def _summary(rows: list[Row]) -> str:
    """The counts first, so the reader knows at once whether anything is left to do."""
    fixed = sum(row.status == "fixed" for row in rows)
    by_fix = sum(row.status in ("missing", "outdated") for row in rows)
    by_hand = sum(row.status == "problem" for row in rows)
    if not by_fix and not by_hand:
        return (
            f"fixed {fixed} of {len(rows)} checks; nothing left to do"
            if fixed
            else f"all {len(rows)} checks ok; nothing to do"
        )
    left = by_fix + by_hand
    how = [f"{by_fix} by kb doctor --fix"] * bool(by_fix) + [f"{by_hand} by hand"] * bool(by_hand)
    need = f"{left} of {len(rows)} checks {'need' if left > 1 else 'needs'} fixing: {', '.join(how)}"
    return f"fixed {fixed}; {need}" if fixed else need


def _steps(rows: list[Row], *, fix: bool) -> list[str]:
    """What the user does next, in order."""
    steps = []
    if not fix and any(row.status in ("missing", "outdated") for row in rows):
        steps.append(RUN_FIX)
    steps += [row.step or f"Fix {row.path} by hand: {row.detail}" for row in rows if row.status == "problem"]
    steps += [row.step for row in rows if row.status != "problem" and row.step]
    if fix and any(row.status in ("missing", "outdated") for row in rows):  # left for after the problems
        steps.append("After the steps above, run kb doctor --fix again")
    return steps


def render(rows: list[Row], *, fix: bool) -> str:
    lines = [f"summary: {value(_summary(rows))}", f"checks[{len(rows)}]{{{','.join(FIELDS)}}}:"]
    lines += ["  " + ",".join(value(getattr(row, field)) for field in FIELDS) for row in rows]
    steps = _steps(rows, fix=fix)
    if steps:
        lines.append(f"next[{len(steps)}]{{by,step}}:")
        lines += [f"  user,{value(step)}" for step in steps]
        lines.append(f"help: {value(HELP)}")
    else:
        lines.append("next: []")
    return "".join(f"{line}\n" for line in lines)
