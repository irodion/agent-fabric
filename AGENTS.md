# AGENTS.md

## What exists

`docs/design.md` plans a memory shared across coding agents: session digests, a live panel, a wiki, an MCP server.
Three parts are built: agent notes (`kb note`, `inbox`, `doctor`, `hook` and the `kb-note` skill), notes into the wiki
(`kb ingest`, `kb lint`), and the CI that builds and vets agentsview. The rest of the design, including `fabricd` and
the other `kb` commands, is still a plan (§9 has the phases).

The design holds decisions, their reasons and dated findings from real agent runs. Details live in code and tests,
which cite it by section. Read the section before changing what it covers:

- §4.6, §4.7: wiki pages, `kb ingest`, `kb lint`
- §4.10: notes, how a note is signed, the hooks, `kb doctor`
- §4.1: the agentsview pin, its build, the vet gate
- §7: anything that reaches an agent's prompt or leaves the machine
- §10: testing in a real agent, and where each agent records what a hook did

A real run that settles or contradicts the design goes in it, with the agent's version and the date. §11 lists what's
still open.

## Commands

```bash
scripts/check.sh               # lint.yml's checks, one line per check
scripts/check.sh --red <rev>   # names new tests that pass on <rev>'s code: they don't show the change
uv run pytest tests/test_note.py::test_a_kind_given_with_resolve_is_refused
uv run kb <args>               # this clone's kb, on the real home
```

In an agent's sandbox, plain `uv run` needs `--cache-dir "$TMPDIR/uv"` after `run`; `scripts/check.sh` adds it
itself.

- `scripts/check.sh` uses one Python version (`.python-version`, 3.14) and skips tools that aren't installed. CI also
  runs 3.13, the minimum, so code has to work there, and the vet gate. The check runs before a commit only where
  `.git/hooks/pre-commit` calls pre-commit; nothing in this repo installs that hook.
- The `kb` on PATH is a non-editable install, so it runs the last installed code. `scripts/install.sh` updates it, the
  skill copies and the hooks, and has to run in the user's own terminal, because a sandbox can't write the agents'
  settings: ask the user to run it.
- `kb note` and `kb hook` write to the real inbox and markers, and the inbox is append-only. `kb doctor --fix` edits
  the real agents' settings, and writes and starts `fabric-ingest.timer` once a project is allowed. Tests that run
  `kb doctor` with an allow list use the `lab` fixture, whose stand-in `systemctl` keeps them off the real user
  manager. To try a command that writes, with `XDG_DATA_HOME` unset, give it a throwaway home:
  `HOME="$(mktemp -d)" .venv/bin/kb <args>`.
- `kb ingest` moves notes out of the inbox, writes `~/wiki` and spends model requests through `codex`, so ask first. To
  try it, use a throwaway home with a stub `codex` on PATH (`tests/test_codex.py` has one).
- Under `uv run`, `kb doctor` reports a `problem` on its `kb` row, because `.venv/bin` comes first on PATH. That's
  expected.
- A commit in a sandbox with other changes unstaged fails, because pre-commit can't stash them in its read-only cache.
  Use `PRE_COMMIT_HOME="$TMPDIR/pre-commit" git commit`.
- `scripts/spike/hook-lab.sh <cursor|copilot|codex> [--stop]` runs a real agent's hooks on this clone's `kb` in a
  throwaway repo. In Cursor the user's own hooks fire too, with the installed `kb` on the same data folder, so a marker
  can show the old code's work. It runs outside the sandbox and spends model requests, so ask first.

## Architecture

`cli.run(argv, env=, cwd=, stdin=, now=)` is the program's entry, and `main` fills those arguments from the process.
Pass them down (`context.Context`) so that tests can drive any command in-process with a fake home and a fixed clock,
through the `kb` fixture in `tests/conftest.py`. The `lab` fixture adds a project in git, a fake model and a stand-in
gitleaks. Two places still see the real environment, so a fake home doesn't
reach them: `gitinfo._git` runs git with it, and `notes.missing_ref` expands `~` from it.

