"""Seam: `kb lint` end to end through `cli.run`, on a wiki that `kb ingest` built (docs/design.md §4.7, §8)."""

import shutil
from pathlib import Path
from typing import Any

import toon_format
from conftest import PAGE, Kb, Lab


def findings(kb: Kb) -> list[dict[str, Any]]:
    """What `kb lint` reports, as the official TOON decoder reads it: an independent check of the format."""
    decoded = toon_format.decode(kb("lint").stdout)
    assert isinstance(decoded, dict)
    found: list[dict[str, Any]] = decoded["findings"]
    return found


def checks(kb: Kb) -> list[tuple[str, str]]:
    return [(finding["level"], finding["check"]) for finding in findings(kb)]


def built_wiki(lab: Lab) -> list[str]:
    """Three claims merged to main, the first anchored to src/app.py."""
    notes = []
    for kind, text, refs in (
        ("gotcha", "main returns 1, whatever it was given", ("src/app.py",)),
        ("fix", "the DB fixture needs a socket; run -m 'not db'", ()),
        ("decision", "the wiki stays local, since the machines have no link", ()),
    ):
        notes.append(lab.leave(kind, text, *refs))
        lab.kb.later(minutes=1)
    result = lab.kb("ingest")
    assert result.exit_code == 0, result.stderr
    lab.merge()
    return notes


def edit(lab: Lab, path: str, old: str, new: str) -> None:
    file = lab.wiki / path
    text = file.read_text()
    assert old in text
    file.write_text(text.replace(old, new, 1))


def test_no_wiki_yet_is_nothing_to_report(kb: Kb) -> None:
    result = kb("lint")

    assert result.exit_code == 0
    assert findings(kb) == []


def test_a_wiki_as_ingest_built_it_has_no_findings(lab: Lab) -> None:
    built_wiki(lab)

    result = lab.kb("lint")

    assert result.exit_code == 0, result.stdout
    assert findings(lab.kb) == []


def test_a_claim_citing_a_note_that_is_missing_is_an_error(lab: Lab) -> None:
    notes = built_wiki(lab)
    next((lab.wiki / "raw").rglob(f"*-{notes[0]}.md")).unlink()

    result = lab.kb("lint")

    assert result.exit_code == 1
    assert checks(lab.kb) == [("error", "missing-note")]
    assert findings(lab.kb)[0]["path"] == PAGE
    assert notes[0] in findings(lab.kb)[0]["detail"]


def test_a_claim_with_no_source_line_is_an_error(lab: Lab) -> None:
    notes = built_wiki(lab)
    edit(lab, PAGE, f"- source: {notes[1]} ", f"- from: {notes[1]} ")

    assert lab.kb("lint").exit_code == 1
    assert checks(lab.kb) == [("error", "no-source")]


def test_a_supersedes_pointing_at_nothing_is_an_error(lab: Lab) -> None:
    built_wiki(lab)
    edit(lab, PAGE, "supersedes: none", "supersedes: n0000aa")

    assert lab.kb("lint").exit_code == 1
    assert checks(lab.kb) == [("error", "dangling-supersedes")]


def test_a_supersedes_pointing_at_a_superseded_claim_is_fine(lab: Lab) -> None:
    notes = built_wiki(lab)
    lab.fake.script["main returns 1, and it logs that too"] = {"claim": "replaces"}
    lab.leave("gotcha", "main returns 1, and it logs that too", "src/app.py")
    lab.kb.later(days=8)
    assert lab.kb("ingest").exit_code == 0

    lab.merge("ingest/2026-11-08")

    assert lab.kb("lint").exit_code == 0
    assert notes[0] in (lab.wiki / PAGE).read_text().split("## Superseded")[1]
    assert findings(lab.kb) == []


def test_only_the_live_claim_is_reported_when_a_superseded_claims_file_changes(lab: Lab) -> None:
    notes = built_wiki(lab)
    lab.fake.script["main returns 1, and it logs that too"] = {"claim": "replaces"}
    new = lab.leave("gotcha", "main returns 1, and it logs that too", "src/app.py")
    lab.kb.later(days=8)
    assert lab.kb("ingest").exit_code == 0
    lab.merge("ingest/2026-11-08")

    lab.commit_in_repo("src/app.py", "def main():\n    return 2\n")

    assert checks(lab.kb) == [("warning", "stale-anchor")]
    assert new in findings(lab.kb)[0]["detail"]
    assert notes[0] not in findings(lab.kb)[0]["detail"]


def test_a_broken_link_is_an_error(lab: Lab) -> None:
    built_wiki(lab)
    edit(
        lab,
        "index.md",
        "# Index\n",
        "# Index\n\nSee [the plan](wiki/plan.md) and [the site](https://example.test/).\n",
    )

    assert lab.kb("lint").exit_code == 1
    assert checks(lab.kb) == [("error", "broken-link")]
    assert "wiki/plan.md" in findings(lab.kb)[0]["detail"]


def test_a_claim_whose_anchor_file_changed_is_a_warning_naming_it(lab: Lab) -> None:
    notes = built_wiki(lab)
    lab.commit_in_repo("src/app.py", "def main():\n    return 2\n")

    result = lab.kb("lint")

    assert result.exit_code == 0
    assert checks(lab.kb) == [("warning", "stale-anchor")]
    assert notes[0] in findings(lab.kb)[0]["detail"]
    assert "src/app.py" in findings(lab.kb)[0]["detail"]


def test_a_claim_whose_anchor_file_is_gone_is_a_warning(lab: Lab) -> None:
    built_wiki(lab)
    (lab.repo / "src" / "app.py").unlink()
    lab.git("add", "-A")
    lab.git("commit", "-q", "-m", "drop it")

    assert checks(lab.kb) == [("warning", "stale-anchor")]


