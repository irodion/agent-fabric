"""Seam: `kb ingest` end to end through `cli.run` (docs/design.md §4.7, §10)."""

import fcntl
import json
import os
import shutil
from pathlib import Path

import pytest
from conftest import BRANCH, LEAK, PAGE, PROJECT, FakeBackend, Kb, Lab

from agent_fabric.backends import BackendError, InvalidAnswer, Question
from agent_fabric.inbox import Inbox
from agent_fabric.notes import Note, parse
from agent_fabric.paths import config_file
from agent_fabric.wikirepo import Git, GitError


def three_notes(lab: Lab) -> list[str]:
    """Three notes, a minute apart: a run takes them oldest first."""
    notes = []
    for kind, text in (
        ("gotcha", "uv needs --cache-dir in the sandbox"),
        ("fix", "the DB fixture needs a socket; run -m 'not db'"),
        ("decision", "the wiki stays local, since the machines have no link"),
    ):
        notes.append(lab.leave(kind, text))
        lab.kb.later(minutes=1)
    return notes


def test_three_open_notes_are_filed_on_a_review_branch_and_main_stays_as_it_was(lab: Lab) -> None:
    three_notes(lab)

    result = lab.kb("ingest")

    assert result.exit_code == 0, result.stderr
    page = lab.show(BRANCH, PAGE)
    assert "uv needs --cache-dir in the sandbox\n" in page
    assert "the DB fixture needs a socket; run -m 'not db'\n" in page
    assert "the wiki stays local, since the machines have no link\n" in page
    assert PAGE not in lab.wiki_git("ls-tree", "-r", "--name-only", "main")


def test_the_model_decides_and_a_note_it_drops_stays_off_the_page(lab: Lab) -> None:
    notes = three_notes(lab)
    lab.fake.script["uv needs --cache-dir"] = {"useful": "no"}

    assert lab.kb("ingest").exit_code == 0

    assert "uv needs --cache-dir" not in lab.show(BRANCH, PAGE)
    outcomes = lab.manifest()["outcomes"]
    assert isinstance(outcomes, dict)
    assert outcomes[notes[0]]["action"] == "skip"
    assert outcomes[notes[1]]["action"] == "add"
    assert outcomes[notes[1]]["answers"]["useful"] == "yes"


def test_two_fresh_notes_wait_for_a_third(lab: Lab) -> None:
    lab.leave("gotcha", "uv needs --cache-dir in the sandbox")
    lab.leave("fix", "the DB fixture needs a socket; run -m 'not db'")

    result = lab.kb("ingest")

    assert result.exit_code == 0
    assert lab.fake.calls == []
    assert not lab.wiki.exists()


def test_a_note_seven_days_old_gets_a_run_by_itself(lab: Lab) -> None:
    lab.leave("gotcha", "uv needs --cache-dir in the sandbox")
    lab.kb.later(days=7)

    assert lab.kb("ingest").exit_code == 0

    assert "uv needs --cache-dir in the sandbox\n" in lab.show("ingest/2026-11-07", PAGE)


def test_status_notes_stay_out(lab: Lab) -> None:
    three_notes(lab)
    lab.leave("status", "integration tests fail on main")

    assert lab.kb("ingest").exit_code == 0

    assert "integration tests fail on main" not in lab.show(BRANCH, PAGE)
    assert len(lab.fake.calls) == 3


def test_a_project_not_on_the_allow_list_stays_local(lab: Lab) -> None:
    three_notes(lab)
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text('[publish]\nallow = ["github.com/me/other"]\n')

    assert lab.kb("ingest").exit_code == 0

    assert lab.fake.calls == []
    assert not lab.wiki.exists()


def test_with_no_project_allowed_it_says_where_to_allow_one(lab: Lab) -> None:
    three_notes(lab)
    config_file({"HOME": str(lab.kb.home), **lab.kb.env}).write_text("")

    for args in ((), ("--dry-run",)):
        result = lab.kb("ingest", *args)

        assert result.exit_code == 0
        assert "no project is allowed" in result.stdout
        assert "[publish] allow in ~/.config/agent-fabric/config.toml" in result.stdout
    assert lab.fake.calls == []


def test_with_no_note_to_take_it_says_so(lab: Lab) -> None:
    lab.leave("status", "integration tests fail on main")

    for args in ((), ("--dry-run",)):
        result = lab.kb("ingest", *args)

        assert result.exit_code == 0
        assert result.stdout == "kb ingest: no new gotcha, fix or decision note in an allowed project\n"
    assert lab.fake.calls == []


def test_with_no_note_to_take_in_the_project_asked_for_it_names_that_project(lab: Lab) -> None:
    lab.leave("status", "integration tests fail on main")

    result = lab.kb("ingest", "--project", PROJECT)

    assert result.stdout == f"kb ingest: no new gotcha, fix or decision note in {PROJECT}\n"


def test_a_project_asked_for_but_not_allowed_is_named(lab: Lab) -> None:
    three_notes(lab)

    result = lab.kb("ingest", "--project", "github.com/me/other")

    assert result.exit_code == 0
    assert "github.com/me/other isn't on [publish] allow in ~/.config/agent-fabric/config.toml" in result.stdout
    assert lab.fake.calls == []


def test_a_later_run_appends_to_the_unmerged_branch_and_skips_its_notes(lab: Lab) -> None:
    notes = three_notes(lab)
    assert lab.kb("ingest").exit_code == 0
    asked = len(lab.fake.calls)
    lab.kb.later(days=1)
    extra = [lab.leave("gotcha", f"new trap number {n}") for n in range(3)]

    assert lab.kb("ingest").exit_code == 0

    assert len(lab.fake.calls) == asked + 3
    assert "refs/heads/ingest/2026-11-01" not in lab.wiki_git("for-each-ref")
    page = lab.show(BRANCH, PAGE)
    assert "uv needs --cache-dir in the sandbox\n" in page
    assert "new trap number 2\n" in page
    assert set(lab.manifest()["outcomes"]) == {*notes, *extra}  # type: ignore[call-overload]


def test_notes_on_the_unmerged_branch_do_not_count_towards_the_gate(lab: Lab) -> None:
    three_notes(lab)
    assert lab.kb("ingest").exit_code == 0
    asked = len(lab.fake.calls)

    result = lab.kb("ingest")

    assert result.exit_code == 0
    assert len(lab.fake.calls) == asked
    assert "no new gotcha, fix or decision note" in result.stdout
    assert f"git -C ~/wiki merge {BRANCH}" in result.stdout


def test_after_the_merge_a_filed_note_moves_to_done_and_the_next_run_starts_a_new_branch(lab: Lab) -> None:
    notes = three_notes(lab)
    assert lab.kb("ingest").exit_code == 0
    lab.merge()
    lab.kb.later(days=1)

    assert lab.kb("ingest").exit_code == 0

    assert list(lab.inbox.glob("*.md")) == []
    done = {path.stem.rpartition("-")[2] for path in (lab.inbox / "done").rglob("*.md")}
    assert done == set(notes)
    extra = [lab.leave("gotcha", f"new trap number {n}") for n in range(3)]
    assert lab.kb("ingest").exit_code == 0
    assert set(lab.manifest("ingest/2026-11-01", "2026-11-01")["outcomes"]) == set(extra)  # type: ignore[call-overload]


def test_a_run_on_the_day_of_a_merged_branch_starts_over_from_main(lab: Lab) -> None:
    three_notes(lab)
    assert lab.kb("ingest").exit_code == 0
    lab.merge()
    extra = [lab.leave("gotcha", f"new trap number {n}") for n in range(3)]

    assert lab.kb("ingest").exit_code == 0

    assert set(lab.manifest("ingest/2026-10-31-2", "2026-10-31-2")["outcomes"]) == set(extra)  # type: ignore[call-overload]
    assert "uv needs --cache-dir in the sandbox\n" in lab.show("ingest/2026-10-31-2", PAGE)
    assert lab.manifest("main")["outcomes"] != {}  # the merged manifest stays as it was


def test_a_deleted_unmerged_branch_returns_its_notes_to_pending(lab: Lab) -> None:
    notes = three_notes(lab)
    assert lab.kb("ingest").exit_code == 0
    lab.wiki_git("branch", "-D", BRANCH)
    lab.kb.later(days=1)

    assert lab.kb("ingest").exit_code == 0

    assert set(lab.manifest("ingest/2026-11-01", "2026-11-01")["outcomes"]) == set(notes)  # type: ignore[call-overload]


