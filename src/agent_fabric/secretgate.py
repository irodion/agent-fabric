"""Redaction and the gitleaks check over what leaves the machine or enters the wiki (docs/design.md §4.5, §4.7).

It fails closed: if gitleaks is missing or misbehaves, `GateUnavailable` stops the run before anything is sent."""

import json
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

REDACTED = "[redacted]"
SECRET_WORD = r"(?:secret|token|password|passwd|api[_-]?key|access[_-]?key|private[_-]?key|credential)"
SECRET_NAME = rf"[A-Za-z0-9_-]*{SECRET_WORD}[A-Za-z0-9_-]*"
VALUE = r"""(?:"[^"\n]*"?|'[^'\n]*'?|\S+)"""  # a quoted value whole, even with no closing quote


def _key(match: re.Match[str]) -> str:
    """Its lines stay, as blanks: an excerpt is found by line number after this."""
    return REDACTED + "\n" * match[0].count("\n")


PATTERNS: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL),
        _key,
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"), REDACTED),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), f"Bearer {REDACTED}"),
    (re.compile(rf"\b({SECRET_NAME})([\"']?\s*[=:]\s*){VALUE}", re.IGNORECASE), rf"\1\2{REDACTED}"),
    (re.compile(rf"(--{SECRET_NAME}\s+)(?!-){VALUE}", re.IGNORECASE), rf"\1{REDACTED}"),
    (re.compile(r"(?<![A-Za-z0-9._%+-])(?!git@)[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), REDACTED),
    (re.compile(r"(?:(?<=file://)|(?<![\w./~-]))/(?:home|Users)/[^/\s\"')\]]+"), "~"),
)
ARGS = ("gitleaks", "stdin", "--no-banner", "--log-level", "error", "--redact")
REPORT = ("--report-format", "json", "--report-path", "-", "--exit-code", "0")


class GateUnavailable(Exception):
    """gitleaks can't vouch for the text, so nothing goes through."""


def redact(text: str) -> str:
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def scan(text: str, env: Mapping[str, str]) -> list[str]:
    """The ids of the gitleaks rules the text trips, never the secrets themselves."""
    home = Path(env["HOME"])
    try:
        done = subprocess.run(
            [*ARGS, *REPORT],
            input=text,
            cwd=home if home.is_dir() else None,  # not wherever kb was run, which may hold a gitleaks config
            env=dict(env),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GateUnavailable(f"can't run gitleaks: {e}") from e
    if done.returncode != 0:
        raise GateUnavailable(f"gitleaks exited {done.returncode}")
    try:
        findings = json.loads(done.stdout)
        return sorted({finding["RuleID"] for finding in findings})
    except (ValueError, TypeError, KeyError) as e:
        raise GateUnavailable("gitleaks gave a report that can't be read") from e