def test_another_commit_that_leaves_the_anchor_file_alone_is_no_finding(lab: Lab) -> None:
    built_wiki(lab)
    lab.commit_in_repo("README.md", "# proj\n")

    assert findings(lab.kb) == []


def test_a_claim_whose_checkout_has_gone_is_a_warning_for_the_page(lab: Lab) -> None:
    built_wiki(lab)
    shutil.rmtree(lab.repo)
    lab.kb.cwd = lab.kb.home

    assert checks(lab.kb) == [("warning", "anchor-unchecked")]
    assert findings(lab.kb)[0]["path"] == PAGE


def test_a_page_not_in_the_index_is_an_orphan(lab: Lab) -> None:
    built_wiki(lab)
    orphan = lab.wiki / "wiki" / "projects" / "github.com" / "me" / "lost.md"
    orphan.write_text("# github.com/me/lost\n\n## Gotchas\n\n")

    assert lab.kb("lint").exit_code == 0
    assert checks(lab.kb) == [("warning", "orphan-page")]
    assert findings(lab.kb)[0]["path"] == "wiki/projects/github.com/me/lost.md"


def test_an_oversized_page_is_a_warning(lab: Lab) -> None:
    built_wiki(lab)
    edit(lab, PAGE, "## Fixes\n", "".join(f"line {n}\n" for n in range(500)) + "## Fixes\n")

    assert ("warning", "oversized-page") in checks(lab.kb)


def test_near_duplicate_titles_on_a_page_are_a_warning(lab: Lab) -> None:
    notes = built_wiki(lab)
    edit(lab, PAGE, "### the DB fixture needs a socket; run -m 'not db'", "### main returns 1, whatever given")

    assert checks(lab.kb) == [("warning", "near-duplicate")]
    assert notes[0] in findings(lab.kb)[0]["detail"]
    assert notes[1] in findings(lab.kb)[0]["detail"]


def test_a_status_note_never_resolved_is_a_warning(lab: Lab) -> None:
    note = lab.leave("status", "integration tests fail on main")
    lab.kb.later(days=15)

    assert checks(lab.kb) == [("warning", "stale-status")]
    assert note in findings(lab.kb)[0]["detail"]


def test_a_fresh_status_note_is_no_finding(lab: Lab) -> None:
    lab.leave("status", "integration tests fail on main")

    assert findings(lab.kb) == []


def test_the_report_says_how_many_of_each(lab: Lab) -> None:
    notes = built_wiki(lab)
    next((lab.wiki / "raw").rglob(f"*-{notes[1]}.md")).unlink()
    lab.commit_in_repo("src/app.py", "def main():\n    return 2\n")

    assert lab.kb("lint").stdout.startswith("summary: 1 error and 1 warning\n")


def test_a_link_in_a_notes_own_text_is_not_a_broken_link(lab: Lab) -> None:
    built_wiki(lab)
    lab.leave("gotcha", "the guide at [setup](docs/setup.md) is out of date")
    lab.kb.later(days=8)
    assert lab.kb("ingest").exit_code == 0

    lab.merge("ingest/2026-11-08")

    assert findings(lab.kb) == []


def doctor_rows(kb: Kb, check: str) -> list[dict[str, Any]]:
    decoded = toon_format.decode(kb("doctor").stdout)
    assert isinstance(decoded, dict)
    return [row for row in decoded["checks"] if row["check"] == check]


def test_doctor_is_quiet_about_the_wiki_where_there_is_none(kb: Kb) -> None:
    assert doctor_rows(kb, "wiki") == []
    assert doctor_rows(kb, "ingest") == []


def test_doctor_shows_a_clean_wiki(lab: Lab) -> None:
    built_wiki(lab)

    assert [row["status"] for row in doctor_rows(lab.kb, "wiki")] == ["ok"]


def test_doctor_shows_lint_errors_as_a_problem(lab: Lab) -> None:
    notes = built_wiki(lab)
    next((lab.wiki / "raw").rglob(f"*-{notes[0]}.md")).unlink()

    row = doctor_rows(lab.kb, "wiki")[0]

    assert row["status"] == "problem"
    assert "1 error" in row["detail"]
    assert lab.kb("doctor").exit_code == 1
    assert "kb lint" in lab.kb("doctor").stdout


def test_doctor_shows_lint_warnings_without_calling_them_a_problem(lab: Lab) -> None:
    built_wiki(lab)
    lab.commit_in_repo("src/app.py", "def main():\n    return 2\n")

    row = doctor_rows(lab.kb, "wiki")[0]

    assert row["status"] == "ok"
    assert "1 warning" in row["detail"]


def test_doctor_warns_of_a_branch_waiting_over_seven_days(lab: Lab) -> None:
    built_wiki(lab)
    lab.leave("gotcha", "a new trap about something else entirely")
    lab.kb.later(days=8)
    assert lab.kb("ingest").exit_code == 0
    assert doctor_rows(lab.kb, "ingest") == []

    lab.kb.later(days=8)

    row = doctor_rows(lab.kb, "ingest")[0]
    assert row["status"] == "problem"
    assert row["path"] == "ingest/2026-11-08"
    assert "8 days" in row["detail"]


def test_doctor_says_when_a_tool_ingest_needs_is_missing(lab: Lab, tmp_path: Path) -> None:
    lab.kb.env["PATH"] = str(tmp_path)

    details = [row["detail"] for row in doctor_rows(lab.kb, "ingest")]

    assert any("gitleaks" in detail for detail in details)
    assert any("codex" in detail for detail in details)


def test_doctor_asks_nothing_of_ingest_with_an_empty_allow_list(kb: Kb, tmp_path: Path) -> None:
    kb.env["PATH"] = str(tmp_path)

    assert doctor_rows(kb, "ingest") == []
