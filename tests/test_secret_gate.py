"""Seam: redaction and the gitleaks check (docs/design.md §4.5, §10). Every fake secret is built here at test time, so
this file trips no scanner, and none of them works anywhere."""

import os
import shutil
import stat
from pathlib import Path

import pytest

from agent_fabric.secretgate import GateUnavailable, redact, scan

ALNUM = "Qx7Pm2Vb9Tz4Lk8Rw3Nc6Hy1Jd5Gs0Fa"
AWS_KEY = "AKIA" + "QYLPMN5HHHFPZAM2"
GITHUB_PAT = "ghp" + "_" + ALNUM + "Ab3D"
FINE_GRAINED = "github" + "_pat_" + ALNUM + "_" + ALNUM + ALNUM
OPENAI = "sk" + "-" + ALNUM + ALNUM
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + "." + ALNUM
PRIVATE_KEY = "-----BEGIN RSA " + "PRIVATE KEY-----\n" + ALNUM + "\n" + ALNUM + "\n-----END RSA " + "PRIVATE KEY-----"
CORPUS = {
    "aws": AWS_KEY,
    "github-pat": GITHUB_PAT,
    "github-fine-grained": FINE_GRAINED,
    "openai": OPENAI,
    "jwt": JWT,
    "bearer": "Bearer " + ALNUM,
    "private-key": PRIVATE_KEY,
    "password-assignment": "DB_PASSWORD=" + ALNUM,
    "api-key-assignment": "STRIPE_API_KEY: " + ALNUM,
    "lower-case-token-assignment": "auth_token = " + ALNUM,
}
ENV = {"HOME": "/nonexistent", "PATH": os.environ["PATH"]}
needs_gitleaks = pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks is not installed here")


@pytest.mark.parametrize("secret", CORPUS.values(), ids=CORPUS.keys())
def test_redaction_removes_each_shape_and_keeps_the_text_around_it(secret: str) -> None:
    clean = redact(f"before {secret} after")

    assert ALNUM not in clean
    assert "QYLPMN5" not in clean
    assert clean.startswith("before ")
    assert clean.endswith(" after")


def test_redaction_removes_an_email_address() -> None:
    assert "someone@example.test" not in redact("ask someone@example.test about it")


def test_redaction_keeps_a_git_remote() -> None:
    assert redact("origin is git@github.com:me/repo.git") == "origin is git@github.com:me/repo.git"


@pytest.mark.parametrize("home", ["/home/alice", "/Users/alice"], ids=["linux", "mac"])
def test_redaction_takes_the_user_name_out_of_a_home_path(home: str) -> None:
    assert redact(f"see {home}/code/x.py") == "see ~/code/x.py"


@pytest.mark.parametrize(
    "ordinary",
    [
        "the token expires after an hour",
        "KEY=value is how the config reads",
        "use sk-learn for this",
        "pytest -k test_password_reset",
    ],
    ids=["a-word", "a-plain-key", "a-short-sk", "a-test-name"],
)
def test_redaction_leaves_ordinary_text_alone(ordinary: str) -> None:
    assert redact(ordinary) == ordinary


@needs_gitleaks
@pytest.mark.parametrize("secret", CORPUS.values(), ids=CORPUS.keys())
def test_gitleaks_finds_nothing_after_redaction(secret: str) -> None:
    assert scan(redact(f"before {secret} after"), ENV) == []


@needs_gitleaks
def test_the_scan_names_the_rule_and_never_the_secret() -> None:
    rules = scan(f"aws_access_key_id = {AWS_KEY}", ENV)

    assert rules == ["aws-access-token"]


def stub(tmp_path: Path, script: str) -> dict[str, str]:
    folder = tmp_path / "stub"
    folder.mkdir()
    gitleaks = folder / "gitleaks"
    gitleaks.write_text(f"#!/bin/sh\n{script}\n")
    gitleaks.chmod(gitleaks.stat().st_mode | stat.S_IEXEC)
    return {"HOME": str(tmp_path), "PATH": f"{folder}:{os.environ['PATH']}"}


def test_a_missing_gitleaks_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(GateUnavailable, match="gitleaks"):
        scan("anything", {"HOME": str(tmp_path), "PATH": str(tmp_path)})


@pytest.mark.parametrize(
    "script",
    ["exit 3", "echo '[]'; exit 3", "echo 'not json'", 'echo \'{"not": "a list"}\''],
    ids=["a-failure", "a-clean-report-then-a-failure", "junk", "the-wrong-shape"],
)
def test_a_gitleaks_that_misbehaves_fails_closed(tmp_path: Path, script: str) -> None:
    with pytest.raises(GateUnavailable, match="gitleaks"):
        scan("anything", stub(tmp_path, script))


@pytest.mark.parametrize(
    "text",
    [
        'password="correct horse battery staple" next',
        "export API_KEY='correct horse battery staple' next",
        '"password": "correct horse battery staple", next',
        '{"client_secret": "correct horse battery staple"} next',
        'password="correct horse battery staple',
        "x-api-key: correct next",
        "deploy --api-key correct next",
        "deploy --password=correct next",
    ],
    ids=[
        "double-quoted",
        "single-quoted",
        "json-field",
        "json-object",
        "a-quote-left-open",
        "a-header",
        "a-flag-and-its-value",
        "a-flag-with-equals",
    ],
)
def test_redaction_removes_a_whole_named_value_however_it_is_written(text: str) -> None:
    clean = redact(text)

    assert "correct" not in clean
    assert "horse" not in clean
    assert "staple" not in clean


def test_redaction_keeps_the_text_after_a_quoted_value() -> None:
    assert redact('password="a b c" and more') == "password=[redacted] and more"


def test_a_private_key_with_no_end_marker_is_redacted_to_the_end_of_the_text() -> None:
    clean = redact("before\n-----BEGIN RSA " + "PRIVATE KEY-----\n" + ALNUM + "\n" + ALNUM + "\n")

    assert ALNUM not in clean
    assert clean.startswith("before\n")


def test_redacting_a_private_key_keeps_the_line_numbers_of_what_follows() -> None:
    clean = redact("before\n" + PRIVATE_KEY + "\nafter\n")

    assert clean.split("\n").index("after") == ("before\n" + PRIVATE_KEY).count("\n") + 1


@pytest.mark.parametrize(
    "ordinary",
    [
        "src/pages/home/Hero.tsx renders twice",
        "https://example.test/home/docs",
        "import x from './Users/List'",
        "~/code/home/proj",
        "app/Users/Profile.php",
    ],
    ids=["a-relative-path", "a-url", "a-dot-path", "a-tilde-path", "a-capital-folder"],
)
def test_redaction_leaves_a_path_that_only_contains_a_home_folder_alone(ordinary: str) -> None:
    assert redact(ordinary) == ordinary


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("cd /home/alice/code", "cd ~/code"),
        ('"/home/alice/code"', '"~/code"'),
        ("--root=/home/alice/code", "--root=~/code"),
        ("(/Users/alice/code)", "(~/code)"),
        ("file:///home/alice/code", "file://~/code"),
    ],
    ids=["after-a-space", "in-quotes", "after-equals", "in-brackets", "a-file-url"],
)
def test_redaction_takes_the_user_name_out_wherever_an_absolute_home_path_starts(text: str, expected: str) -> None:
    assert redact(text) == expected
