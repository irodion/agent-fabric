"""Seam B: `kb setup` with scripted answers in place of a person at the keyboard (docs/design.md §4.11)."""

import io
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import Kb, Lab, a_stub
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.plain_text import PlainTextOutput
from test_doctor import install_kb, report

from agent_fabric.asker import QuestionaryAsker
from agent_fabric.context import Context
from agent_fabric.setup import NothingChanged, Quit, setup

ENTER = object()  # take what the question opens on


@dataclass
class Asked:
    kind: str
    message: str
    choices: list[str]
    default: object


@dataclass
class Scripted:
    """Answers by a fragment of the question; a question no fragment matches gets Enter. A list of texts answers a
    repeated text question in turn, then Enter."""

    answers: dict[str, object] = field(default_factory=dict)
    asked: list[Asked] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)  # what `validate` said of a text answer
    told: list[str] = field(default_factory=list)  # what setup said without asking
    headings: list[tuple[int, str]] = field(default_factory=list)  # each with the number of questions asked before

    def _answer(self, kind: str, message: str, choices: list[str], default: object) -> object:
        self.asked.append(Asked(kind, message, choices, default))
        for fragment, answer in self.answers.items():
            if fragment in message:
                if isinstance(answer, Exception):
                    raise answer
                if isinstance(answer, list) and kind == "text":
                    return answer.pop(0) if answer else ENTER
                return answer
        return ENTER

    def checkbox(self, message: str, choices: list[str], checked: list[str]) -> list[str]:
        answer = self._answer("checkbox", message, choices, checked)
        if answer is ENTER:
            return list(checked)
        assert isinstance(answer, list)
        return [str(item) for item in answer]

    def select(self, message: str, choices: list[str], default: str) -> str:
        answer = self._answer("select", message, choices, default)
        assert answer is ENTER or answer in choices
        return default if answer is ENTER else str(answer)

    def confirm(self, message: str, default: bool) -> bool:
        answer = self._answer("confirm", message, [], default)
        return default if answer is ENTER else bool(answer)

    def text(self, message: str, validate: Callable[[str], str | None]) -> str:
        while True:
            answer = self._answer("text", message, [], "")
            typed = "" if answer is ENTER else str(answer)
            if (refusal := validate(typed)) is None:
                return typed
            assert answer is not ENTER, f"Enter refused at {message!r}: {refusal}"
            self.refused.append(refusal)

    def tell(self, message: str) -> None:
        self.told.append(message)

    def heading(self, title: str) -> None:
        self.headings.append((len(self.asked), title))

    def questions(self, kind: str) -> list[Asked]:
        return [asked for asked in self.asked if asked.kind == kind]