def test_a_run_that_dies_commits_no_outcomes_and_the_next_run_takes_its_notes_again(lab: Lab) -> None:
    notes = three_notes(lab)
    lab.fake.script["the wiki stays local"] = BackendError("login expired")

    result = lab.kb("ingest")

    assert result.exit_code == 1
    assert "login expired" in result.stderr
    assert PAGE not in lab.wiki_git("ls-tree", "-r", "--name-only", BRANCH)
    assert lab.manifest()["notes"] == notes
    assert lab.manifest()["outcomes"] == {}
    worktree = lab.kb.home / ".local" / "share" / "agent-fabric" / "ingest-wt"
    assert (worktree / PAGE).exists()  # the work it hadn't committed, as a killed process leaves it

    del lab.fake.script["the wiki stays local"]
    assert lab.kb("ingest").exit_code == 0

    assert set(lab.manifest()["outcomes"]) == set(notes)  # type: ignore[call-overload]
    assert lab.show(BRANCH, PAGE).count("### uv needs --cache-dir in the sandbox\n") == 1


@pytest.mark.parametrize(
    "bad",
    [{"useful": "maybe"}, {"useful": 3}],
    ids=["an-option-not-offered", "not-a-string"],
)
def test_an_answer_that_is_invalid_twice_reaches_no_page_and_is_listed_as_failed(
    lab: Lab, bad: dict[str, object]
) -> None:
    notes = three_notes(lab)
    lab.fake.script["uv needs --cache-dir"] = bad

    assert lab.kb("ingest").exit_code == 0

    assert "uv needs --cache-dir" not in lab.show(BRANCH, PAGE)
    assert lab.manifest()["outcomes"][notes[0]] == {"failed": "no valid answer"}  # type: ignore[index]
    asked_about_it = [state for state, _ in lab.fake.calls if "uv needs --cache-dir" in state]
    assert len(asked_about_it) == 2


class Flaky:
    """Invalid once for the note it names, then fine."""

    def __init__(self, inner: FakeBackend, fragment: str, invalid: object) -> None:
        self.inner, self.fragment, self.invalid, self.tripped = inner, fragment, invalid, False

    def answer(self, state: str, questions: list[Question]) -> object:
        if self.fragment in state and not self.tripped:
            self.tripped = True
            return self.invalid
        return self.inner.answer(state, questions)


@pytest.mark.parametrize(
    "invalid",
    ["not an object", {"useful": "yes"}, {"useful": "yes", "sure": "high", "extra": "yes"}],
    ids=["not-an-object", "a-question-unanswered", "an-answer-to-no-question"],
)
def test_an_invalid_answer_is_retried_once(lab: Lab, invalid: object) -> None:
    three_notes(lab)
    lab.kb.backend = Flaky(lab.fake, "uv needs --cache-dir", invalid)

    assert lab.kb("ingest").exit_code == 0

    assert "### uv needs --cache-dir in the sandbox\n" in lab.show(BRANCH, PAGE)


def test_a_backend_that_returns_junk_counts_as_an_invalid_answer(lab: Lab) -> None:
    notes = three_notes(lab)
    lab.fake.script["uv needs --cache-dir"] = InvalidAnswer("not JSON")

    assert lab.kb("ingest").exit_code == 0

    assert lab.manifest()["outcomes"][notes[0]] == {"failed": "no valid answer"}  # type: ignore[index]


def test_a_failed_note_is_not_retried_on_its_own_branch(lab: Lab) -> None:
    notes = three_notes(lab)
    lab.fake.script["uv needs --cache-dir"] = {"useful": "maybe"}
    assert lab.kb("ingest").exit_code == 0
    asked = len(lab.fake.calls)
    lab.kb.later(days=30)
    extra = [lab.leave("gotcha", f"new trap number {n}") for n in range(3)]

    assert lab.kb("ingest").exit_code == 0

    assert len(lab.fake.calls) == asked + 3
    assert lab.manifest()["outcomes"][notes[0]] == {"failed": "no valid answer"}  # type: ignore[index]
    assert set(extra) <= set(lab.manifest()["outcomes"])  # type: ignore[call-overload]


def test_a_failed_note_never_triggers_a_run_by_itself(lab: Lab) -> None:
    three_notes(lab)
    lab.fake.script["uv needs --cache-dir"] = {"useful": "maybe"}
    assert lab.kb("ingest").exit_code == 0
    lab.merge()
    asked = len(lab.fake.calls)
    lab.kb.later(days=30)

    assert lab.kb("ingest").exit_code == 0

    assert len(lab.fake.calls) == asked
    assert [path.name for path in lab.inbox.glob("*.md")] != []  # it stays in the inbox until it's resolved


def test_a_failed_note_stays_in_the_inbox_and_is_tried_once_on_the_next_branch(lab: Lab) -> None:
    notes = three_notes(lab)
    lab.fake.script["uv needs --cache-dir"] = {"useful": "maybe"}
    assert lab.kb("ingest").exit_code == 0
    lab.merge()
    lab.kb.later(days=1)
    del lab.fake.script["uv needs --cache-dir"]
    [lab.leave("gotcha", f"new trap number {n}") for n in range(3)]

    assert lab.kb("ingest").exit_code == 0

    manifest = lab.manifest("ingest/2026-11-01", "2026-11-01")
    assert manifest["outcomes"][notes[0]]["action"] == "add"  # type: ignore[index]
    assert "### uv needs --cache-dir in the sandbox\n" in lab.show("ingest/2026-11-01", PAGE)


def seeded(lab: Lab) -> list[str]:
    """Three claims merged to main."""
    notes = three_notes(lab)
    assert lab.kb("ingest").exit_code == 0
    lab.merge()
    return notes


def run_a_week_later(lab: Lab) -> None:
    """A note this old gets a run by itself."""
    lab.kb.later(days=8)
    result = lab.kb("ingest")
    assert result.exit_code == 0, result.stderr


def claim_lines(page: str, note_id: str) -> list[str]:
    """The exact lines of the claim whose source is this note, from its title to its last line."""
    blocks = page.split("\n\n")
    found = [block for block in blocks if f"- source: {note_id} " in block]
    assert len(found) == 1, page
    return found[0].splitlines()


def section_of(page: str, note_id: str) -> str:
    before = page[: page.index(f"- source: {note_id} ")]
    return before[before.rindex("\n## ") + 4 :].split("\n", 1)[0]


NEW = "uv needs --cache-dir in the sandbox, and UV_NO_SYNC too"


def asked(lab: Lab, fragment: str) -> tuple[str, list[Question]]:
    return next(call for call in lab.fake.calls if fragment in call[0])


def test_the_model_is_asked_about_claims_that_share_a_notes_words_and_no_others(lab: Lab) -> None:
    notes = seeded(lab)
    lab.leave("gotcha", NEW)

    run_a_week_later(lab)

    state, questions = asked(lab, NEW)
    ids = {question.id for question in questions}
    assert f"claim-{notes[0]}" in ids
    assert f"claim-{notes[1]}" not in ids
    assert "uv needs --cache-dir in the sandbox\n" in state


def test_a_note_replacing_a_claim_moves_it_unchanged_and_points_to_what_replaced_it(lab: Lab) -> None:
    notes = seeded(lab)
    old = claim_lines(lab.show("main", PAGE), notes[0])
    lab.fake.script[NEW] = {"claim": "replaces"}
    new = lab.leave("gotcha", NEW)

    run_a_week_later(lab)

    page = lab.show("ingest/2026-11-08", PAGE)
    moved = claim_lines(page, notes[0])
    assert moved[: len(old)] == old
    assert moved[len(old)] == f"- superseded by: {new} (2026-11-08)"
    assert section_of(page, notes[0]) == "Superseded"
    assert section_of(page, new) == "Gotchas"
    assert claim_lines(page, new)[-1].endswith(f"supersedes: {notes[0]}")


def test_a_note_adding_to_a_claim_is_filed_right_below_it(lab: Lab) -> None:
    notes = seeded(lab)
    old = claim_lines(lab.show("main", PAGE), notes[0])
    lab.fake.script[NEW] = {"claim": "adds"}
    new = lab.leave("fix", NEW)

    run_a_week_later(lab)

    page = lab.show("ingest/2026-11-08", PAGE)
    assert claim_lines(page, notes[0]) == old
    assert section_of(page, new) == "Gotchas"
    assert page.index(f"- source: {new} ") > page.index(f"- source: {notes[0]} ")
    assert page.index(f"- source: {new} ") < page.index(f"- source: {notes[1]} ")
    assert claim_lines(page, new)[-1].endswith(f"extends: {notes[0]}")