- **Notes.** `notes.py` is the file format, `inbox.py` the folder. The inbox is append-only: each note is a new file
  created exclusively, and `--resolve` writes a further note that points at the old one.
- **Project identity.** `gitinfo.project_id` names a folder's project: the `origin` remote, else the main checkout,
  else the folder. `kb inbox`, the start hook's count and the markers' project go through it. `kb note` works out the
  same thing separately, from `git_facts` and `Note.scope`, so a change to one has to match the other.
- **Workspaces.** `workspaces.workspace_of` names a folder's workspace (§4.11): the innermost `[[workspace]]` root
  that holds its main checkout, else the default. `kb note`, `kb inbox` and the start hook's count use that
  workspace's inbox. The hooks' "is kb set up" check looks for any workspace's inbox without running git, since the
  stop hook must ask before git can hang, and a config they can't read leaves them on the default workspace, while
  `kb note` stops and names it. Ingest, lint, doctor and the markers don't know workspaces yet.
- **Who signs a note.** `agents.detect_agent` depends on whether an agent's variable is set. With one, an `--agent`
  naming a tool wins, because an agent started by another inherits its variables, and otherwise the variable does. With
  none, an `--agent` naming Copilot CLI or Cursor CLI wins, then the hooks' markers, then any other `--agent`. `kb`
  knows no variable for those two, so `hooks.py` keeps one marker per session in `~/.local/share/agent-fabric/hooks/`.
  The same markers limit the stop question to once per session and give `kb doctor` its "last ran".
- **Hooks.** `hooks.py` runs at each hook and knows each agent's events and output format. `hookfiles.py` installs kb's
  entries in each agent's hooks file, recognises them by their command, and keeps every other entry.
- **Ingest.** `ingest.py` runs §4.7's steps, and the model answers only through `backends.Backend`. `decisions.py` turns
  answers into one action, `evidence.py` reads a note's refs, `secretgate.py` redacts and scans, `pages.py` is the page
  format and `manifests.py` records what each branch took. `lint.py` is `kb lint`, and ingest runs it too.
- **`kb doctor`.** `doctor.py` gathers one `report.Row` per check, with `sandboxes.py` and `hookfiles.py` covering the
  agents' settings, `timer.py` the systemd units that run `kb ingest` daily, and `report.py` renders one TOON
  document. It writes TOON by hand: `toon-format` is a dev dependency that tests use as an independent decoder, and
  the package has no runtime dependencies yet.

These hold across the code:

- A hook never breaks a session: on any error it prints nothing and exits 0.
- The start hook's reminder carries a count and the project id, and the stop hook's question is a fixed string. Note
  text reaches an agent only through `kb inbox`, under its untrusted header.
- No model text reaches a claim: `kb` files the note's own text, and a claim's lines never change once filed.
- A note is untrusted. Its refs are read only from git objects of its own project, and its text is escaped when filed.
  A bad note fails by itself, and the run files the others.
- Ingest fails closed: if gitleaks can't vouch for a text, the run stops before any model sees it. A file is redacted
  and scanned whole before it's cut to an excerpt, because a cut can hide what makes a secret recognisable.
- `kb doctor` only reads, unless given `--fix`. `--fix` edits the user's agent settings and keeps everything else in
  them. A file it can't edit safely gets a `problem` row with a step for the user, and stays as it is.
- `src/agent_fabric/skills/kb-note/SKILL.md` is the one copy of the skill. `kb doctor` compares the installed copies
  byte for byte with its own package's copy, so after an edit only `uv run kb doctor` shows `outdated`. The `kb` on
  PATH says `ok` until `scripts/install.sh` runs again.
- This repo is public, so test fixtures are synthetic or scrubbed recordings.

## Dependencies

The vet gate fails any version published less than 14 days ago. `exclude-newer` in `pyproject.toml` holds uv to an
older PyPI snapshot. To upgrade, move that date forward, keeping it at least 14 days back, then run `uv lock --upgrade`:
moving the date alone changes no versions, and the checks still pass.