def a_repo(lab: Lab, folder: Path, origin: str | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    lab.git("init", "-q", "-b", "main", cwd=folder)
    if origin:
        lab.git("remote", "add", "origin", origin, cwd=folder)
    return folder


def registered(kb: Kb, *roots: str) -> None:
    for root in roots:
        kb.home.joinpath(root.removeprefix("~/")).mkdir(parents=True, exist_ok=True)
    config(kb).parent.mkdir(parents=True, exist_ok=True)
    config(kb).write_text("".join(f'[[workspace]]\nroot = "{root}"\n' for root in roots))


def config(kb: Kb) -> Path:
    return kb.home / ".config" / "agent-fabric" / "config.toml"


def kb_setup(kb: Kb, asker: Scripted) -> tuple[int, str]:
    install_kb(kb, kb.home / ".local" / "share" / "uv" / "tools")
    env = {"HOME": str(kb.home), **kb.env}
    return setup(Context(env=env, cwd=kb.cwd, stdin=io.StringIO(), now=kb.now), asker)


def test_enter_at_every_question_sets_up_the_defaults(kb: Kb) -> None:
    exit_code, output = kb_setup(kb, Scripted())

    assert report(output)["checks"]
    assert not config(kb).exists()  # nothing to record: every choice is its default
    assert (kb.home / ".local" / "share" / "agent-fabric" / "inbox").is_dir()
    assert exit_code in (0, 1)


def test_the_hooks_chosen_are_recorded(kb: Kb) -> None:
    kb_setup(kb, Scripted({"Hooks": "start"}))

    assert config(kb).read_text() == '[notes]\nhooks = ["start"]\n'


def test_a_folder_added_as_a_root_gets_its_entry_and_its_inbox(kb: Kb) -> None:
    root = kb.home / "work" / "acme"
    root.mkdir(parents=True)

    kb_setup(kb, Scripted({"new root": ["~/work/acme"]}))

    assert '[[workspace]]\nroot = "~/work/acme"\n' in config(kb).read_text()
    assert (root / ".fabric" / "inbox").is_dir()


def test_a_root_must_be_a_folder_outside_git_and_not_a_root_yet(lab: Lab) -> None:
    kb = lab.kb
    (kb.home / "work").mkdir()
    asker = Scripted({"new root": ["~/nowhere", "work", str(lab.repo), "~/work", "~/work"]})

    kb_setup(kb, asker)

    assert asker.refused == [
        "~/nowhere isn't a folder",
        "a full path, or one that starts with ~/",
        "it's in a git work tree: a root holds repos and is none itself",
        "it's a root already",
    ]
    assert config(kb).read_text().count("[[workspace]]") == 1


def test_a_root_left_out_loses_its_entry_and_keeps_its_folder(kb: Kb) -> None:
    for name in ("acme", "globex"):
        (kb.home / "work" / name / ".fabric" / "inbox").mkdir(parents=True)
    config(kb).parent.mkdir(parents=True)
    config(kb).write_text(
        '# my roots\n\n# client A\n[[workspace]]\nroot = "~/work/acme"\nallow = ["x/y/z"]\n\n'
        '# client B, kept apart\n[[workspace]]\nroot = "~/work/globex"\n\n[notes]\nhooks = ["start", "stop"]\n'
    )

    kb_setup(kb, Scripted({"keep which roots": ["~/work/globex"]}))

    assert config(kb).read_text() == (
        '# my roots\n\n# client B, kept apart\n[[workspace]]\nroot = "~/work/globex"\n\n'
        '[notes]\nhooks = ["start", "stop"]\n'
    )
    assert (kb.home / "work" / "acme" / ".fabric" / "inbox").is_dir()


def test_a_roots_allow_list_offers_its_projects_and_records_the_choice(lab: Lab) -> None:
    kb = lab.kb
    registered(kb, "~/work/acme", "~/work/acme/sub")
    a_repo(lab, kb.home / "work" / "acme" / "billing", "git@github.com:acme/billing.git")
    a_repo(lab, kb.home / "work" / "acme" / "group" / "infra")
    a_repo(lab, kb.home / "work" / "acme" / "sub" / "inner", "git@github.com:acme/inner.git")
    asker = Scripted({"~/work/acme: projects to allow": ["github.com/acme/billing", "machine"]})

    kb_setup(kb, asker)

    offered = next(q for q in asker.questions("checkbox") if "~/work/acme: projects to allow" in q.message)
    assert offered.choices == [
        str(kb.home / "work" / "acme" / "group" / "infra"),
        "github.com/acme/billing",
        "machine",
    ]
    assert offered.default == []
    assert 'root = "~/work/acme"\nallow = ["github.com/acme/billing", "machine"]\n' in config(kb).read_text()


def test_the_default_workspace_offers_the_projects_with_notes_in_its_inbox(lab: Lab) -> None:
    lab.leave("gotcha", "the build needs a warm cache")
    asker = Scripted()

    kb_setup(lab.kb, asker)

    offered = next(q for q in asker.questions("checkbox") if "default workspace: projects to allow" in q.message)
    assert offered.choices == ["github.com/me/proj", "machine"]
    assert offered.default == ["github.com/me/proj"]  # the lab allows it


def test_a_project_folder_named_is_allowed_by_its_id(lab: Lab) -> None:
    kb = lab.kb
    registered(kb, "~/work/acme")
    a_repo(lab, kb.home / "work" / "acme" / "deep" / "er" / "still" / "api", "https://example.test/acme/api.git")
    asker = Scripted({"~/work/acme: another project folder": [str(lab.repo), "~/work/acme/deep/er/still/api"]})

    kb_setup(kb, asker)

    assert asker.refused == ["it's in another workspace"]
    assert 'allow = ["example.test/acme/api"]' in config(kb).read_text()


HAND_WRITTEN = """\
# kb's settings, by hand
[publish]
allow = [
    "github.com/me/proj",  # mine
    "machine",
]  # what may leave
share_machine_notes = false

[ingest]
backend = "codex"

[[workspace]]
root = "~/work/acme"   # client A
allow = ["github.com/acme/billing"]
share_machine_notes = false
backend = "local"

[notes]
hooks = ["stop"]
"""


def test_a_rerun_that_only_presses_enter_changes_no_byte(lab: Lab) -> None:
    kb = lab.kb
    (kb.home / "work" / "acme").mkdir(parents=True)
    config(kb).write_text(HAND_WRITTEN)
    a_stub(kb.home / ".local" / "bin", "codex", "#!/bin/sh\nexit 1\n")
    asker = Scripted()

    kb_setup(kb, asker)

    assert config(kb).read_text() == HAND_WRITTEN
    assert [(q.message, q.default) for q in asker.questions("confirm")][:2] == [
        ("default workspace: share machine notes with the other workspaces?", False),
        ("~/work/acme: share machine notes with the other workspaces?", False),
    ]
    backend = next(q for q in asker.questions("select") if q.message.startswith("~/work/acme"))
    assert (backend.choices, backend.default) == (["codex", "local"], "local")


def test_a_change_touches_only_its_own_value(lab: Lab) -> None:
    kb = lab.kb
    (kb.home / "work" / "acme").mkdir(parents=True)
    config(kb).write_text(HAND_WRITTEN)

    kb_setup(kb, Scripted({"default workspace: projects to allow": ["machine"], "~/work/acme: share": True}))

    assert config(kb).read_text() == HAND_WRITTEN.replace(
        '[\n    "github.com/me/proj",  # mine\n    "machine",\n]  # what may leave', '["machine"]  # what may leave'
    ).replace(
        'allow = ["github.com/acme/billing"]\nshare_machine_notes = false',
        'allow = ["github.com/acme/billing"]\nshare_machine_notes = true',
    )


def test_settings_that_change_from_their_defaults_are_added_to_their_tables(lab: Lab) -> None:
    kb = lab.kb
    registered(kb, "~/work/acme")

    kb_setup(kb, Scripted({"default workspace: share": False, "~/work/acme: share": False}))

    assert config(kb).read_text() == (
        '[[workspace]]\nroot = "~/work/acme"\nshare_machine_notes = false\n\n[publish]\nshare_machine_notes = false\n'
    )


def test_the_backend_is_asked_only_between_backends_installed_and_named_where_there_is_one(lab: Lab) -> None:
    a_stub(lab.kb.home / ".local" / "bin", "codex", "#!/bin/sh\nexit 1\n")
    asker = Scripted()

    kb_setup(lab.kb, asker)

    assert [q.message for q in asker.questions("select")] == ["Hooks: which reminders should agents get?"]
    assert asker.told == ["default workspace: kb ingest uses codex, the one backend kb has that's installed"]


def test_the_backend_is_asked_where_codex_and_claude_are_both_installed(lab: Lab) -> None:
    for cli in ("codex", "claude"):
        a_stub(lab.kb.home / ".local" / "bin", cli, "#!/bin/sh\nexit 1\n")
    asker = Scripted({"default workspace: backend": "claude"})

    kb_setup(lab.kb, asker)

    backend = next(q for q in asker.questions("select") if q.message.startswith("default workspace"))
    assert (backend.choices, backend.default) == (["codex", "claude"], "codex")
    assert 'backend = "claude"' in config(lab.kb).read_text()


def test_where_only_claude_is_installed_it_is_the_default_over_codex(lab: Lab) -> None:
    a_stub(lab.kb.home / ".local" / "bin", "claude", "#!/bin/sh\nexit 1\n")
    asker = Scripted()

    kb_setup(lab.kb, asker)

    backend = next(q for q in asker.questions("select") if q.message.startswith("default workspace"))
    assert (backend.choices, backend.default) == (["claude", "codex"], "claude")
    assert 'backend = "claude"' in config(lab.kb).read_text()


@pytest.mark.parametrize("recorded", ["codex", "local"])
def test_a_backend_that_isnt_installed_is_named_as_missing(lab: Lab, recorded: str) -> None:
    kb = lab.kb
    config(kb).write_text(f'[ingest]\nbackend = "{recorded}"\n')
    asker = Scripted()

    kb_setup(kb, asker)

    assert asker.told == [
        f"default workspace: kb ingest uses {recorded}, but no backend kb has is installed (codex, claude)"
    ]


def test_leaving_before_the_last_step_writes_nothing(kb: Kb) -> None:
    (kb.home / "work").mkdir()
    for answers in ({"Hooks": Quit()}, {"Write these choices": False}):
        with pytest.raises(Quit):
            kb_setup(kb, Scripted({"new root": ["~/work"], "Hooks": "none", **answers}))

    assert not config(kb).exists()
    assert not (kb.home / ".local" / "share" / "agent-fabric" / "inbox").exists()


def test_a_config_kb_cant_edit_safely_is_left_as_it_is(lab: Lab) -> None:
    kb = lab.kb
    inline = 'publish = { allow = ["github.com/me/proj"] }\n'
    config(kb).write_text(inline)

    with pytest.raises(NothingChanged, match="by hand"):
        kb_setup(kb, Scripted({"default workspace: projects to allow": ["machine"]}))

    assert config(kb).read_text() == inline


def test_setup_wants_a_terminal(kb: Kb) -> None:
    result = kb("setup")

    assert result.exit_code == 2
    assert "in a terminal" in result.stderr
    assert not config(kb).exists()


def test_only_setup_loads_questionary() -> None:
    script = "import sys, agent_fabric.cli; print('questionary' in sys.modules)"

    loaded = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True).stdout

    assert loaded == "False\n"