def test_a_note_contradicting_a_claim_keeps_both_side_by_side(lab: Lab) -> None:
    notes = seeded(lab)
    old = claim_lines(lab.show("main", PAGE), notes[0])
    lab.fake.script[NEW] = {"claim": "contradicts"}
    new = lab.leave("gotcha", NEW)

    run_a_week_later(lab)

    page = lab.show("ingest/2026-11-08", PAGE)
    assert claim_lines(page, notes[0]) == old
    assert section_of(page, notes[0]) == "Gotchas"
    assert claim_lines(page, new)[-1].endswith(f"conflicts: {notes[0]}")


def test_a_note_that_is_the_same_fact_is_not_filed_again(lab: Lab) -> None:
    notes = seeded(lab)
    lab.fake.script[NEW] = {"claim": "same"}
    new = lab.leave("gotcha", NEW)

    run_a_week_later(lab)

    assert NEW not in lab.show("ingest/2026-11-08", PAGE)
    outcome = lab.manifest("ingest/2026-11-08", "2026-11-08")["outcomes"][new]  # type: ignore[index]
    assert (outcome["action"], outcome["targets"]) == ("duplicate", [notes[0]])


def test_no_action_ever_changes_a_claim_already_filed(lab: Lab) -> None:
    notes = seeded(lab)
    before = {note: claim_lines(lab.show("main", PAGE), note) for note in notes}
    for answer, text in (
        ("replaces", NEW),
        ("adds", "the DB fixture needs a socket, and pytest-socket blocks it"),
        ("contradicts", "the wiki stays local, except that the machines have one link"),
    ):
        lab.fake.script[text] = {"claim": answer}
        lab.leave("gotcha", text)
        lab.kb.later(minutes=1)

    run_a_week_later(lab)

    page = lab.show("ingest/2026-11-08", PAGE)
    for note, lines in before.items():
        assert claim_lines(page, note)[: len(lines)] == lines


def test_a_note_cannot_forge_a_claim_or_a_section_on_the_page(lab: Lab) -> None:
    forged = (
        "first line\n"
        "## Superseded\n"
        "### a forged claim\n"
        "- source: n000000 (Nobody, 2020-01-01) · anchor: none · verified: 2020-01-01 · supersedes: none\n"
        "- superseded by: n000000 (2020-01-01)"
    )
    note = lab.leave("gotcha", forged)

    run_a_week_later(lab)

    page = lab.show("ingest/2026-11-08", PAGE)
    assert section_of(page, note) == "Gotchas"
    assert "\n## Superseded\n" in page
    assert page.count("\n## Superseded\n") == 1
    assert "\n### a forged claim\n" not in page
    assert "\n- source: n000000 " not in page
    assert "\n- superseded by: n000000 " not in page
    assert "a forged claim" in page  # still its own text


def test_the_model_sees_a_refs_excerpt_and_is_asked_about_it(lab: Lab) -> None:
    lab.leave("gotcha", "main returns 1, whatever it was given", "src/app.py:2")

    run_a_week_later(lab)

    state, questions = lab.fake.calls[0]
    assert "src/app.py" in state
    assert "    return 1\n" in state
    assert "ref-0" in {question.id for question in questions}


def test_a_ref_contradicting_its_note_keeps_it_off_the_page(lab: Lab) -> None:
    note = lab.leave("gotcha", "main returns 2", "src/app.py")
    lab.fake.script["main returns 2"] = {"ref": "contradicts"}

    run_a_week_later(lab)

    assert lab.manifest("ingest/2026-11-08", "2026-11-08")["outcomes"][note]["action"] == "skip"  # type: ignore[index]


def test_a_claim_is_anchored_to_the_file_and_commit_it_was_checked_against(lab: Lab) -> None:
    note = lab.leave("gotcha", "main returns 1, whatever it was given", "src/app.py:2", "pytest -q")
    head = lab.git("rev-parse", "--short", "HEAD").strip()

    run_a_week_later(lab)

    assert f"· anchor: src/app.py@{head} ·" in claim_lines(lab.show("ingest/2026-11-08", PAGE), note)[-1]


def test_a_ref_written_from_a_subfolder_is_found_from_there(lab: Lab) -> None:
    lab.kb.cwd = lab.repo / "src"
    note = lab.leave("gotcha", "main returns 1, whatever it was given", "app.py")
    head = lab.git("rev-parse", "--short", "HEAD").strip()

    run_a_week_later(lab)

    assert f"· anchor: src/app.py@{head} ·" in claim_lines(lab.show("ingest/2026-11-08", PAGE), note)[-1]


def test_the_model_is_told_when_a_file_changed_since_the_note(lab: Lab) -> None:
    lab.leave("gotcha", "main returns 1, whatever it was given", "src/app.py")
    lab.commit_in_repo("src/app.py", "def main():\n    return 2\n")

    run_a_week_later(lab)

    state, _ = lab.fake.calls[0]
    assert "changed since the note: yes" in state
    assert "    return 2\n" in state


def test_a_slashed_ref_that_is_not_a_path_in_the_repo_is_not_a_dead_ref(lab: Lab) -> None:
    note = lab.leave("gotcha", "rebasing on origin/main drops the lock file", "origin/main")

    run_a_week_later(lab)

    outcome = lab.manifest("ingest/2026-11-08", "2026-11-08")["outcomes"][note]  # type: ignore[index]
    assert outcome["action"] == "add"


def test_a_refs_commands_and_urls_get_no_question(lab: Lab) -> None:
    lab.leave("gotcha", "the suite hangs on a socket", "pytest -q", "https://example.test/issue/1")

    run_a_week_later(lab)

    assert not [question for question in lab.fake.calls[0][1] if question.kind == "ref"]


@pytest.mark.parametrize(
    ("ref", "where", "secret"),
    [
        ("~/.ssh/id_rsa", ".ssh/id_rsa", "a private key"),
        ("../secrets/token.txt", "code/secrets/token.txt", "a token"),
    ],
    ids=["outside-the-repo-by-home", "outside-the-repo-by-dots"],
)
def test_a_ref_outside_the_repo_is_never_read(lab: Lab, ref: str, where: str, secret: str) -> None:
    target = lab.kb.home / where
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{secret}\n")
    lab.leave("gotcha", "the deploy reads this file before it starts", ref)

    run_a_week_later(lab)

    assert secret not in lab.fake.calls[0][0]


def test_a_ref_by_absolute_path_is_never_read(lab: Lab) -> None:
    target = lab.kb.home / "private.txt"
    target.write_text("a private value\n")
    lab.leave("gotcha", "the deploy reads this file before it starts", str(target))

    run_a_week_later(lab)

    assert "a private value" not in lab.fake.calls[0][0]


def test_an_untracked_file_is_never_read(lab: Lab) -> None:
    (lab.repo / "local.txt").write_text("something uncommitted\n")
    lab.leave("gotcha", "the deploy reads this file before it starts", "local.txt")

    run_a_week_later(lab)

    assert "something uncommitted" not in lab.fake.calls[0][0]


def test_a_tracked_env_file_is_never_read(lab: Lab) -> None:
    lab.commit_in_repo("config/.env.local", "VALUE=something private\n")
    lab.leave("gotcha", "the deploy reads this file before it starts", "config/.env.local")

    run_a_week_later(lab)

    assert "something private" not in lab.fake.calls[0][0]


def test_a_tracked_env_file_is_never_read_through_a_symlink(lab: Lab) -> None:
    lab.commit_in_repo("config/.env.local", "VALUE=something private\n")
    (lab.repo / "link.txt").symlink_to("config/.env.local")
    lab.git("add", "-A")
    lab.git("commit", "-q", "-m", "a link")
    lab.leave("gotcha", "the deploy reads this file before it starts", str(lab.repo / "link.txt"))

    run_a_week_later(lab)

    assert "something private" not in lab.fake.calls[0][0]


def test_a_note_naming_another_projects_folder_reads_nothing_from_it(lab: Lab) -> None:
    other = lab.kb.home / "code" / "other"
    other.mkdir(parents=True)
    lab.git("init", "-q", "-b", "main", cwd=other)
    (other / "private.py").write_text("something private\n")
    lab.git("add", "-A", cwd=other)
    lab.git("commit", "-q", "-m", "start", cwd=other)
    note = Note(
        id="n00aa11",
        kind="gotcha",
        agent={"name": "Codex"},
        project=PROJECT,
        cwd="~/code/other",
        branch=None,
        commit=None,
        time=lab.kb.now,
        refs=["private.py"],
        resolves=None,
        text="the deploy reads this file before it starts",
    )
    Inbox(lab.inbox).add(note)

    run_a_week_later(lab)

    assert "something private" not in lab.fake.calls[0][0]


def test_a_note_whose_checkout_has_gone_is_still_decided(lab: Lab) -> None:
    note = lab.leave("gotcha", "main returns 1, whatever it was given", "src/app.py")
    shutil.rmtree(lab.repo)
    lab.kb.cwd = lab.kb.home

    run_a_week_later(lab)

    claim = claim_lines(lab.show("ingest/2026-11-08", PAGE), note)
    assert "· anchor: none ·" in claim[-1]


def test_a_note_repeating_the_projects_agents_file_is_a_duplicate(lab: Lab) -> None:
    lab.commit_in_repo(
        "AGENTS.md", "# Rules\n\nRun pytest with uv and the cache directory flag.\nDeploys are manual.\n"
    )
    note = lab.leave("gotcha", "pytest needs the cache directory flag under uv")
    lab.fake.script["pytest needs the cache"] = {"doc-0": "yes"}

    run_a_week_later(lab)

    state, questions = lab.fake.calls[0]
    assert "Run pytest with uv and the cache directory flag." in state
    assert "Deploys are manual." not in state
    assert [question.id for question in questions if question.kind == "doc"] == ["doc-0"]
    outcome = lab.manifest("ingest/2026-11-08", "2026-11-08")["outcomes"][note]  # type: ignore[index]
    assert outcome["action"] == "duplicate"


TOKEN = "ghp" + "_" + "Qx7Pm2Vb9Tz4Lk8Rw3Nc6Hy1Jd5Gs0FaAb3D"  # built here so that this file trips no scanner


def test_a_token_is_redacted_before_the_model_and_the_page_see_it(lab: Lab) -> None:
    note = lab.leave("gotcha", f"the deploy fails with the token {TOKEN} in its log")

    run_a_week_later(lab)

    assert TOKEN not in lab.fake.calls[0][0]
    page = lab.show("ingest/2026-11-08", PAGE)
    assert TOKEN not in page
    assert "the deploy fails with the token [redacted] in its log\n" in page
    assert TOKEN not in lab.show("ingest/2026-11-08", raw_note(lab, note))


def test_a_token_in_a_refs_path_is_not_filed_as_its_anchor(lab: Lab) -> None:
    lab.commit_in_repo(f"data/{TOKEN}.txt", "a value\n")
    note = lab.leave("gotcha", "the deploy reads this data file first", f"data/{TOKEN}.txt")

    run_a_week_later(lab)

    page = lab.show("ingest/2026-11-08", PAGE)
    assert TOKEN not in page
    assert TOKEN not in lab.fake.calls[0][0]
    assert TOKEN not in lab.show("ingest/2026-11-08", raw_note(lab, note))


def raw_note(lab: Lab, note: str) -> str:
    return f"raw/notes/{PROJECT}/2026/10/{next(lab.inbox.rglob(f'*-{note}.md')).name}"


def test_a_gitleaks_hit_in_a_note_quarantines_it_before_any_model_sees_it(lab: Lab) -> None:
    note = lab.leave("gotcha", f"this note holds a {LEAK} itself")

    run_a_week_later(lab)

    assert lab.fake.calls == []
    outcome = lab.manifest("ingest/2026-11-08", "2026-11-08")["outcomes"][note]  # type: ignore[index]
    assert outcome == {"failed": "quarantined", "rules": ["fake-rule"]}
    assert PAGE not in lab.wiki_git("ls-tree", "-r", "--name-only", "ingest/2026-11-08")


def test_a_quarantined_note_stays_in_the_inbox_after_the_merge(lab: Lab) -> None:
    note = lab.leave("gotcha", f"this note holds a {LEAK} itself")
    run_a_week_later(lab)

    lab.merge("ingest/2026-11-08")
    lab.kb.later(days=1)
    assert lab.kb("ingest").exit_code == 0

    assert [path.name.endswith(f"-{note}.md") for path in lab.inbox.glob("*.md")] == [True]


def test_a_broken_gitleaks_stops_the_run_before_any_model_is_asked(lab: Lab, tmp_path: Path) -> None:
    three_notes(lab)
    (tmp_path / "stubs" / "gitleaks").write_text("#!/bin/sh\nexit 3\n")

    result = lab.kb("ingest")

    assert result.exit_code == 1
    assert "gitleaks" in result.stderr
    assert lab.fake.calls == []


def test_the_output_gate_stops_a_commit_of_a_page_holding_a_secret(lab: Lab) -> None:
    notes = seeded(lab)
    path = lab.wiki / PAGE
    path.write_text(path.read_text().replace("\n## Gotchas\n", f"\nsomeone pasted a {LEAK} here\n\n## Gotchas\n", 1))
    lab.wiki_git("add", "-A")
    lab.wiki_git("commit", "-q", "-m", "a hand edit")
    lab.leave("gotcha", "a new trap about something else entirely")
    lab.kb.later(days=8)

    result = lab.kb("ingest")

    assert result.exit_code == 1
    assert "gitleaks" in result.stderr
    assert "a new trap" not in lab.show("ingest/2026-11-08", PAGE)
    assert lab.manifest("ingest/2026-11-08", "2026-11-08")["outcomes"] == {}
    assert notes[0] not in lab.manifest("ingest/2026-11-08", "2026-11-08")["notes"]  # type: ignore[operator]


def test_each_note_with_an_action_is_copied_to_raw_notes_and_a_failed_one_is_not(lab: Lab) -> None:
    notes = three_notes(lab)
    lab.fake.script["uv needs --cache-dir"] = {"useful": "maybe"}
    lab.fake.script["the DB fixture"] = {"useful": "no"}

    assert lab.kb("ingest").exit_code == 0

    files = lab.wiki_git("ls-tree", "-r", "--name-only", BRANCH)
    assert f"-{notes[0]}.md" not in files
    assert raw_note(lab, notes[1]) in files
    assert raw_note(lab, notes[2]) in files
    assert "the DB fixture needs a socket" in lab.show(BRANCH, raw_note(lab, notes[1]))


def test_the_index_lists_each_page_and_the_log_gets_a_line_per_note(lab: Lab) -> None:
    notes = three_notes(lab)

    assert lab.kb("ingest").exit_code == 0

    index = lab.show(BRANCH, "index.md")
    assert f"- [{PROJECT}]({PAGE}): 3 claims\n" in index
    log = lab.show(BRANCH, "log.md")
    assert f"2026-10-31 ingest {notes[0]} add {PROJECT}\n" in log
    assert f"2026-10-31 ingest {notes[2]} add {PROJECT}\n" in log


def test_a_quiet_inbox_still_lints_and_says_what_it_found(lab: Lab) -> None:
    lab.leave("status", "integration tests fail on main")
    lab.kb.later(days=15)

    result = lab.kb("ingest")

    assert result.exit_code == 0
    assert "stale-status" in result.stdout
    assert lab.fake.calls == []


def test_the_output_gate_stops_a_commit_that_leaves_a_changed_page_broken(lab: Lab) -> None:
    notes = seeded(lab)
    next((lab.wiki / "raw").rglob(f"*-{notes[1]}.md")).unlink()
    lab.wiki_git("add", "-A")
    lab.wiki_git("commit", "-q", "-m", "a hand edit")
    lab.leave("gotcha", "a new trap about something else entirely")
    lab.kb.later(days=8)

    result = lab.kb("ingest")

    assert result.exit_code == 1
    assert "missing-note" in result.stderr
    assert lab.manifest("ingest/2026-11-08", "2026-11-08")["outcomes"] == {}


def test_a_broken_page_the_run_did_not_change_does_not_stop_it(lab: Lab) -> None:
    seeded(lab)
    other = lab.wiki / "wiki" / "projects" / "github.com" / "me" / "other.md"
    other.write_text("# github.com/me/other\n\n## Gotchas\n\n### a claim with no source\nsome text\n\n")
    lab.wiki_git("add", "-A")
    lab.wiki_git("commit", "-q", "-m", "a hand edit")
    lab.leave("gotcha", "a new trap about something else entirely")

    run_a_week_later(lab)

    assert "a new trap about something else entirely\n" in lab.show("ingest/2026-11-08", PAGE)