def test_an_unreadable_config_stops_setup_before_any_question(kb: Kb) -> None:
    config(kb).parent.mkdir(parents=True)
    config(kb).write_text("[notes\n")
    asker = Scripted()

    with pytest.raises(NothingChanged, match=r"can't read ~/\.config/agent-fabric/config\.toml"):
        kb_setup(kb, asker)

    assert asker.asked == []


def test_the_terminal_asker_opens_on_the_defaults_and_quits_on_ctrl_c() -> None:
    def answer(keys: str, ask: Callable[[QuestionaryAsker], object]) -> object:
        with create_pipe_input() as typed:
            typed.send_text(keys)
            return ask(QuestionaryAsker(input=typed, output=DummyOutput()))

    assert answer("\r", lambda a: a.checkbox("roots", ["a", "b"], ["b"])) == ["b"]
    assert answer(" \r", lambda a: a.checkbox("roots", ["a", "b"], ["b"])) == ["a", "b"]
    assert answer("\r", lambda a: a.select("hooks", ["both", "none"], "none")) == "none"
    assert answer("\r", lambda a: a.confirm("share", False)) is False
    assert answer("x\r\x7fy\r", lambda a: a.text("folder", lambda t: "not x" if t == "x" else None)) == "y"
    with pytest.raises(Quit):
        answer("\x03", lambda a: a.confirm("share", True))
    with pytest.raises(Quit):  # Ctrl-D, or input that ends
        answer("\x04", lambda a: a.text("folder", lambda t: None))