def test_the_review_list_shows_each_note_with_its_answers_least_certain_first(lab: Lab) -> None:
    notes = three_notes(lab)
    lab.fake.script["uv needs --cache-dir"] = {"sure": "high", "useful": "no"}
    lab.fake.script["the DB fixture"] = {"sure": "low"}
    lab.fake.script["the wiki stays local"] = {"sure": "medium"}

    assert lab.kb("ingest").exit_code == 0

    review = lab.show(BRANCH, "manifests/2026-10-31.md")
    assert review.index(notes[1]) < review.index(notes[2]) < review.index(notes[0])
    assert f"## {notes[0]} skip (sure: high)\n" in review
    assert "> uv needs --cache-dir in the sandbox\n" in review
    assert "- useful: no\n" in review


def test_a_failed_note_heads_the_review_list_and_its_text_stays_off_it(lab: Lab) -> None:
    notes = three_notes(lab)
    leak = lab.leave("gotcha", f"this note holds a {LEAK} itself")
    lab.fake.script["the DB fixture"] = {"useful": "maybe"}
    lab.fake.script["uv needs --cache-dir"] = {"sure": "low"}

    assert lab.kb("ingest").exit_code == 0

    review = lab.show(BRANCH, "manifests/2026-10-31.md")
    assert f"## {leak} quarantined (fake-rule)\n" in review
    assert f"## {notes[1]} no valid answer\n" in review
    assert review.index(leak) < review.index(notes[0])
    assert review.index(notes[1]) < review.index(notes[0])
    assert LEAK not in review


def test_the_review_list_keeps_notes_of_earlier_runs_on_the_branch(lab: Lab) -> None:
    notes = three_notes(lab)
    assert lab.kb("ingest").exit_code == 0
    extra = [lab.leave("gotcha", f"new trap number {n}") for n in range(3)]

    assert lab.kb("ingest").exit_code == 0

    review = lab.show(BRANCH, "manifests/2026-10-31.md")
    assert all(note in review for note in [*notes, *extra])


def test_the_review_list_shows_what_lint_found(lab: Lab) -> None:
    three_notes(lab)
    lab.leave("status", "integration tests fail on main")
    lab.kb.later(days=15)

    assert lab.kb("ingest").exit_code == 0

    assert "- warning stale-status" in lab.show("ingest/2026-11-15", "manifests/2026-11-15.md")


def test_project_limits_the_run_to_one_project(lab: Lab) -> None:
    three_notes(lab)
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text(f'[publish]\nallow = ["{PROJECT}", "github.com/me/other"]\n')

    assert lab.kb("ingest", "--project", "github.com/me/other").exit_code == 0

    assert lab.fake.calls == []
    assert lab.kb("ingest", "--project", PROJECT).exit_code == 0
    assert len(lab.fake.calls) == 3


def test_project_can_be_a_folder(lab: Lab) -> None:
    three_notes(lab)

    assert lab.kb("ingest", "--project", str(lab.repo)).exit_code == 0

    assert len(lab.fake.calls) == 3


def test_a_dry_run_says_what_it_would_take_and_changes_nothing(lab: Lab) -> None:
    resolved = lab.leave("status", "integration tests fail on main")
    lab.kb("note", "--resolve", resolved, "--agent", "Codex", "fixed")
    notes = three_notes(lab)

    result = lab.kb("ingest", "--dry-run")

    assert result.exit_code == 0
    assert all(note in result.stdout for note in notes)
    assert lab.fake.calls == []
    assert not lab.wiki.exists()
    assert len(list(lab.inbox.glob("*.md"))) == 5


def test_a_second_run_at_the_same_time_stops_and_says_so(lab: Lab) -> None:
    three_notes(lab)
    lock = lab.kb.home / ".local" / "share" / "agent-fabric" / "ingest.lock"
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)

        result = lab.kb("ingest")

    assert result.exit_code == 1
    assert "running" in result.stderr
    assert lab.fake.calls == []


def test_a_config_that_cannot_be_read_stops_the_run_and_says_where(lab: Lab) -> None:
    three_notes(lab)
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text("[publish\n")

    result = lab.kb("ingest")

    assert result.exit_code == 1
    assert "config.toml" in result.stderr


def test_a_folder_at_the_wiki_path_that_holds_other_files_is_left_alone(lab: Lab) -> None:
    three_notes(lab)
    lab.wiki.mkdir()
    (lab.wiki / "thoughts.txt").write_text("mine\n")

    result = lab.kb("ingest")

    assert result.exit_code == 1
    assert "isn't a git repo" in result.stderr
    assert sorted(path.name for path in lab.wiki.iterdir()) == ["thoughts.txt"]


def test_a_new_wiki_holds_the_wording_of_its_questions(lab: Lab) -> None:
    three_notes(lab)

    assert lab.kb("ingest").exit_code == 0

    agents = lab.show("main", "AGENTS.md")
    assert "- useful: Would a future agent plausibly act better for knowing this?\n" in agents
    assert "Contradicting means both can't be true, not that one is newer." in agents


def test_the_wording_the_model_sees_comes_from_the_wikis_agents_file(lab: Lab) -> None:
    seeded(lab)
    agents = lab.wiki / "AGENTS.md"
    agents.write_text(agents.read_text().replace("Would a future agent plausibly", "Would the next agent really"))
    lab.wiki_git("commit", "-qam", "reword")
    lab.leave("gotcha", "a new trap about something else entirely")

    run_a_week_later(lab)

    useful = next(question for question in lab.fake.calls[-1][1] if question.id == "useful")
    assert useful.text == "Would the next agent really act better for knowing this?"


def test_a_question_missing_from_the_agents_file_keeps_its_own_wording(lab: Lab) -> None:
    seeded(lab)
    (lab.wiki / "AGENTS.md").write_text("# Mine\n")
    lab.wiki_git("commit", "-qam", "my own")
    lab.leave("gotcha", "a new trap about something else entirely")

    run_a_week_later(lab)

    useful = next(question for question in lab.fake.calls[-1][1] if question.id == "useful")
    assert useful.text == "Would a future agent plausibly act better for knowing this?"


def test_notes_moved_to_done_still_show_in_inbox_all(lab: Lab) -> None:
    notes = seeded(lab)
    lab.kb.later(days=1)
    assert lab.kb("ingest").exit_code == 0
    assert list(lab.inbox.glob("*.md")) == []

    assert notes[0] not in lab.kb("inbox").stdout
    assert all(note in lab.kb("inbox", "--all").stdout for note in notes)


def test_a_run_says_what_it_did_and_what_to_do_next(lab: Lab) -> None:
    notes = three_notes(lab)
    lab.fake.script["uv needs --cache-dir"] = {"useful": "no"}
    lab.fake.script["the DB fixture"] = {"useful": "maybe"}

    result = lab.kb("ingest")

    assert "3 notes on ingest/2026-10-31: 1 add, 1 skip, 1 failed\n" in result.stdout
    assert "manifests/2026-10-31.md" in result.stdout
    assert f"git -C ~/wiki merge {BRANCH}" in result.stdout
    assert all(note not in result.stdout for note in notes)


def test_a_token_in_a_refs_file_is_redacted_before_the_model_sees_it(lab: Lab) -> None:
    lab.commit_in_repo("deploy.log", f"failed with {TOKEN}\n")
    lab.leave("gotcha", "the deploy log holds the reason it failed", "deploy.log")

    run_a_week_later(lab)

    assert "failed with [redacted]\n" in lab.fake.calls[0][0]
    assert TOKEN not in lab.fake.calls[0][0]


def test_a_run_with_nothing_open_succeeds_and_creates_no_wiki(kb: Kb) -> None:
    result = kb("ingest")

    assert result.exit_code == 0
    assert not (kb.home / "wiki").exists()


def test_a_resolved_note_moves_to_done_with_its_resolution(kb: Kb) -> None:
    note = kb("note", "--kind", "status", "--agent", "Codex", "integration tests fail on main").stdout.split()[0]
    kb.later(hours=2)
    kb("note", "--resolve", note, "--agent", "Codex", "fixed by the pool change")

    assert kb("ingest").exit_code == 0

    inbox = kb.home / ".local" / "share" / "agent-fabric" / "inbox"
    assert list(inbox.glob("*.md")) == []
    assert len(list((inbox / "done" / "2026" / "10").glob("*.md"))) == 2