def test_a_root_written_two_ways_is_asked_about_once_by_its_first_entry(lab: Lab) -> None:
    kb = lab.kb
    root = kb.home / "work" / "acme"
    root.mkdir(parents=True)
    config(kb).write_text(
        f'[[workspace]]\nroot = "~/work/acme"\nshare_machine_notes = false\n\n[[workspace]]\nroot = "{root}"\n'
    )
    before = config(kb).read_text()
    asker = Scripted()

    kb_setup(kb, asker)

    assert asker.questions("checkbox")[0].choices == ["~/work/acme"]
    shares = [(q.message, q.default) for q in asker.questions("confirm") if "share" in q.message]
    assert shares == [
        ("default workspace: share machine notes with the other workspaces?", True),
        ("~/work/acme: share machine notes with the other workspaces?", False),
    ]
    assert config(kb).read_text().startswith(before)


def test_a_project_folder_is_named_by_a_full_path(lab: Lab) -> None:
    kb = lab.kb
    registered(kb, "~/work/acme")
    a_repo(lab, kb.home / "work" / "acme" / "api", "https://example.test/acme/api.git")
    kb.cwd = kb.home / "work" / "acme"
    asker = Scripted({"~/work/acme: another project folder": ["api", "~/work/acme/api"]})

    kb_setup(kb, asker)

    assert asker.refused == ["a full path, or one that starts with ~/"]