def key_file() -> str:
    """A private key as a file holds it, long enough that an excerpt can't hold all of it."""
    body = "\n".join(f"{n:02d}Qx7Pm2Vb9Tz4Lk8Rw3Nc6Hy1Jd5Gs0FaQx7Pm2Vb9Tz4Lk8Rw3Nc6Hy1Jd5Gs0Fa" for n in range(40))
    return "-----BEGIN RSA " + f"PRIVATE KEY-----\n{body}\n-----END RSA " + "PRIVATE KEY-----\n"


@pytest.mark.parametrize(
    "ref",
    ["deploy/client.pem", "deploy/client.pem:20", "deploy/client.pem:41"],
    ids=["from-the-top", "from-the-middle", "from-the-end"],
)
def test_no_line_of_a_tracked_private_key_reaches_the_model_whatever_the_excerpt_cuts(lab: Lab, ref: str) -> None:
    lab.commit_in_repo("deploy/client.pem", f"# the client key\n{key_file()}")
    lab.leave("gotcha", "the client key loads before the socket", ref)

    run_a_week_later(lab)

    state = lab.fake.calls[0][0]
    assert "Qx7Pm2Vb9" not in state
    assert "BEGIN RSA" not in state


def test_an_excerpt_shows_the_lines_a_ref_points_at_even_below_a_private_key(lab: Lab) -> None:
    lab.commit_in_repo("deploy/client.pem", f"{key_file()}\n# the line it means\n")
    line = key_file().count("\n") + 2
    lab.leave("gotcha", "the client key loads before the socket", f"deploy/client.pem:{line}")

    run_a_week_later(lab)

    assert "# the line it means" in lab.fake.calls[0][0]


def test_a_file_gitleaks_flags_outside_the_excerpt_sends_no_excerpt_and_the_note_is_still_decided(lab: Lab) -> None:
    filler = "".join(f"line {n}\n" for n in range(200))
    lab.commit_in_repo("data.txt", f"the first line\n{filler}a {LEAK} far below\n")
    note = lab.leave("gotcha", "the data file starts with a header", "data.txt")

    run_a_week_later(lab)

    state, questions = lab.fake.calls[0]
    assert "the first line" not in state
    assert not [question for question in questions if question.kind == "ref"]
    assert lab.manifest("ingest/2026-11-08", "2026-11-08")["outcomes"][note]["action"] == "add"  # type: ignore[index]


def test_a_very_large_file_sends_no_excerpt(lab: Lab) -> None:
    lab.commit_in_repo("data.txt", "the first line\n" + "x" * 1_100_000 + "\n")
    lab.leave("gotcha", "the data file starts with a header", "data.txt")

    run_a_week_later(lab)

    assert "the first line" not in lab.fake.calls[0][0]


def test_a_path_that_only_contains_home_is_filed_as_written(lab: Lab) -> None:
    lab.commit_in_repo("src/pages/home/Hero.tsx", "export const Hero = 1\n")
    head = lab.git("rev-parse", "--short", "HEAD").strip()
    note = lab.leave("gotcha", "the Hero in src/pages/home/Hero.tsx renders twice", "src/pages/home/Hero.tsx")

    run_a_week_later(lab)

    claim = claim_lines(lab.show("ingest/2026-11-08", PAGE), note)
    assert claim[1] == "the Hero in src/pages/home/Hero.tsx renders twice"
    assert f"· anchor: src/pages/home/Hero.tsx@{head} ·" in claim[-1]


def outcome_of(lab: Lab, note: str) -> dict[str, object]:
    found: dict[str, object] = lab.manifest("ingest/2026-11-08", "2026-11-08")["outcomes"][note]  # type: ignore[index]
    return found


REVIEW = "manifests/2026-11-08.md"


@pytest.mark.parametrize(
    ("cwd", "ref"),
    [("", "src/app.py#L2"), ("", "src/app.py:main"), ("", "README.md#setup"), ("src", "app.py#L2")],
    ids=["a-github-line", "a-symbol", "a-heading", "a-github-line-from-a-subfolder"],
)
def test_a_ref_written_with_a_line_or_symbol_after_the_file_finds_the_file(lab: Lab, cwd: str, ref: str) -> None:
    lab.commit_in_repo("README.md", "# Setup\n")
    lab.kb.cwd = lab.repo / cwd
    note = lab.leave("gotcha", "main returns 1, whatever it was given", ref)

    run_a_week_later(lab)

    assert outcome_of(lab, note)["action"] == "add"
    assert "· anchor: none ·" not in claim_lines(lab.show("ingest/2026-11-08", PAGE), note)[-1]
    assert not outcome_of(lab, note).get("missing")


def test_a_github_style_line_ref_shows_the_lines_it_points_at(lab: Lab) -> None:
    body = "".join(f"line {n}\n" for n in range(1, 101))
    lab.commit_in_repo("src/long.py", body)
    lab.leave("gotcha", "the long file has a trap on line 80", "src/long.py#L80")

    run_a_week_later(lab)

    assert "line 80\n" in lab.fake.calls[0][0]
    assert "line 1\n" not in lab.fake.calls[0][0]


@pytest.mark.parametrize("cwd", ["", "src"], ids=["at-the-root", "in-a-subfolder"])
def test_an_absolute_ref_to_a_file_in_the_repo_is_read_like_a_relative_one(lab: Lab, cwd: str) -> None:
    lab.kb.cwd = lab.repo / cwd
    head = lab.git("rev-parse", "--short", "HEAD").strip()
    note = lab.leave("gotcha", "main returns 1, whatever it was given", str(lab.repo / "src" / "app.py"))

    run_a_week_later(lab)

    assert "    return 1\n" in lab.fake.calls[0][0]
    assert f"· anchor: src/app.py@{head} ·" in claim_lines(lab.show("ingest/2026-11-08", PAGE), note)[-1]


@pytest.mark.parametrize(
    ("setup", "ref"),
    [("ignored", "ignored.log"), ("branch", "src/later.py"), ("never", "src/loader.py")],
    ids=["a-gitignored-file", "a-file-only-on-another-branch", "a-file-that-never-existed"],
)
def test_a_ref_not_found_in_git_is_filed_with_no_anchor_and_marked_for_review(lab: Lab, setup: str, ref: str) -> None:
    if setup == "ignored":
        lab.commit_in_repo(".gitignore", "*.log\n")
        (lab.repo / ref).write_text("the reason\n")
    elif setup == "branch":
        lab.git("switch", "-q", "-c", "feat")
        lab.commit_in_repo(ref, "later = 1\n")
        lab.git("switch", "-q", "main")
    note = lab.leave("gotcha", "the file holds the reason it failed", ref)

    run_a_week_later(lab)

    assert outcome_of(lab, note)["action"] == "add"
    claim = claim_lines(lab.show("ingest/2026-11-08", PAGE), note)
    assert claim[1] == "the file holds the reason it failed"
    assert "· anchor: none ·" in claim[-1]
    assert f"- ref not found: {ref}\n" in lab.show("ingest/2026-11-08", REVIEW)


def test_the_model_is_told_a_missing_ref_may_only_be_untracked(lab: Lab) -> None:
    lab.leave("gotcha", "the deploy log holds the reason it failed", "deploy.log")

    run_a_week_later(lab)

    state = lab.fake.calls[0][0]
    assert "Ref 0: deploy.log\nNot found in git" in state
    assert "untracked" in state


def test_a_slashed_ref_from_a_subfolder_that_is_not_a_path_is_not_marked(lab: Lab) -> None:
    lab.kb.cwd = lab.repo / "src"
    note = lab.leave("gotcha", "rebasing on origin/main drops the lock file", "origin/main")

    run_a_week_later(lab)

    assert "Not found in git" not in lab.fake.calls[0][0]
    assert not outcome_of(lab, note).get("missing")
    assert "ref not found" not in lab.show("ingest/2026-11-08", REVIEW)


def test_a_file_gitleaks_flags_is_named_in_the_review_list(lab: Lab) -> None:
    lab.commit_in_repo("data.txt", f"a {LEAK} here\n")
    lab.leave("gotcha", "the data file starts with a header", "data.txt")

    run_a_week_later(lab)

    assert "- excerpt withheld, gitleaks flagged it: data.txt\n" in lab.show("ingest/2026-11-08", REVIEW)