def test_enter_keeps_an_allow_list_in_its_own_order(lab: Lab) -> None:
    kb = lab.kb
    text = '[publish]\nallow = [\n    "machine",  # first\n    "github.com/me/proj",\n]\n'
    config(kb).write_text(text)

    # as questionary returns a checkbox's picks: in the order the choices are listed
    kb_setup(kb, Scripted({"default workspace: projects to allow": ["github.com/me/proj", "machine"]}))

    assert config(kb).read_text().startswith(text)


@pytest.mark.parametrize(
    "entry",
    [
        '[[workspace]]\nroot = "~/w"\nbackend = 1\n',
        'workspace = [{ root = "~/w" }]\n',
        '[publish]\n"allow" = []\n',
        "publish.allow = []\n",
        '[notes]\n"hooks" = ["start"]\n',
        '[[workspace]]\nroot = "~/w"\n\n[workspace.extra]\nx = 1\n',
    ],
    ids=[
        "a bad setting",
        "an entry kb can't edit",
        "a quoted key",
        "a dotted key",
        "a quoted hooks key",
        "an entry kb can't drop",
    ],
)
def test_a_root_entry_kb_cant_use_stops_setup_before_any_question(kb: Kb, entry: str) -> None:
    config(kb).parent.mkdir(parents=True)
    config(kb).write_text(entry)
    asker = Scripted()

    with pytest.raises(NothingChanged):
        kb_setup(kb, asker)

    assert asker.asked == []


def test_the_default_workspace_leaves_out_projects_now_under_a_root(lab: Lab) -> None:
    kb = lab.kb
    lab.leave("gotcha", "the build needs a warm cache")
    config(kb).write_text("")
    asker = Scripted({"new root": ["~/code"]})

    kb_setup(kb, asker)

    offered = {q.message.partition(":")[0]: q.choices for q in asker.questions("checkbox")}
    assert offered == {"default workspace": ["machine"], "~/code": ["github.com/me/proj", "machine"]}


def test_settings_added_to_a_last_line_without_a_line_break_are_one_line_each(lab: Lab) -> None:
    kb = lab.kb
    (kb.home / "w").mkdir()
    config(kb).write_text('[[workspace]]\nroot = "~/w"')

    kb_setup(kb, Scripted({"~/w: projects to allow": ["machine"], "~/w: share": False}))

    assert config(kb).read_text() == '[[workspace]]\nroot = "~/w"\nallow = ["machine"]\nshare_machine_notes = false\n'


def test_a_root_removed_and_added_again_another_way_keeps_its_settings(lab: Lab) -> None:
    kb = lab.kb
    root = kb.home / "work" / "acme"
    root.mkdir(parents=True)
    config(kb).write_text(f'[[workspace]]\nroot = "{root}"\nallow = ["machine"]\n')

    kb_setup(kb, Scripted({"keep which roots": [], "new root": ["~/work/acme"]}))

    assert config(kb).read_text() == '[[workspace]]\nroot = "~/work/acme"\nallow = ["machine"]\n'


def test_a_root_kept_through_its_second_entry_gets_the_settings_shown(lab: Lab) -> None:
    kb = lab.kb
    root = kb.home / "work" / "acme"
    root.mkdir(parents=True)
    config(kb).write_text(
        f'[[workspace]]\nroot = "{root}"\nshare_machine_notes = false\n\n[[workspace]]\nroot = "~/work/acme"\n'
    )

    kb_setup(kb, Scripted({"keep which roots": [], "new root": ["~/work/acme"]}))

    # the share shown was the first entry's: the one left must record it, not keep its own
    assert config(kb).read_text() == '[[workspace]]\nroot = "~/work/acme"\nshare_machine_notes = false\n'


def test_a_worktree_below_a_root_is_offered_where_its_main_checkout_is(lab: Lab) -> None:
    kb = lab.kb
    registered(kb, "~/work/acme")
    lab.git("worktree", "add", "-q", str(kb.home / "work" / "acme" / "wt"), cwd=lab.repo)
    asker = Scripted()

    kb_setup(kb, asker)

    offered = next(q for q in asker.questions("checkbox") if "~/work/acme: projects to allow" in q.message)
    assert offered.choices == ["machine"]


def test_a_note_with_a_relative_folder_stays_in_the_default_workspace(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    kb = lab.kb
    lab.leave("gotcha", "the build needs a warm cache")
    note = next((kb.home / ".local" / "share" / "agent-fabric" / "inbox").rglob("*.md"))
    note.write_text(re.sub(r"^cwd: .*$", 'cwd: "code/proj"', note.read_text(), flags=re.MULTILINE))
    monkeypatch.chdir(kb.home)  # where a relative folder would resolve
    config(kb).write_text("")
    asker = Scripted({"new root": ["~/code"]})

    kb_setup(kb, asker)

    offered = next(q for q in asker.questions("checkbox") if "default workspace" in q.message)
    assert offered.choices == ["github.com/me/proj", "machine"]  # a note's folder is untrusted: not placed by it


def test_sharing_chosen_for_a_root_written_two_ways_takes_effect_in_both_entries(lab: Lab) -> None:
    kb = lab.kb
    root = kb.home / "work" / "acme"
    root.mkdir(parents=True)
    config(kb).write_text(
        f'[[workspace]]\nroot = "~/work/acme"\n\n[[workspace]]\nroot = "{root}"\nshare_machine_notes = false\n'
    )

    kb_setup(kb, Scripted({"~/work/acme: share": True}))

    # kb reads an opt-out from any entry for the folder
    assert config(kb).read_text() == (
        f'[[workspace]]\nroot = "~/work/acme"\n\n[[workspace]]\nroot = "{root}"\nshare_machine_notes = true\n'
    )


@pytest.mark.parametrize("opted_out", [0, 1], ids=["first entry opts out", "second entry opts out"])
def test_enter_keeps_a_root_written_two_ways_as_it_is(lab: Lab, opted_out: int) -> None:
    kb = lab.kb
    root = kb.home / "work" / "acme"
    root.mkdir(parents=True)
    entries = [f'[[workspace]]\nroot = "{written}"\n' for written in ("~/work/acme", root)]
    entries[opted_out] += "share_machine_notes = false\n"
    config(kb).write_text("\n".join(entries))
    before = config(kb).read_text()

    kb_setup(kb, Scripted())

    assert config(kb).read_text() == before


def test_each_workspaces_questions_come_under_a_heading_that_names_it(kb: Kb) -> None:
    registered(kb, "~/work/acme")
    asker = Scripted()

    kb_setup(kb, asker)

    # Each heading, and the questions just after and just before it: its workspace's first, the last one's before.
    assert [(asker.asked[at - 1].message, title, asker.asked[at].message) for at, title in asker.headings[1:]] == [
        (
            "default workspace: share machine notes with the other workspaces?",
            "~/work/acme: notes from folders under it",
            "~/work/acme: projects to allow",
        ),
        (
            "~/work/acme: share machine notes with the other workspaces?",
            "this machine: every agent",
            "Hooks: which reminders should agents get?",
        ),
    ]
    assert asker.headings[0][1] == "default workspace: notes from folders under no root"
    assert asker.asked[asker.headings[0][0]].message == "default workspace: projects to allow"


def test_with_one_workspace_its_questions_need_no_heading(kb: Kb) -> None:
    asker = Scripted()

    kb_setup(kb, asker)

    assert asker.headings == []


def test_the_terminal_asker_shows_a_heading_on_a_line_of_its_own() -> None:
    shown = io.StringIO()

    QuestionaryAsker(output=PlainTextOutput(shown)).heading("~/work/acme: notes from folders under it")

    assert shown.getvalue().splitlines() == ["", "── ~/work/acme: notes from folders under it ──"]


def test_the_terminal_asker_tells_a_choice_made_without_asking() -> None:
    shown = io.StringIO()

    QuestionaryAsker(output=PlainTextOutput(shown)).tell("default workspace: kb ingest uses codex")

    assert shown.getvalue().splitlines() == ["  default workspace: kb ingest uses codex"]