def test_a_secret_in_a_ref_leaves_the_raw_copy_readable(lab: Lab) -> None:
    note = lab.leave("gotcha", "main returns 1, whatever it was given", "src/app.py", "DEPLOY_TOKEN=abc123")

    run_a_week_later(lab)

    raw = parse(lab.show("ingest/2026-11-08", raw_note(lab, note)))
    assert raw.refs == ["src/app.py", "DEPLOY_TOKEN=[redacted]"]


def test_a_secret_in_a_ref_leaves_its_claims_anchor_checkable(lab: Lab) -> None:
    lab.leave("gotcha", "main returns 1, whatever it was given", "src/app.py", "DEPLOY_TOKEN=abc123")
    run_a_week_later(lab)
    lab.merge("ingest/2026-11-08")
    lab.commit_in_repo("src/app.py", "def main():\n    return 2\n")

    assert "stale-anchor" in lab.kb("lint").stdout


def test_a_branch_folder_or_agent_holding_a_secret_is_redacted_in_the_raw_copy(lab: Lab) -> None:
    secret = "abc123secretvalue"
    Inbox(lab.inbox).add(
        Note(
            id="n00aa11",
            kind="gotcha",
            agent={"name": "someone@example.test"},
            project=PROJECT,
            cwd=f"~/code/token={secret}",
            branch=f"token={secret}",
            commit=f"token={secret}",
            time=lab.kb.now,
            refs=[],
            resolves=None,
            text="the deploy reads this file before it starts",
        )
    )

    run_a_week_later(lab)

    raw = lab.show("ingest/2026-11-08", raw_note(lab, "n00aa11"))
    page = lab.show("ingest/2026-11-08", PAGE)
    assert secret not in raw
    assert "someone@example.test" not in raw
    assert "someone@example.test" not in page
    assert parse(raw).id == "n00aa11"


HOME_PROJECT = "/home/alice/code/proj"


def home_project(lab: Lab) -> str:
    """A repo whose id holds a home path: its origin is a folder on this machine."""
    lab.git("remote", "set-url", "origin", f"{HOME_PROJECT}.git")
    config = lab.kb.home / ".config" / "agent-fabric" / "config.toml"
    config.write_text(f'[publish]\nallow = ["{HOME_PROJECT}"]\n')
    note = lab.leave("gotcha", "main returns 1, whatever it was given", "src/app.py")
    assert parse(next(lab.inbox.glob(f"*-{note}.md")).read_text()).scope == HOME_PROJECT
    return note


def test_a_project_id_holding_a_home_path_keeps_it_off_the_wiki(lab: Lab) -> None:
    home_project(lab)

    run_a_week_later(lab)

    files = lab.wiki_git("ls-tree", "-r", "--name-only", "ingest/2026-11-08")
    assert "alice" not in files
    for name in ("index.md", "log.md", *[file for file in files.split() if file.startswith(("wiki/", "raw/"))]):
        assert "/home/alice" not in lab.show("ingest/2026-11-08", name), name


def test_a_project_id_holding_a_home_path_still_has_its_anchors_checked(lab: Lab) -> None:
    home_project(lab)
    run_a_week_later(lab)
    lab.merge("ingest/2026-11-08")
    lab.commit_in_repo("src/app.py", "def main():\n    return 2\n")

    assert "stale-anchor" in lab.kb("lint").stdout


def two_claims(lab: Lab) -> list[str]:
    """Two claims on main that a new note can both be about."""
    notes = []
    for text in ("the socket fixture hangs under the sandbox", "the socket fixture hangs under pytest-xdist"):
        notes.append(lab.leave("gotcha", text))
        lab.kb.later(minutes=1)
    run_a_week_later(lab)
    lab.merge("ingest/2026-11-08")
    return notes


@pytest.mark.parametrize(("answer", "relation"), [("contradicts", "conflicts"), ("adds", "extends")])
def test_a_note_about_two_claims_links_to_both(lab: Lab, answer: str, relation: str) -> None:
    old = two_claims(lab)
    text = "the socket fixture hangs everywhere under pytest"
    lab.fake.script[text] = {"claim": answer}
    new = lab.leave("gotcha", text)

    lab.kb.later(days=8)
    assert lab.kb("ingest").exit_code == 0

    page = lab.show("ingest/2026-11-16", PAGE)
    linked = claim_lines(page, new)[-1].partition(f" · {relation}: ")[2].split(", ")
    assert sorted(linked) == sorted(old)
    first, second = linked  # the closest match comes first, and the note goes right below it
    assert f"- source: {first} " in page.split(f"- source: {new} ")[0].rsplit("\n\n", 2)[-2]
    assert section_of(page, second) == "Gotchas"
    lab.merge("ingest/2026-11-16")
    assert "no-source" not in lab.kb("lint").stdout


def test_a_note_resolved_while_its_branch_waits_is_flagged_at_the_top_of_the_review_list(lab: Lab) -> None:
    notes = three_notes(lab)
    assert lab.kb("ingest").exit_code == 0
    lab.kb("note", "--resolve", notes[0], "--agent", "Codex", "no longer true since the uv upgrade")

    result = lab.kb("ingest")

    assert result.exit_code == 0
    review = lab.show(BRANCH, "manifests/2026-10-31.md")
    assert "## Resolved since filing" in review
    assert review.index("## Resolved since filing") < review.index(f"## {notes[1]} ")
    assert f"- {notes[0]}: no longer true since the uv upgrade\n" in review
    assert "resolved" in result.stdout
    assert "no new" not in result.stdout  # it did commit: the flag
    assert "uv needs --cache-dir in the sandbox\n" in lab.show(BRANCH, PAGE)  # kb leaves the claim for me to take out
    assert notes[0] not in [path.stem.rpartition("-")[2] for path in lab.inbox.glob("*.md")]


def test_a_resolved_note_is_flagged_once_and_the_next_run_changes_nothing(lab: Lab) -> None:
    notes = three_notes(lab)
    assert lab.kb("ingest").exit_code == 0
    lab.kb("note", "--resolve", notes[0], "--agent", "Codex", "no longer true")
    assert lab.kb("ingest").exit_code == 0
    tip = lab.wiki_git("rev-parse", BRANCH)

    result = lab.kb("ingest")

    assert result.exit_code == 0
    assert lab.wiki_git("rev-parse", BRANCH) == tip
    assert "resolved" not in result.stdout


def test_a_note_resolved_before_any_run_is_not_flagged(lab: Lab) -> None:
    notes = three_notes(lab)
    lab.kb("note", "--resolve", notes[0], "--agent", "Codex", "no longer true")
    lab.kb.later(days=8)

    assert lab.kb("ingest").exit_code == 0

    assert "Resolved since filing" not in lab.show("ingest/2026-11-08", REVIEW)


def test_a_failed_note_resolved_later_is_not_flagged_since_it_has_no_claim(lab: Lab) -> None:
    notes = three_notes(lab)
    lab.fake.script["uv needs --cache-dir"] = {"useful": "maybe"}
    assert lab.kb("ingest").exit_code == 0
    lab.kb("note", "--resolve", notes[0], "--agent", "Codex", "no longer true")

    assert lab.kb("ingest").exit_code == 0

    assert "Resolved since filing" not in lab.show(BRANCH, "manifests/2026-10-31.md")


def test_a_dry_run_does_not_flag_a_resolved_note(lab: Lab) -> None:
    notes = three_notes(lab)
    assert lab.kb("ingest").exit_code == 0
    tip = lab.wiki_git("rev-parse", BRANCH)
    lab.kb("note", "--resolve", notes[0], "--agent", "Codex", "no longer true")

    assert lab.kb("ingest", "--dry-run").exit_code == 0

    assert lab.wiki_git("rev-parse", BRANCH) == tip


def test_a_secret_in_a_command_ref_quarantines_that_note_and_the_others_are_filed(lab: Lab) -> None:
    notes = three_notes(lab)
    poison = lab.leave("gotcha", "the deploy command reads its key from the flag", f"deploy --key {LEAK}")

    result = lab.kb("ingest")

    assert result.exit_code == 0, result.stderr
    outcomes = lab.manifest()["outcomes"]
    assert outcomes[poison] == {"failed": "quarantined", "rules": ["fake-rule"]}  # type: ignore[index]
    assert all(outcomes[note]["action"] == "add" for note in notes)  # type: ignore[index]
    assert not [state for state, _ in lab.fake.calls if "the deploy command" in state]
    assert f"-{poison}.md" not in lab.wiki_git("ls-tree", "-r", "--name-only", BRANCH)


@pytest.mark.parametrize(
    "bad",
    ["src/a\x00b.py", "src/a\ud800b.py"],
    ids=["a-nul", "a-lone-surrogate"],
)
def test_a_note_with_unreadable_text_fails_alone_and_the_others_are_filed(lab: Lab, bad: str) -> None:
    notes = three_notes(lab)
    written = lab.inbox / "2026-10-31-n00aa11.md"
    good = next(lab.inbox.glob("*.md")).read_text()
    field = json.dumps(bad)  # kb note can't write this, but any agent can write the inbox
    lines = [f"refs: [{field}]" if line.startswith("refs:") else line for line in good.split("\n")]
    lines = ['id: "n00aa11"' if line.startswith("id:") else line for line in lines]
    written.write_text("\n".join(lines))

    result = lab.kb("ingest")

    assert result.exit_code == 0, result.stderr
    outcomes = lab.manifest()["outcomes"]
    assert outcomes["n00aa11"] == {"failed": "unreadable"}  # type: ignore[index]
    assert all(outcomes[note]["action"] == "add" for note in notes)  # type: ignore[index]
    assert len(lab.fake.calls) == 3


def test_a_file_at_the_wiki_path_is_an_error_not_a_crash(lab: Lab) -> None:
    three_notes(lab)
    lab.wiki.write_text("not a folder\n")

    result = lab.kb("ingest")

    assert result.exit_code == 1
    assert "wiki" in result.stderr


def worktree_of(lab: Lab) -> Path:
    return lab.kb.home / ".local" / "share" / "agent-fabric" / "ingest-wt"


def test_a_wiki_made_again_gets_a_new_worktree_and_the_run_goes_on(lab: Lab) -> None:
    notes = three_notes(lab)
    assert lab.kb("ingest").exit_code == 0
    assert worktree_of(lab).exists()
    shutil.rmtree(lab.wiki)
    lab.kb.later(days=1)

    result = lab.kb("ingest")

    assert result.exit_code == 0, result.stderr
    assert set(lab.manifest("ingest/2026-11-01", "2026-11-01")["outcomes"]) == set(notes)  # type: ignore[call-overload]


def test_a_first_run_that_fails_at_the_first_commit_can_run_again(lab: Lab) -> None:
    notes = three_notes(lab)
    good = {key: lab.kb.env.pop(key) for key in list(lab.kb.env) if key.startswith(("GIT_AUTHOR", "GIT_COMMITTER"))}
    lab.kb.env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    lab.kb.env["EMAIL"] = ""
    lab.kb.env["GIT_AUTHOR_NAME"] = ""

    first = lab.kb("ingest")

    assert first.exit_code == 1
    assert "commit" in first.stderr
    lab.kb.env.update(good)

    second = lab.kb("ingest")

    assert second.exit_code == 0, second.stderr
    assert set(lab.manifest()["outcomes"]) == set(notes)  # type: ignore[call-overload]


def test_a_repo_of_mine_at_the_wiki_path_is_left_alone(lab: Lab) -> None:
    three_notes(lab)
    lab.wiki.mkdir()
    (lab.wiki / "index.md").write_text("my own index\n")
    lab.git("init", "-q", "-b", "trunk", cwd=lab.wiki)
    lab.git("add", "-A", cwd=lab.wiki)
    lab.git("commit", "-q", "-m", "mine", cwd=lab.wiki)

    result = lab.kb("ingest")

    assert result.exit_code == 1
    assert "no main branch" in result.stderr
    assert (lab.wiki / "index.md").read_text() == "my own index\n"
    assert lab.fake.calls == []


def test_a_run_stops_before_any_model_is_asked_while_its_branch_is_checked_out_in_the_wiki(lab: Lab) -> None:
    three_notes(lab)
    assert lab.kb("ingest").exit_code == 0
    lab.wiki_git("checkout", "-q", BRANCH)
    tip = lab.wiki_git("rev-parse", BRANCH)
    asked = len(lab.fake.calls)
    extra = [lab.leave("gotcha", f"new trap number {n}") for n in range(3)]

    result = lab.kb("ingest")

    assert result.exit_code == 1
    assert "checked out" in result.stderr
    assert BRANCH in result.stderr
    assert len(lab.fake.calls) == asked
    assert lab.wiki_git("rev-parse", BRANCH) == tip
    assert lab.wiki_git("status", "--porcelain") == ""

    lab.wiki_git("checkout", "-q", "main")
    assert lab.kb("ingest").exit_code == 0
    assert set(extra) <= set(lab.manifest()["outcomes"])  # type: ignore[call-overload]


class CommitsDuringTheRun:
    """I commit on the branch while the model is thinking."""

    def __init__(self, lab: Lab) -> None:
        self.lab = lab
        self.committed = False

    def answer(self, state: str, questions: list[Question]) -> object:
        if not self.committed:
            self.committed = True
            other = self.lab.kb.home / "other-wt"
            self.lab.wiki_git("worktree", "add", "-q", str(other), BRANCH)
            (other / "mine.txt").write_text("a hand edit\n")
            self.lab.git("add", "-A", cwd=other)
            self.lab.git("commit", "-q", "-m", "a hand edit", cwd=other)
            self.lab.wiki_git("worktree", "remove", "--force", str(other))
        return self.lab.fake.answer(state, questions)


def test_a_commit_on_the_branch_during_a_run_is_kept_and_the_run_stops(lab: Lab) -> None:
    three_notes(lab)
    assert lab.kb("ingest").exit_code == 0
    extra = [lab.leave("gotcha", f"new trap number {n}") for n in range(3)]
    lab.kb.backend = CommitsDuringTheRun(lab)

    result = lab.kb("ingest")

    assert result.exit_code == 1
    assert lab.show(BRANCH, "mine.txt") == "a hand edit\n"
    assert not set(extra) & set(lab.manifest()["outcomes"])  # type: ignore[call-overload]


def test_a_git_that_hangs_stops_the_run_and_says_so(tmp_path: Path) -> None:
    stub = tmp_path / "git"
    stub.write_text("#!/bin/sh\nexec sleep 30\n")
    stub.chmod(0o755)

    with pytest.raises(GitError, match="timed out"):
        Git(tmp_path, {"PATH": f"{tmp_path}:{os.environ['PATH']}"}, timeout=0.2)("status")


def test_a_resolution_gitleaks_flags_does_not_stop_the_run_and_its_text_stays_off_the_review_list(lab: Lab) -> None:
    notes = three_notes(lab)
    assert lab.kb("ingest").exit_code == 0
    lab.kb("note", "--resolve", notes[0], "--agent", "Codex", f"fixed, and the {LEAK} was rotated")

    result = lab.kb("ingest")

    assert result.exit_code == 0, result.stderr
    review = lab.show(BRANCH, "manifests/2026-10-31.md")
    assert f"- {notes[0]}: " in review
    assert LEAK not in review
    assert LEAK not in lab.show(BRANCH, "manifests/2026-10-31.json")


def test_a_flag_that_could_not_be_committed_is_made_on_the_next_run(lab: Lab) -> None:
    notes = three_notes(lab)
    assert lab.kb("ingest").exit_code == 0
    lab.kb("note", "--resolve", notes[0], "--agent", "Codex", "no longer true")
    lab.wiki_git("checkout", "-q", BRANCH)
    assert lab.kb("ingest").exit_code == 1  # it has moved the notes to done/ by now
    lab.wiki_git("checkout", "-q", "main")

    assert lab.kb("ingest").exit_code == 0

    assert f"- {notes[0]}: no longer true\n" in lab.show(BRANCH, "manifests/2026-10-31.md")


def test_a_ref_whose_checkout_has_gone_is_told_to_the_model_and_marked_for_review(lab: Lab) -> None:
    lab.leave("gotcha", "main returns 1, whatever it was given", "src/app.py", "pytest -q")
    shutil.rmtree(lab.repo)
    lab.kb.cwd = lab.kb.home

    run_a_week_later(lab)

    state = lab.fake.calls[0][0]
    assert "Ref 0: src/app.py\nNot checked" in state
    assert "pytest -q" not in state
    review = lab.show("ingest/2026-11-08", REVIEW)
    assert "- ref not checked, its checkout isn't here: src/app.py\n" in review
    assert "pytest -q" not in review
    assert "ref not found" not in review
