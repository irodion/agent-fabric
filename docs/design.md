# agent-fabric — design

Status: draft v0.6, 2026-11-04 (v0.3 had two review rounds; v0.4 put the CLI agents first and added the CLI
capture check, §4.1; v0.5 adds agent notes, §4.10; v0.6 rebuilds ingest around notes, §4.7). Name: agent-fabric; CLI:
`kb`.
Background research: `~/code/reports/Cross agent coding memory harness.md`, `~/code/reports/Inbox to wiki.md`.

## 1. Goal

Switch freely between coding agents without losing the history of what was done and decided: first the CLI agents
(Claude Code, Codex, Copilot CLI, Cursor CLI), later the IDE agents (Cursor IDE, VS Code Copilot Chat). Concretely:

1. Every session leaves a deterministic, redacted **digest** in a local Markdown wiki repo.
2. A **live panel** beside the agent lists decisions, risky actions and open loops ("disabled test, claimed
   temporary — verify later") while the session runs.
3. An LLM periodically folds notes, and later digests, into curated **wiki pages** (manual trigger + timer), reviewed
   before merge.
4. Agents and I **pull** history on demand through one CLI (`kb`) and one MCP server. No history is injected into
   agent prompts automatically; the standing instructions are the `kb-note` skill and two hooks that remind the
   agent of it, with a count of open notes but never their text (item 5).
5. Agents leave short **notes** for each other and for me with `kb note` when they find something the next agent
   must know (broken tests, a surprising fix, a decision and its reason); a skill tells them when and how, and hooks
   at session start and at the end of a turn remind them (§4.10).

### Non-goals

- Replacing or wrapping the agents. They run in their native UIs, untouched.
- Blocking or approving agent actions. The panel is advisory only.
- Multi-user or multi-machine sync. Each machine keeps its own inbox and wiki (§2).
- Parsing vendor storage formats ourselves (delegated to agentsview, §4.1).

## 2. Context and decisions so far

| Topic | Decision | Why |
|---|---|---|
| Host | Linux desktop (Ubuntu 24.04 or newer), Core Ultra 7 265H (Arc 140T iGPU), 64 GB RAM; all agents run locally on it | IDE chat history lives where the IDE runs |
| Machines | Each machine keeps its own inbox and wiki, with no sync (2026-11-04) | Machines need no link to each other |
| Agents first | The CLI agents: Claude Code, Codex, Copilot CLI, Cursor CLI; the IDE agents in phase 3 | Current focus (2026-10-31) |
| Capture | agentsview, built on CI from a pinned tag, sandboxed, behind an adapter; Cursor CLI through its own reader | agentsview parses Claude Code, Codex, Copilot CLI, Cursor IDE and VS Code Copilot Chat on Linux; for Cursor CLI it drops tool results and fails under a read-only home (§4.1) |
| Supply chain | SafeDep `vet` gate on agentsview's dependencies and on ours from the first commit | One reviewed build per version; malware and fresh-package checks before anything runs on the desktop |
| Publishing | Allow list of projects; everything else stays out of the wiki | Client work never reaches the wiki or a hosted model by default |
| Repos | `agent-fabric` (code) public, MIT; the wiki repo local, with no remote | Code is shareable; session data never is |
| Session end | Idle timeout, not session-end hooks | Cursor's `sessionEnd` is unreliable; Codex/Claude give hooks 1–3 s |
| Compaction | Deterministic extraction; no LLM in the digest | Facts are extractable; only "why" needs a model |
| Retrieval | Pull only (CLI/MCP), manually triggered | Avoids context pollution and memory-poisoning via injection |
| Agent notes | `kb note` CLI + `kb-note` skill → local inbox; the wiki only through ingest (§4.10) | Agents notice things no digest records; a CLI works in every agent, and a skill loads only when used |
| Note reminders | A session-start hook (reminder + count of open notes) and an end-of-turn hook (asks once per session, and again in a long one), installed by `scripts/install.sh`; either can be left out (§4.10) | A skill alone waits for the agent to think of it; at session end the agent can no longer act |
| Setup report | `kb doctor` (and so `install.sh`) prints one TOON document after AXI: counts, a row per check, next steps for the user (§4.10) | Agents run `kb doctor` and must tell me what to do; their sandbox can't do setup |
| Wiki ingest | Notes first, manual (`kb ingest`) and on a timer, onto a review branch. The model answers typed questions about each note and `kb` files the note's text; `codex` first, a local decision model nightly later. `kb prune` adds cited summaries on a trigger and never removes a claim (§4.7) | Review gate before anything becomes canon; no model text reaches a claim, a small model can run the same job, and a bad summary can be thrown away |
| Live panel | Rules detect, small model only phrases and extracts decisions | Rules measurably work; LLM judgement alone did not |
| OpenHands | Not used; event model borrowed | It cannot observe IDE sessions |

## 3. Architecture

```
 Claude Code · Codex · Copilot CLI · Cursor CLI · [Cursor IDE, VS Code Copilot Chat: phase 3]
        │ each agent writes its own files (~/.claude, ~/.codex, ~/.copilot, ~/.cursor)
        ▼
 ┌──────────────────────────────┐
 │ agentsview  127.0.0.1:8080   │  file watch + 1.5 s poll → SQLite archive (raw layer)
 │ REST /api/v1 · SSE · MCP     │
 └──────────────┬───────────────┘
                │ adapter (REST + watch); Cursor CLI: own reader of ~/.cursor/chats (§4.1)
 ┌──────────────▼───────────────────────────────────────────────┐
 │ fabricd (Python)                                              │
 │  tracker ── session lifecycle (active → idle → finalized)     │
 │  observer ─ rules ─┬─> panel  127.0.0.1:8090 (SSE web page)   │
 │                    └─> llama-server 127.0.0.1:8091 (SLM)      │
 │  digester ─ extract → redact → gitleaks gate → commit         │
 │  state.db (loops, cursors, ledger) · index.db (FTS5)          │
 └──────────────┬───────────────────────────────────────────────┘
                │ git
 ┌──────────────▼───────────────┐       ┌───────────────────────────┐
 │ wiki repo (~/wiki, private)  │<──────│ kb ingest (manual/timer)  │
 │ raw/ digests · wiki/ pages   │ branch│ codex exec | local model  │
 └──────────────────────────────┘       └───────────────────────────┘

 Query: kb CLI / `kb mcp` (wiki, digests, loops, notes)  +  `agentsview mcp --profile memory` (raw transcripts)
 Notes: agents run `kb note` (skill `kb-note`) → ~/.local/share/agent-fabric/inbox/ → kb inbox · search · ingest
```

One long-running Python daemon (`fabricd`) hosts tracker, observer, digester and panel. Ingest is a separate
short-lived job. The `kb` CLI and `kb mcp` read the same local files. `kb note` writes to the inbox directly; it
needs neither agentsview nor fabricd (§4.10).

## 4. Components

### 4.1 Capture: agentsview

- Built once per version on CI from a pinned tag (below) and installed as a checksummed binary. Upgrade
  deliberately: bump the pin, CI rebuilds, the adapter contract tests pass, then release it with a manual run.
- Run sandboxed under systemd (below): `agentsview serve --no-browser --no-update-check --port <port> --replace`.
  `<port>` is the one port set in `config.toml` (§5): 8080, or another when 8080 is taken on the desktop. The unit's
  `--port` and every client's `--server http://127.0.0.1:<port>` use it. The port is explicit because without
  `--port` agentsview moves to the next free port when it's taken (seen on the desktop in phase 0) and its clients
  miss it; with it, the unit fails and retries every 30 s. `--replace` because otherwise `serve` defers to a running
  daemon of the same version (prints "already running", exits 0) and the unit stays down while that unconfined
  daemon captures.
- Sources used (defaults verified in its source and with `agentsview doctor sync` on the devbox): Claude Code
  `~/.claude/projects` (subagent transcripts in `<session>/subagents/`), Codex `~/.codex/sessions` +
  `archived_sessions`, Copilot CLI `~/.copilot`, Cursor CLI `~/.cursor/projects` (the thin transcripts only; see the
  capture check below), VS Code Copilot Chat `~/.config/Code/User` (workspaceStorage `chatSessions` + global
  empty-window chats), Cursor IDE `state.vscdb` (override `CURSOR_IDE_DIR` if needed).
- Do **not** install its Memory plugin: it adds a session-start hook that injects memory.
- Its SQLite archive is the raw layer and, over time, the only full copy (vendors prune and migrate their files).
  Back it up with SQLite's online backup (`sqlite3 … ".backup"`) or its DuckDB/PG push, never by copying the
  DB/WAL files. The archive contains Cursor's `state.vscdb` content, which includes auth tokens: keep it local.

**Build (CI).** `.github/workflows/build-agentsview.yml` builds agentsview once per pinned version; nothing is
compiled on the desktop. `vendor/agentsview.pin` holds the tag and commit. A PR that changes the pin, the vet files
or the workflow runs steps 1–4, and so does the same change on `main`. Step 5 runs only in a manual run on `main`
(`gh workflow run build-agentsview.yml`), so nothing is released without a deliberate action. A weekly run repeats
step 2 alone, so a dependency of the installed version that is later found to be malware or critical still shows up.
All actions are pinned by commit SHA.

1. Check the pin: the tag must still point at the pinned commit (`git ls-remote`), and upstream's `CI` workflow
   must have passed on that commit. That workflow runs lint, Go tests on Linux, Windows and macOS (plus the race
   detector), PostgreSQL and ClickHouse integration, frontend tests and e2e. Its e2e server is built with the same
   `fts5,kit_posthog_disabled` tags as ours, so we don't rerun the suite. Then check out `kenn-io/agentsview` at
   that commit.
2. Dependency gate: SafeDep `vet` v1.20.0 over `go.mod` and `frontend/package-lock.json`. vet is Apache-2.0 and
   is installed from its release after a SHA-256 and attestation check. It runs in community mode (no account) and
   sends package names and versions to SafeDep, no code. Its telemetry is off (`VET_DISABLE_TELEMETRY`). The gate
   fails on:
   - known malware (`--malware-query`). SafeDep's database covered 265 of the 564 npm packages in v0.45.0, so
     this catches known cases only;
   - lockfile poisoning, meaning a package resolved from an unexpected URL. v0.45.0 has two on purpose, the `vite`
     npm alias and the commit-pinned `@kenn-io/kit-ui`; `.github/vet/agentsview-trusted-urls.txt` trusts those
     exact URLs;
   - the policy in `.github/vet/policy.yml`: a critical vulnerability, or a version published less than 14 days
     ago.

   Exceptions go in `.github/vet/exceptions.yml`, each with the advisory, why it doesn't apply, and an expiry of at
   most 30 days. An exception skips that version in every check, malware included. Run against v0.45.0 on
   2026-10-23, the gate needed one: `dompurify` 3.4.16 (GHSA-cg2g-xh64-v4q7), which only affects the
   `SAFE_FOR_TEMPLATES` option; agentsview calls `DOMPurify.sanitize` with the default config.
   `go.kenn.io/kit` v0.33.0, published with agentsview v0.45.0 on 2026-10-09, cleared the 14-day rule that day.
3. Build. The frontend builds in its own job (Node 24, `npm ci --ignore-scripts && npm run build`). The binary
   builds in `golang:1.27.0-bookworm`, pinned by digest: the go-sqlite3 header and pricing-snapshot steps from
   upstream's Dockerfile, then `go build -tags fts5,kit_posthog_disabled -trimpath …`, then the glibc check below.
   By hand on 2 vCPU this took about 7 minutes; the binary is 145 MB (DuckDB is linked in statically).
4. Smoke test on GitHub's `ubuntu-24.04` runner, the minimum desktop (`.github/scripts/smoke-agentsview.sh`):
   `agentsview --version`, then `serve` on a throwaway home and data dir until `GET /api/v1/sessions` answers.
5. Attest the binary (`actions/attest-build-provenance`; available because this repo is public) and publish the
   release `agentsview-<tag>` with the binary, agentsview's `LICENSE` (MIT requires the notice to travel with the
   binary) and `SHA256SUMS`. An existing release is never replaced. On the desktop,
   `scripts/install-agentsview.sh` installs it: `gh release download`, `sha256sum -c`, then
   `gh attestation verify` limited to this workflow on `main`.

**glibc.** The binary uses cgo (SQLite, DuckDB) and links glibc and libstdc++ dynamically, so it runs only on
distros at least as new as the build image. The devbox build needs `GLIBC_2.43` and would not start on Ubuntu 24.04
(2.39). The minimum desktop is **Ubuntu 24.04** (glibc 2.39, libstdc++ up to `GLIBCXX_3.4.33`), so the build job
runs in `golang:1.27.0-bookworm` (glibc 2.36, GCC 12), pinned by digest. It also fails if `objdump -T` shows a
`GLIBC_` version above 2.39 or a `GLIBCXX_` version above 3.4.33, so a base-image bump can't raise the floor
unnoticed. Options considered:

| Option | Runs on | Cost |
|---|---|---|
| **Build in an old-enough container** (chosen): `golang:1.27.0-bookworm`, the base of upstream's own Dockerfile | glibc ≥ 2.36: Ubuntu 24.04+, Debian 12+, Fedora 37+ | none |
| Build in `ubuntu:24.04` + Go tarball | Ubuntu 24.04+ | more CI setup, no gain over bookworm |
| Fully static binary (musl or static glibc) | any Linux | DuckDB is a prebuilt glibc C++ library with no build tag to leave it out in v0.45.0, so this means patching agentsview; not worth it |
| Upstream release binary, checksum-verified | whatever upstream targets | loses the compile-time telemetry removal and our `vet` gate on what runs; needs `AGENTSVIEW_TELEMETRY_ENABLED=0` |

The `kit_posthog_disabled` tag compiles telemetry out; v0.45.0 ignores `DO_NOT_TRACK`.

The same `vet` gate (`.github/workflows/vet.yml`) runs on this repo's own dependencies on every PR, every push to
`main` and weekly: the actions its workflows use now, and `uv.lock` once the Python core has one (vet parses
`uv.lock` directly).
Snyk can join as a second scanner: per its docs, the free plan allows unlimited tests on public repositories. It
needs an account and a `SNYK_TOKEN` secret, and whether `snyk test` runs from a public repo's CI count as public
needs checking on the first run (Settings → Usage).

**Outbound calls** (v0.45.0, observed with egress blocked):

| Call | When | Switch |
|---|---|---|
| PostHog `us.i.posthog.com` | startup and daemon pings, even with `DO_NOT_TRACK=1` | `kit_posthog_disabled` tag, or `AGENTSVIEW_TELEMETRY_ENABLED=0` |
| Pricing JSON from `raw.githubusercontent.com` (LiteLLM, genai-prices) | at start, then every 24 h | none; uses the embedded snapshot when blocked |
| Update check (`github.com/…/releases`) | update-check endpoint | `--no-update-check` |
| Gist export (`api.github.com`, token from `gh auth token`) | only when used from the UI | blocked by the sandbox |

The web UI can also start processes: resume a session in a terminal, open an editor, or run an agent CLI to
generate insights. We use none of these, and inside the sandbox they would be confined too.

**Sandbox.** The source-built binary ran unchanged in Claude Code's bubblewrap sandbox, with no network and writes
limited to its data dir. Initial sync and the REST API worked, appended Codex lines showed up through the API in
0.2–5 s, and the blocked calls failed quietly. On the desktop the unit confines it:

```ini
[Service]
ExecStart=/home/<me>/.local/bin/agentsview serve --no-browser --no-update-check --port <port> --replace
Environment=AGENTSVIEW_TELEMETRY_ENABLED=0
Restart=on-failure
RestartSec=30
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/<me>/.agentsview
InaccessiblePaths=-/home/<me>/.ssh -/home/<me>/.gnupg -/home/<me>/.config/gh -/home/<me>/.codex/auth.json
PrivateTmp=yes
NoNewPrivileges=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
IPAddressDeny=any
IPAddressAllow=localhost
```

`IPAddressAllow=localhost` keeps the API reachable on loopback for fabricd and the browser. A network namespace
(bwrap `--unshare-net`, `PrivateNetwork=`) would hide it. The spike decides between a user unit and a system unit
with `User=`. A user unit gets the filesystem settings only through unprivileged user namespaces, which recent
Ubuntu restricts with AppArmor, and the IP filter needs cgroup BPF, which a user manager may not be able to attach.
If `systemd-run --user --wait -p IPAddressDeny=any curl -sI https://example.com` succeeds, or
`systemd-run --user --wait -p ProtectHome=read-only touch ~/probe` succeeds, the filter is not enforced. In that
case install a system unit with `User=` (root once). On the devbox (Ubuntu 26.04, systemd 259, AppArmor
restricting unprivileged user namespaces) a user unit with these settings starts but enforces neither: both probes
succeed, so expect a system unit on the desktop too. Two more spike checks: Cursor's `state.vscdb` is opened
`mode=ro` without `immutable`, so if it is in WAL mode SQLite may need its `-shm` file under a read-only home; and
the list of Copilot and Cursor credential files to add to `InaccessiblePaths`. Cursor's own `state.vscdb` holds
tokens too, but agentsview has to read it.

**Capture check (CLI agents, devbox, 2026-10-31).** agentsview v0.45.0 ran `sync` over the real home folders with
its own data dir, inside Claude Code's sandbox, so the agents' folders were read-only as under the unit. Existing
sessions were compared with each agent's own files, counts only. Then one test session per agent (a greeting, a test
run that fails, an edit that fails) was compared with the agent's own record; Codex and Cursor ran interactively in
tmux as well as headless.

| Agent (version) | Sessions archived | Tool calls and results | Failure status | Gaps |
|---|---|---|---|---|
| Claude Code 2.1.316 | all: 1,359 sessions + 4,960 subagent transcripts | 75,541 of 75,637 calls; the rest were written after the snapshot, or are a resumed session's copies of earlier messages, filed under the original | `errored` | Read and Glob results are stored as length and digest only, failed ones included |
| Codex 0.162.1 | all: 72 `codex exec` + 1 interactive | 784 of 784, with output | none: a failed script is `completed` | tools are code-mode scripts (§4.2) |
| Copilot CLI 1.0.105 | the test session (`-p`) | all, with output | `errored` on the failed edit | the error text stays in `events.jsonl` |
| Cursor CLI 2026.10.22 | **none** under a read-only home | calls only, no results | none | see below |

- **Headless runs.** agentsview files `codex exec` (`session_kind = non-interactive`) and `claude -p`/SDK runs
  (`entrypoint = sdk-cli`) as `relationship_type = subagent` with no parent, so the session list hides them unless
  `include_children=true`, and `is_automated` is 0 for all of them. `copilot -p` sessions are filed as ordinary
  sessions. Neither Copilot nor Cursor records anything found so far that marks a headless run. The rule is in §4.3.
- **Cursor CLI.** It writes two records per chat: a thin `~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl`
  (role and content parts; no tool results, ids or timestamps) and the full `~/.cursor/chats/<hash>/<id>/store.db`
  (tables `blobs` and `meta`, `journal_mode=delete`), next to a `meta.json` with `cwd`, `createdAtMs`, `updatedAtMs`
  and `hasConversation`. agentsview parses the transcript but also opens `store.db`, and when `~/.cursor/chats` is
  read-only the whole session fails (`1 failed`, `local sync processing incomplete`). The unit's
  `ProtectHome=read-only` has the same effect. With write access it archives the prompts, replies and tool calls, but
  no tool results: the failed edit and two commands the allowlist rejected are invisible. Only 2 of 8–10 messages get
  a timestamp and 1 gets reasoning, in `-p` and interactive sessions alike. `store.db` holds the whole conversation as
  plain JSON blobs shaped like AI SDK messages: `system`, `user`, `assistant` with `reasoning`/`text`/`tool-call`
  parts, and `tool` with `tool-result` parts, rejections included. The other blobs are protobuf, and `meta` key `0`
  is hex-encoded JSON (`agentId`, `latestRootBlobId`, `createdAt`, `mode`, `isRunEverything`, `blobEncryptionKey`).
  The JSON blobs carry no timestamps. v0.46.0 and v0.46.1 change none of this. Cursor CLI therefore gets its own
  `CaptureSource` (Adapter, below) that opens `store.db` read-only, rather than write access for agentsview to
  Cursor's data; the bug also goes upstream.
- **Upgrade.** v0.46.0 (2026-10-23) reads Codex rollouts after Codex compresses them to `.jsonl.zst` (none on the
  devbox yet), and keeps archived Claude Code tool output after Claude Code deletes its tool-result files. It clears
  the 14-day rule on 2026-11-06; bump the pin then.
- **Cost.** The first full sync of 6,391 sessions took 5 min 39 s on 2 vCPU and made a 497 MB archive; incremental
  syncs take about 1 s.

**Query path.** `agentsview mcp` talks to the daemon over HTTP, and if none answers it starts one itself, outside
the sandbox. Register it with `--server http://127.0.0.1:<port>` (§4.1) so it fails instead. MCP servers are started
by the agent app rather than in the agent's shell sandbox, so they reach loopback. A CLI call from a sandboxed shell
cannot: Codex `workspace-write` has no network. This is why `kb` reads files instead of calling a daemon. `index.db`
uses `journal_mode=DELETE` so read-only readers inside agent sandboxes never need to create a `-shm` file.

**Adapter.** All agentsview access goes through one module with this surface:

```python
class CaptureSource(Protocol):
    def active_sessions(self, since: datetime) -> list[SessionMeta]: ...
    def watch(self, session_id: str) -> AsyncIterator[None]: ...           # "changed" ticks
    def messages(self, session_id: str, from_ordinal: int) -> list[Message]: ...
    def session(self, session_id: str) -> SessionMeta: ...
```

Implemented over `GET /api/v1/sessions?include_one_shot=true` (the default list hides sessions with one user
message), `GET /api/v1/sessions/{id}`, `GET /api/v1/sessions/{id}/messages?from=N`
and `GET /api/v1/sessions/{id}/watch` (SSE; emits a change signal, not content). A contract test runs the adapter
against recorded responses validated with the pinned `openapi.yaml`. If agentsview fails for one agent, that agent
gets its own `CaptureSource` (direct transcript reader) without touching the rest.

Cursor CLI is the first such agent. `CursorChatSource` lists `~/.cursor/chats/*/*/meta.json`, watches each
`store.db`, opens it read-only and decodes the JSON message blobs; `cwd` and the times come from `meta.json`, the
project from `cwd` (§4.4). Still to work out from synthetic sessions: message order from the blob tree
(`latestRootBlobId` and the protobuf blobs; row order matched the conversation in the check, which isn't a
guarantee), timestamps (the blobs have none, so the watcher stamps each message when it first sees it), and whether
`blobEncryptionKey` means some blobs are encrypted (none of the JSON ones were).

Verify in spike: what `/api/v1/events` emits (could replace session-list polling), and end-to-end lag for Cursor
IDE (agentsview re-reads some `state.vscdb` changes only on a scheduled reconcile).

### 4.2 Normalized event model

Borrowed from OpenHands/Pi: typed, append-only, addressed by `(session_id, ordinal)`.

| Field | Source (agentsview) |
|---|---|
| `session_id` | `DbSession.id` (prefixed, e.g. `cursor-ide:…`) |
| `agent`, `project`, `cwd`, `git_branch`, `parent_session_id` | `DbSession` |
| `ordinal`, `ts`, `role`, `model` | `DbMessage` |
| `text`, `thinking` | `content`, `thinking_text` |
| `tool_calls[]`: `tool`, `category`, `input`, `result` (full output), `result_status` | `ToolCall.result_content` (stored in full, except Claude Code's Read and Glob results, §4.1) + `result_events[].status` (`completed`, `errored`, `running`, …) |
| `kind` (derived) | `user` · `assistant` · `shell` · `edit` · `read` · `search` · `other` |
| `command`, `exit_code` (derived, shell only) | parsed from `input_json` / `result_content` per agent |
| `failed` (derived) | `result_status = errored`, a non-zero `exit_code`, or an error pattern in the result (traceback, `error:`, `FAILED`, `permission denied`, patch did not apply, `Script failed`, `apply_patch verification failed`, `Failed to apply patch`) |

The per-agent mapping of tool names to `kind` and of shell results to exit codes lives in a small table per agent,
covered by fixture tests. Sessions with `is_automated = true` are ignored; headless runs follow §4.3.

Codex 0.162 runs every tool through one code-mode tool, `exec`, whose input is a JavaScript program that calls
`tools.exec_command({cmd: …})` or `tools.apply_patch("*** Begin Patch …")`. agentsview files both as `exec`, category
Bash. The Codex mapping takes `kind` and `command` from the calls inside the script, and a result that starts with
`Script failed` counts as failed.

Failed calls keep the output agentsview stores. In v0.45.0 it sets `errored` for Claude Code and Copilot CLI, but
not for Codex or Cursor, so failure also comes from the exit code and the error patterns. Agents often append
`; echo EXIT:$?` to a command (Copilot and Cursor both did in the capture check), so the shell's own exit code is 0;
the error patterns still catch the failing test. Copilot stores a failed call's status but not its error text, so
the adapter takes that text from the session's `events.jsonl` (`tool.execution_complete`, `error`). The digest
stores an excerpt and the message ordinal (§4.4); `kb show <session>#<ordinal>` or `agentsview mcp` `get_messages`
return the full output from the local archive.

### 4.3 Session lifecycle

```
 first seen ──> ACTIVE ──(no new messages for 30 min)──> FINALIZED ──(new messages)──> REOPENED ──> … FINALIZED
                  │ every turn end: refresh draft digest (local only)
```

- **Why a timeout:** no agent gives a reliable session-end signal (§2), so "finished" means "quiet for N
  minutes". The value is low-stakes: too short and a paused session finalizes and later reopens (one extra commit
  to the same file); too long and its digest arrives late. Config value, default 30 min.
- `tracker` lists sessions updated in the last 24 h every 10 s, with `include_children=true` because agentsview files
  headless runs as children (§4.1), and opens a `watch` for each ACTIVE one.
- **Headless runs** (`claude -p` and SDK runs, `codex exec`, `copilot -p`, `cursor-agent -p`) get a local digest
  only, indexed for `kb` and never committed, unless the project is also on the `[publish] headless` list (§4.5).
  Scripts and benchmarks make them in bulk: 1,324 of the 1,359 Claude Code sessions on the devbox are headless, most
  from one benchmark project. Detection: `entrypoint = sdk-cli` (Claude Code) and `session_kind = non-interactive`
  (Codex). Copilot and Cursor show no marker yet (§4.1), so their `-p` runs count as interactive until one is found.
- **Turn end** (heuristic, verify in spike): last message is assistant text with no pending tool call, and no
  change for 5 s.
- **Finalize**: write the digest, commit it. A reopened session rewrites the same digest file on its next
  finalization (new commit). The digest is a pure function of the session's messages + git snapshots, so
  regeneration is byte-stable.
- Per-session cursor (`last_ordinal`) is persisted in `state.db`, so restarts resume without reprocessing.

### 4.4 Digest

Path: `raw/sessions/<project>/<yyyy>/<mm>/<yyyy-mm-dd>-<agent>-<shortid>.md`. Machine-written only; humans and the
ingest LLM never edit it.

```markdown
---
schema: fabric.digest/1
session: cursor-ide:7f3a9c…
agent: cursor-ide
project: billing-api
cwd: ~/code/billing-api
branch: feat/retry
worktree: {path: ~/.codex/worktrees/a1b2/billing-api, main: ~/code/billing-api}   # null when not a linked worktree
head: {start: 1a2b3c4, end: 9d8e7f6}
time: {start: 2026-10-22T09:14:03Z, end: 2026-10-22T10:02:41Z, active_min: 41}
models: [...]
outcome: {last_tests: failed, uncommitted_changes: true, ends_with_question: false}
counts: {prompts: 6, tool_calls: 48, commands: 17, edits: 12, errors: 3}
files_modified: [src/retry.py, tests/test_retry.py]
loops: {opened: [L-0142], closed: [L-0137]}
revision: 2
---
## Prompts            verbatim, timestamped; pasted blocks over 40 lines collapsed to head/tail 10 + pointer
## Commands           table: command · exit · kind (test/build/lint/git/install/other) · first error line on failure
## Tests              runner, pass/fail/skip counts, failing test names (pytest, jest/vitest, go test, cargo test)
## Errors             one entry per distinct error (content hash), last 20: tool or command, exit/status,
                      first error line + last 30 lines of output, ordinal of the full output
## Changes            `git diff --stat` start→end, compact diff capped at 200 lines
## Commits            sha + subject made during the session
## Markers            TODO/FIXME/HACK, skips, lint suppressions added in the diff
## Open loops         opened/closed in this session, with evidence (how loops are identified: §4.8)
## Final message      last assistant message, verbatim
```

Size target ≤ 50 KB, hard cap 200 KB (truncate sections, never drop front matter). No LLM involved. Every
truncation leaves a pointer to the full text in the agentsview archive, which stays local. Typed prompt text is
never cut, because it carries intent. Pasted blocks (logs, stack traces, code, documents) are collapsed: they are
most of the bulk, most of the noise for the ingest LLM, and where accidental secrets and client code end up.

**Git snapshots.** Diff-based fields come from the session's `cwd`, read-only: `git rev-parse HEAD`,
`git diff HEAD --unified=0` and `git ls-files --others --exclude-standard`, taken at first sight and at each turn
end. This is agent-agnostic. Caveat: edits I make by hand in the same tree during a session are attributed to it
(labelled "working tree during session", not "agent wrote").

**Worktrees.** The first snapshot also runs `git rev-parse --path-format=absolute --git-dir --git-common-dir`. If
the two differ, `cwd` is a linked worktree and `main` is the checkout that owns the common dir. agentsview already
maps known worktree layouts (Codex, Claude Code, Conductor, …) to the main repo's name for `project`, but stores no
worktree flag, so we record it. Snapshots are taken while the session runs, so a worktree deleted afterwards still
has its record.

**Project identity.** Every session is filed under one project id. The id sets the folder
`raw/sessions/<project>/`, the page `wiki/projects/<project>.md`, the allow-list entry (§4.5), and which open loops
the panel carries over when a session starts (§4.8). Folder names are not stable: one repo can be
`~/code/billing-api`, a Codex worktree `~/.codex/worktrees/a1b2/billing-api`, a second clone, or a renamed folder.
The id is the first of:

1. the normalised `origin` remote, which is host/owner/repo without scheme, user or `.git`
   (`git@github.com:me/billing-api.git` → `github.com/me/billing-api`);
2. without a remote, the main checkout's path from `--git-common-dir`;
3. outside git, agentsview's `project`.

Aliases in config (`[projects.aliases]`) merge or split ids by hand. A monorepo is one project. The id is resolved
when the session is first seen and stored with it.

### 4.5 Redaction and commit gate

**Allow list first.** Only sessions whose project is on the allow list reach the wiki repo. Every other session
gets the same digest under `~/.local/share/agent-fabric/local/`: it is indexed for `kb` and shown in the panel, but
never committed. The list starts empty, so nothing is published until I add a project.

```toml
# ~/.config/agent-fabric/config.toml
[publish]
allow = ["github.com/me/agent-fabric", "github.com/me/billing-api"]   # project ids, §4.4
headless = []   # allowed projects whose headless runs (§4.3) are published too
```

For every file written into the wiki repo, in order:

1. Regex redaction: known token shapes (`sk-…`, `ghp_…`, `github_pat_…`, AWS keys, JWTs, `Bearer …`, private key
   blocks), `KEY=value` assignments for secret-looking names, contents of `.env*` files read by tools, email
   addresses, `/home/<user>` → `~`.
2. `gitleaks stdin` on the redacted text. Any finding → write to `~/.local/share/agent-fabric/quarantine/`, show
   a panel notice, do not commit.
3. Write file, regenerate `index.md`, append to `log.md`, `git add <paths>`, commit (`digest: <project> <agent>
   <shortid>`). The repo's own gitleaks pre-commit hook is a second backstop; never `--no-verify`.

Local-only digests go through step 1 as well, so `kb` output never shows raw secrets.

What ingest sends to a hosted model goes through steps 1 and 2 first (§4.7). The steps above cover files written, and
this is another way out of the machine.

Single writer: all commits to `main` go through `fabricd` holding a lock file; ingest writes only to its branch in
a separate worktree.

### 4.6 Wiki repo

One local repo per machine, for all its projects (`~/wiki`), with no remote.

```
AGENTS.md          schema: the wording of ingest's questions, and the rules for `kb prune`'s summaries
index.md           generated catalogue (never hand-edited)
log.md             append-only, one line per digest/ingest (merge=union in .gitattributes)
manifests/...      what each ingest branch took and what became of each note, and its review list
raw/sessions/...   digests (§4.4)
raw/notes/...      agent notes that ingest used (§4.10)
wiki/projects/<project>.md       a project's claims, by kind, under a summary once `kb prune` writes one
wiki/concepts/<slug>.md          a `kb prune` summary citing claims across projects ("pnpm workspace quirks")
wiki/retros/<project>/<date>.md  retro findings (§4.7)
wiki/archive/...                 pages retired by pruning (§4.7)
```

Pages are made of claims: a note's own text with its evidence, not prose.

```markdown
### `pytest` hangs in the sandbox
`pytest` hangs in the sandbox: the DB fixture needs a socket; run `-m 'not db'`.
- source: n1a2b3c (Codex, 2026-11-20) · anchor: tests/conftest.py@9d8e7f6 · verified: 2026-11-20 · supersedes: none
```

`kb` files each claim by the model's answers (§4.7). A `supersede` moves the old claim to the page's `## Superseded`
section, with a pointer to what replaced it. A `conflict` keeps both claims side by side until I decide, and a note that
adds to a claim is filed below it. Each links to every claim it's about. A claim says only what its note said, and nothing rewrites it, so whether it still
holds shows only in its anchor and `verified` date (§4.7's lint).

A page may open with a `## Summary` written by `kb prune`, marked as such, with every sentence citing note ids (§4.7).
The claims stay the record, so deleting a summary loses nothing.

Every claim in `wiki/` links to the note or digest it came from. Per-directory entry counts stay small through the
year/month sharding. The wiki only grows, since no claim is ever removed; §4.7's lint and prune keep it readable.

### 4.7 Ingest, lint and prune, retro

`kb ingest [--project P] [--backend codex|local] [--dry-run]`, run by `fabric-ingest.timer` (§5). It starts with notes;
digests join when they exist (§11).

The model only decides. It answers typed questions about a note, as a decision model does, and `kb` acts on the answers
and files the note's own text as the claim. No model text reaches a claim, so this step can't make anything up, and a
small local model can run the same job as a hosted one: the move changes one setting. Pages read as filed notes until
`kb prune` writes their first summary, and connections between notes appear only then.

1. **Gate, no model.** Run `kb lint` first, since claims go stale while the inbox is quiet. Resolved notes, and notes
   with an action on a merged manifest, move to `inbox/done/` (§4.10), and so do ideas open for over 30 days. Then
   count the open `gotcha`, `fix`, `decision` and `idea` notes of allowed projects with no outcome on any manifest.
   Under 3, with the oldest under 7 days: log it and exit 0. An empty run is a success.
2. **Manifest.** Work in worktree `~/.local/share/agent-fabric/ingest-wt` on branch `ingest/<yyyy-mm-dd>`, or on an
   unmerged ingest branch if there is one. The run takes the open notes of those kinds and projects with no outcome on
   this branch, so a note that failed on a merged branch gets one more try. Commit their ids and the base commit. A
   deleted, unmerged branch returns its notes to pending.
3. **Evidence, no model.** For each ref of each note: did it exist at the note's commit, does it exist now, has it
   changed since, and an excerpt. It reads only git objects of the note's own project, and no `.env*` file (§7). A file
   is redacted and scanned whole before it's cut to an excerpt, since a cut can hide what makes a secret recognisable,
   such as a private key's end marker. A file gitleaks still flags sends no excerpt, and the review list names it. Add
   candidate claims that share the note's words, and any such lines of the project's `AGENTS.md`, to show what's
   already recorded.
4. **Input gate.** Redaction and gitleaks (§4.5) over all of that and the note's other fields, before a hosted model
   sees it. A hit quarantines that note, and the others go on.
5. **Typed questions, per note.** Each is narrow, because decision models read a question literally, and code combines
   the answers.
   - For each ref's excerpt: does it support the note, contradict it, or say nothing?
   - For each candidate claim: is the note the same fact, does it add to the claim, replace it or contradict it, or is
     it unrelated? Contradicting means both can't be true, not that one is newer.
   - For each matching line of the project's `AGENTS.md`: does the note state the same fact?
   - Would a future agent plausibly act better for knowing this?
   - How sure are you of these answers? Review goes by this, since `codex` gives no probabilities.
6. **`kb` acts.** It turns the answers into one action: `skip`, `add`, `duplicate`, `extend`, `supersede` or `conflict`.
   A ref that contradicts the note means `skip`, so a false alarm can't replace a good claim. A ref git can't find
   proves nothing, since the file may be untracked or on another branch, and neither does one whose checkout has gone:
   the note is decided as usual, with no anchor if no other ref has one, and the review list marks it. The claim goes
   under its kind's section on `wiki/projects/<project>.md`. It records the action in the manifest, copies the note to
   `raw/notes/` and regenerates `index.md` and `log.md`. An invalid answer that fails again on one retry gets no action,
   goes on a list for review and reaches no page.
7. **Output gate.** `kb lint` and §4.5 over every changed file, then commit on the branch, with each note's outcome in
   the manifest: its action, or why it failed. A run that dies before this commit leaves no outcomes, so the next run
   drops its uncommitted work and takes those notes again.
8. **Review.** I review in tuicr, about weekly, with the note and the answers beside each action, least certain first,
   and mark each action right or wrong (§10). A note resolved after it was filed heads the list, and I take its claim
   off the branch by hand if it no longer holds, since `kb` never removes a claim.
9. **Merge.** The next run finds the merged manifests and moves the notes with an action to `inbox/done/`, so notes need
   no ledger in `state.db`. A quarantined note, or one with no valid answer, stays in the inbox until it's resolved.
   It's tried once on each later branch, and doesn't count towards the gate.

The model sees one note, its evidence and its candidate claims, and has no tools. It treats the note as one agent's
claim, since the first inbox held a false alarm (2026-11-02). Checked with Codex 0.166.0 (2026-11-07): run as the
`codex` backend runs it, and told to `cat` a file holding a random word, it said it would read the file, made no tool
call, and couldn't tell what the file held. With `code_mode_host` off, its code mode, through which every tool runs
(§4.2), fails closed. A new Codex feature can add a tool, so the features turned off are checked on each upgrade.

**Backends.**

- `codex`, first, on the daily timer (§5): no tools, with a schema that allows only the questions' options. Expect
  about a dollar a month.
- `local`, nightly once it passes §10: a decision model on `llama-server`'s `/v1/systemone`, on loopback. Nightly runs
  append to one branch, so review stays weekly. These models are small enough for an 8 GB machine, and should take
  seconds per note on a CPU, which still has to be measured. Speed and RAM aren't the constraint; whether the decisions
  are right is. A generative model limited to the same options is the baseline to beat.

**What differs from Karpathy's gist.** The layers, the three operations, `index.md` and `log.md` are his. Three things
change:

- His ingest writes summaries and "might touch 10-15 wiki pages". Here it files one note and writes nothing, since these
  sources are 500 characters already written for a reader without context, and repeated LLM rewriting made memory worse
  than none in one study (arXiv 2605.12978). Writing waits for `kb prune`, on a trigger, and adds to the claims rather
  than replacing them.
- His raw sources are "your source of truth", and he reads each ingest ("I read the summaries, check the updates, and
  guide the LLM"). Notes are unchecked claims and the job runs unattended, so the evidence step and the review branch
  stand in for both.
- His lint never re-checks a page against its own source (below).

**Reading.** Agents read pages directly until `kb search` exists, and nothing is injected. The `kb-note` skill tells
them to check a claim's anchor before relying on it, as Copilot Memory does (§4.10), and to treat a summary as a pointer
to its claims.

**Lint and prune.** Without these the wiki becomes a long list of stale and duplicate claims. `kb lint` follows the
"lint" operation in Karpathy's gist, and `kb prune` does the writing his ingest does:

- `kb lint` (deterministic; on every ingest run, even when the gate exits, and in `kb doctor`, which is where its
  findings show when no branch is waiting for review) reports broken links, claims citing missing notes or digests,
  claims whose anchor file changed since `verified`, a `supersedes` pointing at nothing, a summary citing a missing
  claim, orphan pages, oversized pages and near-duplicate claims, compared by their whole text: a title is the first
  sentence cut at 80 characters, and on the first real run (Codex CLI 0.166.0, 2026-11-12) two notes that opened with
  the same long phrase had close titles and different claims. Shared words count against the shorter text, so a note
  repeated inside a longer one still matches. Karpathy's lint compares pages with each other and with newer sources,
  and the failure people report is a page "confidently wrong for months" that passes those checks. A changed anchor is
  the check that looks at the source, and it replaces a 90-day age check. A contradiction can't be found
  deterministically, so it appears only as a `conflict` or a `supersedes` from ingest.
- `kb prune` (manual + weekly timer) is the only pass that writes. It does nothing unless a page has 10 claims and no
  summary, 10 new claims since its summary, or a summary citing a claim that has since been superseded. Then, on branch
  `prune/<yyyy-mm-dd>`, a generative model with no tools (`codex` first) reads the page's claims and writes a
  `## Summary` above them, and `kb` refuses any sentence that cites no claim on the page. It can also write a
  `wiki/concepts/` page, which holds only a summary citing claims across projects, split an oversized page by moving
  claims unchanged, and move pages of projects inactive for 6 months to `wiki/archive/`. It never rewrites or removes a
  claim. It goes through the same review and merge as ingest.

Raw digests are never pruned. Git history keeps them anyway, so deleting would not shrink the repo; `kb search`
ranks old digests lower instead.

**Retro.** `kb retro [--session S | --project P --since 14d]`, manual (a weekly timer later if useful). It is
modelled on Matt Pocock's `retro` skill (mattpocock/skills, MIT, still in its in-progress folder). That skill reads
a session and proposes changes to the agent's *environment*, not the code: navigation pointers, automated checks
that would have caught a mistake (an existing check that is unwired or broken comes first), coding standards,
AGENTS.md cleanup, tool economy, instructions that change nothing, and missing information access. Our version also
starts from deterministic pre-findings across sessions, which a single-session retro cannot see:

- the same error or failing test in several sessions;
- my corrections: prompts that start with "no", "don't", "stop", "I said", "again". Repeated corrections point to a
  missing instruction or check;
- commands that keep failing the same way (a wrong test command means a navigation or AGENTS.md fix);
- `docs-drift` flags (§4.8) and loops open longer than 14 days;
- bypassed checks (`safety-bypass`, `lint-suppressed`) more than once.

The LLM (same backend as `kb prune`) turns these into candidates ordered by severity, each citing digests, and writes
them to `wiki/retros/<project>/<date>.md` on the ingest branch. Proposed edits to a project's AGENTS.md, CI or lint
config appear as diffs on that page, and I apply them myself in the project repo. Nothing changes agent
instructions automatically.

### 4.8 Observer and live panel

**Pipeline per session:**

```
change tick → fetch messages from last ordinal → normalize → rules (sync) ──> flags → panel (immediately)
                                         └─> batch buffer ──(turn end | ≥15 events | ≥1,200 tokens)──> SLM ──> bullets → panel
```

**Rules** (each emits a flag with rule id, file/line or command, evidence ordinal):

| Rule id | Detects | Opens loop |
|---|---|---|
| `test-disabled` | added `@pytest.mark.skip/xfail`, `pytest.skip(`, `it/test/describe.skip(`, `xit(`, `@Disabled`, `@Ignore`, `t.Skip(`, `#[ignore]` | yes |
| `test-focused` | added `.only(`, `fit(` | yes |
| `test-weakened` | removed assert/expect lines without replacement, `==`→`>=` in asserts, test deletion, snapshot update flags, narrowed test command (`-k`, `--deselect`) | yes |
| `lint-suppressed` | added `# type: ignore`, `# noqa`, `eslint-disable`, `@ts-ignore`, `#[allow(…)]` | yes |
| `debt-marker` | added `TODO/FIXME/HACK/XXX`, `NotImplementedError`, stub returns | yes |
| `safety-bypass` | `--no-verify`, `HUSKY=0`, `SKIP=`, `git push --force` (not `--force-with-lease`) | yes |
| `destructive-cmd` | `git reset --hard`, `git clean -fd`, `rm -rf`, `curl … \| sh`, `chmod -R 777` | no |
| `ci-config-edit` | edits to `.github/workflows/**`, `.pre-commit-config.yaml`, lint/type/coverage configs | no |
| `secret-candidate` | gitleaks hit on added hunks | no |
| `claimed-success` | final message claims done/tests pass while last test run failed, skipped, or never ran after the last edit | yes |
| `docs-drift` | at finalization: the diff adds or changes CLI options (`add_argument(`, `@click.option`, `typer.Option`, cobra flags), env var reads, or config/OpenAPI schema files, and no `README*`, `docs/**`, `AGENTS.md` or `CHANGELOG*` changed | yes |

Rule inputs: shell commands from tool calls; added/removed lines from the git snapshot delta (§4.4). Sources to
port patterns from: eslint-plugin-jest `no-disabled-tests`, Sonar RSPEC-1607, dcg, code-supervisor, agentsview's
`internal/signals` heuristics.

**Open loops: how they are identified.** Only a rule marked "opens loop" above opens a loop. Detection is
therefore deterministic: a rule fires on an added or removed line in the git snapshot delta, on a shell command, or
on the final message. It never fires on the model's reading of the transcript. Each loop has the key
`(project, rule, subject)`:

| Rule | Subject | Closes when |
|---|---|---|
| `test-disabled`, `test-focused` | `path::test_name` (enclosing test function, or `it`/`test` title) | the marker is gone from that test in a later snapshot |
| `test-weakened` | `path::test_name` | dismissal only (needs judgement) |
| `lint-suppressed`, `debt-marker` | `path` + the marked line's text, whitespace-normalised, without line number (lines move) | that text is gone from the file |
| `safety-bypass` | normalised command + branch | dismissal only |
| `claimed-success` | session id | a later test run in the project passes |
| `docs-drift` | project + changed surface (option name, env var, schema file) | a later session changes docs, or dismissal |

When the same key shows up again, from any session or agent, the new evidence attaches to the existing loop
instead of opening a duplicate. A closed key that reappears reopens its loop. Loops live in `state.db` (`id`,
`key`, `opened_in`, `evidence[]`, `reason`, `status`). `reason` is the agent's stated justification, linked by
the SLM through `flag_reasons`. It must be a verbatim quote from the transcript, which is checked by substring
match. The SLM may also *suggest* a loop for a deferral that has no code marker ("I'll add the migration later").
A suggestion shows greyed out in the panel and becomes a loop only when I confirm it. The SLM never opens or closes
loops on its own.

**SLM.** llama-server on the Arc iGPU (SYCL or Vulkan backend, decided by the spike benchmark), Gemma 4 E4B Q4_K_M
(fallback E2B or Qwen3.5-4B), `cache_prompt` on, one request in flight per session, coalesce while busy. If
llama-server is down or slow, the panel runs rules-only.

- Prompt: `[fixed system prompt + schema]` `[state: session goal (first prompt), open loops, last 10 bullets]`
  `[new events, tool output truncated to head/tail 20 lines]`.
- Output, constrained by JSON schema and re-validated with pydantic (llama-server ignores unsupported keywords):

```json
{
  "bullets": [{"kind": "decision|action|risk", "text": "≤160 chars", "evidence": [123, 127], "key": "retry-lib"}],
  "flag_reasons": [{"flag_id": "F-88", "quote": "skipping for now, it's flaky"}]
}
```

- Dedupe: bullets merge by `key` or token-Jaccard ≥ 0.6 against the last 20; evidence lists merge.
- Trust boundary: transcript and tool output are untrusted input. The SLM starts with no tools; read-only tools
  may come later under the limits in §7. Panel renders plain text only.

**Panel UI.** Starlette app on `127.0.0.1:8090` with SSE updates, opened in a browser, VS Code Simple Browser or
Cursor's browser tab. The mock-up below is a placeholder. The real layout is decided in a prototype session in
phase 1, with throwaway versions run against recorded sessions: one page for all sessions or one per session, and
what comes first. If both layouts are cheap to keep, the choice becomes a config switch.

```
┌ billing-api · cursor-ide · active 41 min ───────────────────────────────┐
│ OPEN LOOPS                                                                │
│  ● L-0142 test_retry_after_timeout skipped — "flaky, temporary"  [dismiss]│
│  ○ L-0131 (from codex, 2 days ago) # type: ignore in client.py   [dismiss]│
│ RISKS        ⚠ claimed "all tests pass" — last pytest run: 2 failed       │
│ DECISIONS    switched requests → httpx for async retries                  │
│ ACTIONS      edited 4 files · 9 commands · 2 commits                      │
└───────────────────────────────────────────────────────────────────────────┘
```

When a session starts in a project, the panel shows that project's open loops from earlier sessions, whichever
agent created them. That is for me to read; nothing goes to the agent unless I pull it.

### 4.9 Query: `kb` CLI and MCP

`index.db` (rebuildable from the wiki repo, the inbox and `state.db`): FTS5 tables over digests, wiki pages and notes
(porter tokenizer for prose, trigram for paths/commands/errors), plus relational tables from digest and note front
matter (`sessions`, `files`, `commands`, `tests`, `loops`, `notes`).

| CLI | MCP tool (`kb mcp`, stdio, read-only) | Returns |
|---|---|---|
| `kb catchup [--project .]` | `catchup(project)` | open loops + unresolved `status` notes + latest notes + last 3 digests' summaries + project page excerpt |
| `kb search <q> [--project] [--since] [--kind]` | `search(query, project?, since?, kind?)` | ranked snippets with paths |
| `kb sessions [--file F] [--failing-tests] [--since]` | `list_sessions(...)` | structured session rows |
| `kb show <path\|session[#ordinal]>` | `get_page(path)` | full digest or wiki page; with `#ordinal`, that message's full output from agentsview |
| `kb loops [--all]` | `open_loops(project?)` | loops with evidence |
| `kb loop close <id>` | — | (CLI only; agents can't close loops) |
| `kb note --kind K [--ref R]… "text"`, `kb note --resolve <id>` | — | id and path of the new note (CLI only, §4.10) |
| `kb inbox [--project P \| --all-projects] [--since] [--kind] [--all]` | `list_notes(project?, since?, kind?, all?)` | unresolved notes not yet ingested, under an "unchecked" header, between markers (`--all`: resolved ones too) |
| `kb hook start\|stop --agent A` | — | the hooks' reminder in agent A's format (CLI only, run by the agents' hooks, §4.10) |
| `kb ingest`, `kb lint`, `kb prune`, `kb retro`, `kb reindex`, `kb doctor` | — | maintenance (§4.7) |

Project defaults to the git root of the current directory. MCP results are labelled as historical notes (untrusted
content), mirroring OpenHands' `<UNTRUSTED_CONTENT>` wrapping.

Registration (manual, once): Codex `~/.codex/config.toml` `[mcp_servers.kb]`, Cursor `~/.cursor/mcp.json`, VS Code
user `mcp.json`; each gets `kb` and `agentsview` (`mcp --profile memory --server http://127.0.0.1:<port>`, §4.1).
The manual trigger is me telling the agent "run `kb catchup`" or "check kb for …"; per-agent slash commands can
wrap that later.

### 4.10 Agent notes: `kb note`

Digests record what happened in a session. Some things only the agent notices: the tests on `main` are broken, an
error has a non-obvious fix, a choice was made for a reason that isn't in the code. For those every agent gets a CLI
command and a skill that says when to use it. Neither needs agentsview or fabricd: the command writes a file, and
`kb` reads it.

```text
kb note --kind <status|gotcha|fix|decision|idea> [--ref R]... [--agent NAME] [--project FOLDER | --machine] "text"
kb note --kind <kind> [--ref R]... - <<'EOF'     # text on stdin: quotes, backticks and $ need no escaping
kb note --resolve <id> ["what changed"]
```

The kind says what the next agent should do with the note, and decides how long it stays true:

| Kind | For | Example | Lifetime |
|---|---|---|---|
| `status` | a current state the next agent must know | integration tests fail on `main` since the httpx bump; not your change | until `--resolve` |
| `gotcha` | a trap or surprising behaviour | `uv run` in the sandbox needs `--cache-dir "$TMPDIR/uv"` | until ingest |
| `fix` | an error and what fixed it | `pytest` hangs in the sandbox: the DB fixture needs a socket; run `-m 'not db'` | until ingest |
| `decision` | a choice and its reason, when the code doesn't show it | retries use tenacity, because httpx ignores `Retry-After` | until ingest |
| `idea` | an improvement the session ran into and didn't make | `kb doctor` could say which hooks file it read | until ingest, or 30 days |

- **Text.** One to three sentences, at most 500 characters, written for an agent with none of the writer's context.
  Longer detail belongs in the repo (a doc, a comment, an issue), linked with `--ref`.
- **`--ref`** (repeatable): the evidence, such as a path (with `:line`), a command, a commit, an error message or a
  URL. The next agent and ingest check the note against it, the way Copilot Memory checks a memory's citations
  before using it. Optional, but a note without one counts for less in ingest. A path that doesn't exist gets a
  warning, not a failure.
- **Agent.** `kb` tries three things, in this order:
  1. *The agent's own variable.* Claude Code 2.1.316 sets `AI_AGENT` (`claude-code_2-1-316_agent`),
     `CLAUDE_CODE_SESSION_ID` and `CLAUDE_CODE_ENTRYPOINT` (`cli` or `sdk-cli`), inside its sandbox and outside it,
     and Codex sets `CODEX_THREAD_ID` on every shell command it runs (source, 2026-10-31); either links the note to
     its session's digest.
  2. *The hooks' marker.* The install script names the agent in each hook entry, and the hook leaves a marker with
     that name and the project (below). A variable can't carry it: `kb note` is started by the agent's shell, not
     by the hook. Without a variable this can't be Claude Code or Codex, so only Copilot CLI's and Cursor CLI's
     markers count, for 8 hours after their hook last ran, and they are read only when no variable has answered.
     One of them at work in this project signs the note (`from: hook`, which `kb inbox` shows). It beats an
     `--agent` it can disprove, naming Claude Code, Codex or no tool: Copilot CLI's model passed `--agent Codex` of
     its own accord (2026-11-02). The note keeps what the flag said as `claimed`, so a wrong signature can be told
     from a corrected model. `--agent` naming Copilot CLI or Cursor CLI is taken at its word, since one's finished
     session is no proof against the other. With both at work and no such flag, `kb` exits 2 and says only those two
     will do.
  3. *Asking.* Otherwise `kb` asks for `--agent` with the tool's name, not the model's (Cursor running a Claude
     model is still Cursor CLI).

  An agent started by another (Codex run from Claude Code) inherits its variables, so `--agent` naming a tool wins
  over them, and with two agents' variables set `kb` asks for it; a flag naming no tool, such as a model name, loses
  to the environment. Copilot CLI or Cursor CLI started from Claude Code without `--agent` is still recorded as
  Claude Code.

  A marker says who ran in the project lately, not who is calling, and these limits are accepted for now, since a
  wrong name on a note is a small error and `(from hook)` in `kb inbox` shows which notes it can touch:
  - A note written by hand, or by an agent `kb` doesn't know, in a project where Copilot CLI or Cursor CLI ran in the
    last 8 hours is signed with that agent, whatever other name its `--agent` gives. Sibling clones of a repo share
    a project, and so its markers.
  - After 8 hours with no hook, a session's marker no longer counts, and the model's `--agent` does, until its next
    hook: no hook runs at the start of a turn. That means idle time, a single turn longer than that, or with
    `--hooks start` any session past 8 hours. With `--hooks stop` there's no marker during the first turn.
  - Claude Code or Codex with its variable missing (an older version, `env -i`, a container exec) counts as an agent
    `kb` doesn't know, so a marker overrules its true `--agent`. `kb` can't tell that from Copilot CLI's model saying
    `--agent Codex`, the case the marker is there to beat; `claimed` shows it afterwards.
  - With both at work, a caller that is neither of them, such as a person or another tool, gets exit 2 and has no
    true name to give until one marker lapses. Taking its word would also take a model's name or a wrong tool, and the
    refusal is what makes a model name its tool.
  - A marker counts only where `kb note` works out the same project. Outside git, each subfolder is a project of its
    own, and a nested repo or submodule is another project. Matching by folder prefix would be worse: a session
    started in `~` would sign notes in every folder under it outside git.
  - A Cursor CLI session with several roots (`--add-dir`) counts only for its first, where its reminder counts notes
    too.
- **`--project <folder>`**: the repo the note is about, when it isn't the current folder's. The note is written as
  if from that folder: its project, branch, commit and `cwd` are that folder's, and its refs are checked there, by
  `kb note` now and by ingest later. The markers are still read where the session runs, since they say who is at
  work there. It takes a folder that exists, not a project id, so a mistyped id can't file a note under a project
  nobody reads. A note filed under a folder outside git, given or not, gets advice on stderr to resolve it and write
  it again with `--project` if it's about a repo; only `--project .` files it there without that advice. A
  resolution takes no `--project`: `kb` finds the note by its id wherever either is filed, and the note counts as
  resolved in every project. In a real inbox, 4 of the first 43 notes were about a repo but written from a workspace
  above it, outside git, so none reached that repo's sessions (2026-11-12).
- **`--machine`**: the note holds on this machine in every project, such as what the GitHub token may do or a tool
  that fails in the sandbox. Its project is `machine`, which no project id `kb` makes can be, since those all have a
  `/` (a repo whose origin is a relative path without one, such as `machine`, is named by its folder), so the format
  stays `fabric.note/1` and an older `kb` reads it as one more project. It has no branch or commit, and its cwd is
  `~`: the folder it was written in would name a project that may not be on the allow list. It takes no `--ref`, as a
  path means something else in each project where it's read, so the evidence goes in the text. `kb inbox` lists the
  machine's notes in every project with the project's own, and `kb inbox --project machine` lists them alone, even
  beside a folder called `machine`. The start hook counts them with the project's and says how many are the machine's:
  "2 status notes … for <project>, 1 of them for this machine". Ingest takes them only once `machine` is on the allow
  list (§4.5), and files them on `wiki/machine.md`, outside `wiki/projects`, where a folder outside git called
  `machine` would get the same page. In a real inbox, five of the first 43 notes were about the machine and sat in one
  project's notes, where the next project couldn't see them (2026-11-12). A resolution takes neither flag.
- **`--resolve <id>`** closes an earlier note, usually a `status` once the tests pass again. It writes a new note
  that points at the old one, so the inbox stays append-only; resolved notes drop out of `kb inbox` and
  `kb catchup`. A note already filed on the wiki can't be resolved: what's true now goes in a new note, which ingest
  weighs against the claim.
- **Repeats.** The same kind and text in the same project within 24 h writes nothing and prints the existing id, so a
  retried command doesn't duplicate a note.
- **Result.** The note's id and path, exit 0. Bad input exits 2 and says what to change (the kinds, the length
  limit, a missing `--agent`); an inbox it can't write exits 1 and names the folder (setup, below). Each note is a
  new file, created exclusively; `kb` never edits or deletes one.

```markdown
~/.local/share/agent-fabric/inbox/2026-10-31-n3f9a2c.md
---
schema: fabric.note/1
id: n3f9a2c
kind: status
agent: {name: Claude Code, from: env, session: 5b1e…, entrypoint: cli}
project: billing-api             # project id (§4.4) of the current folder
cwd: ~/code/billing-api
branch: feat/retry
commit: 9d8e7f6
time: 2026-10-31T17:05:12Z
refs: [pytest tests/integration, tests/integration/test_client.py]
resolves: null
---
The integration tests fail on `main` since the httpx 0.29 bump (connection pool API changed). Failures there are
not caused by your change.
```

**Reading notes.** `kb inbox` lists the unresolved notes not yet ingested, and marks `status` notes older than 14
days as possibly stale. `kb catchup` (§4.9) includes the project's unresolved `status` notes and its latest other
notes; `kb search --kind note` searches all of them; `kb mcp` offers the same. Like all `kb` output they are labelled
untrusted: an agent wrote them and nobody has checked them yet.

**Into the wiki.** `kb ingest` (§4.7) takes the open `gotcha`, `fix`, `decision` and `idea` notes of allowed projects.
An idea is a suggestion nobody has decided on, so it never becomes a claim and the model never sees it: ingest files
its own text, escaped, under its project in `ideas.md` at the wiki's root, and the review list shows it last. An agent
always has a suggestion, so the stop question doesn't ask for ideas, the skill allows one per session, and the start
hook's count leaves them out. One left open for 30 days expires: `kb inbox` hides it and ingest retires it unfiled.
`status` notes stay out: they describe a moment, and end with `--resolve`. A resolved note skips the model too, and
moves to `inbox/done/<yyyy>/<mm>/` with its resolution, where search still finds it. All of the first five notes in a
real inbox (2026-11-02 to 04) were resolved within two days, and what lasted was already in this document or
AGENTS.md. A resolution that holds a lasting fact should be its own note, and the skill says so. A note ingest uses is
copied to `raw/notes/<project>/<yyyy>/<mm>/` in the wiki repo, after the redaction gate (§4.5), so pages can cite it.
After the merge, every note the run used or dropped moves to `inbox/done/<yyyy>/<mm>/`, so the inbox holds only notes
still to act on. Notes from projects not on the allow list stay local.

**Skill.** `src/agent_fabric/skills/kb-note/SKILL.md`, shipped in the package, copied into two user skills folders
that between them reach all four CLI agents: `~/.claude/skills/` (read by Claude Code and Cursor CLI) and
`~/.agents/skills/` (Codex and Copilot CLI). Copilot CLI stopped reading `~/.claude/` in 1.0.36 and reads personal
skills from `~/.copilot/skills/` and `~/.agents/skills/` (`copilot skill --help`, 1.0.105); Codex still reads
`$CODEX_HOME/skills/` but calls it deprecated in favour of `~/.agents/skills/` (source, 0.162.1). The first setup
put the second copy in `$CODEX_HOME/skills/` (`~/.codex/skills/` by default), where Copilot CLI never saw it
(corrected 2026-11-01); `kb doctor --fix` removes that copy, wherever `CODEX_HOME` puts it, once
`~/.agents/skills/` has one, so Codex doesn't load the skill twice. Only the name and
description sit in the agent's context; the body loads when the agent decides to use it. `CLAUDE_CONFIG_DIR` moves
Claude Code's folder, settings and skills with it; Cursor CLI is assumed to keep reading `~/.claude/skills/`, which
then gets a copy of its own. With `COPILOT_HOME` set, Copilot CLI reads `$COPILOT_HOME/skills/` instead of
`~/.agents/skills/`, and that folder gets a copy. `kb doctor` checks every copy against the package, for each agent
it finds installed, and `--fix` adds missing copies and updates outdated ones. A `~/.claude` folder alone doesn't
mean Claude Code is installed: a copy for Cursor CLI creates it.

That file is the one copy of the skill text.

**Hooks.** The skill waits for the agent to think of it. Two hooks remind it, at the start of a session and at the
end of a turn. Like the skill, they carry an instruction and a count, never a note's text (§7): the agent reads the
notes with `kb inbox`, under its "unchecked" header and between two markers. The markers carry an id that is new on
each run, and the header names the closing one, so a note can't end the list early and pass for `kb`'s own text. The
one text in a reminder that `kb` doesn't write is the project id, from the `origin` remote or a folder name, which can
carry an instruction as well as a note can. The reminder works without it, so it names the project only when the id
is plain (letters, digits and `._/~:@+-`, up to 200 characters), and otherwise says "this project". Wherever `kb`
shows a note's text (`kb inbox`, wiki pages, manifests), characters that act instead of showing are escaped: control
characters, and Unicode format and separator characters such as bidi overrides, which reorder text on screen.

```text
kb hook start --agent <claude-code|codex|copilot|cursor>   # session start: the reminder, in that agent's format
kb hook stop --agent <claude-code|codex|copilot|cursor>    # end of a turn: the question (Stop, below)
```

- **Start** runs when a session starts, resumes, is cleared or is compacted (the last two drop the earlier reminder).
  It gives the number of open `status` notes for the project, says to run `kb inbox` before relying on the repo's
  state, then gives the number of other notes (gotchas, fixes, decisions) and says to read the ones about the work,
  and reminds the agent of `kb note`. Only a `status` note says the repo may not be as the agent assumes; the others
  wait for the wiki. With one count for all, 9 gotchas and decisions waiting for a first ingest told every session in
  a project to check the repo's state (2026-11-12). "Most sessions need no note" is in the wording because the first
  one drew a note that only described the repo, from a session asked what the project does (Copilot CLI, 2026-11-02).
  Cursor CLI's `--resume` ran no start hook in the one session that logged it (2026-11-03).
- **Stop** runs at the end of each turn, not at session end: when the session ends, the agent can no longer act. At
  the first end of a turn at least 10 minutes after the session started, it makes the agent continue with a question:
  did this session find something the next agent must know? If not, just stop. It asks again 8 hours after its last
  question, or an hour after it if a compaction has dropped what the session saw since. Only Claude Code's start hook
  says it follows a compaction (`source: compact`); the hour keeps a session that compacts every few minutes from
  paying a turn each time, and a compaction in the first 10 minutes doesn't cut them short. One Claude Code session
  ran 2½ days through four compactions and was asked once, on its first day, so the work of the later days went
  unasked (2026-11-06). The answer costs one turn, and Copilot CLI counts the question as a premium request: its count
  rose by 2 for one typed prompt plus the question (2026-11-02). Shorter sessions aren't asked, and the trial (§10)
  tunes the 10 minutes. Cursor CLI doesn't ask in a `-p` run: a chat 44 minutes old wasn't asked with `-p`, and was in
  an interactive session (2026-11-02). A `-p` turn and two interactive ones logged two stop hooks, which fits `-p`
  running none (2026-11-03).
- **Markers.** One per session in `~/.local/share/agent-fabric/hooks/` records when the session started, when it was
  last asked (`asked_at`; a marker from before it counts as asked at its next hook), whether it has compacted since,
  and its project. The file's own time says when a hook last ran, which tells `kb note` who is at work (above) and
  `kb doctor` whether the hooks run. Markers no hook has run for in 7 days are removed. Where a marker can't be
  written, the stop hook stays silent rather than ask every turn. Each end of a turn looks the project up again, so
  the marker follows a session that runs `git init` or moves (a few milliseconds; where git hangs, the turn waits up
  to the hook's 10 seconds). The end of a turn that asks doesn't, since a hanging git would stop the question ever
  being asked, so a note answering it can still meet the old project, or none with `--hooks stop`.
- **Never in the way.** Any error, such as unreadable input or a missing inbox, prints nothing and exits 0, and
  each hook entry has a 10-second timeout: a session never fails or waits because of `kb`.

| Agent | Hooks file | Start | End of turn | Output |
|---|---|---|---|---|
| Claude Code 2.1.316 | `~/.claude/settings.json` (`hooks`) | `SessionStart` | `Stop` | `hookSpecificOutput.additionalContext`; `decision: "block"` with `reason` |
| Codex 0.162.1 | `~/.codex/hooks.json` | `SessionStart` | `Stop` | the same as Claude Code |
| Copilot CLI 1.0.105 | `~/.copilot/hooks/kb.json` (kb's own file) | `sessionStart` | `agentStop` | `additionalContext`; `decision: "block"` with `reason` |
| Cursor CLI 2026.10.22 | `~/.cursor/hooks.json` | `sessionStart` | `stop` | `additional_context`; `followup_message` |

`CLAUDE_CONFIG_DIR`, `CODEX_HOME` and `COPILOT_HOME` move the first three files with their folders, as they move
the settings and skills: `$CLAUDE_CONFIG_DIR/settings.json`, `$CODEX_HOME/hooks.json`, `$COPILOT_HOME/hooks/kb.json`.

From each agent's hooks reference, and Codex's source at `rust-v0.162.1` (2026-11-01). All four have since run both
hooks live (2026-11-02); what's still unseen is in §11. Each sends the hook its input as JSON on stdin and runs user
hooks in every project. Claude Code and Codex run hooks outside their sandboxes (Claude Code's docs, Codex's source),
and Copilot CLI has none, so the hook can write its marker. Two agents need more:

- Cursor CLI also runs Claude Code's user hooks (its "third-party configs" setting, on by default), so in Cursor
  both entries fire. `kb hook --agent claude-code` stays silent when its input comes from Cursor, which adds
  `cursor_version`. Copilot CLI runs Claude Code's hooks only from a repo's `.claude/`, not the user's.
- Codex runs a new or changed user hook only after I approve it, once, in Codex (`/hooks`, or the prompt at
  startup). `kb doctor` reads that from `[hooks.state]` in `config.toml`, by the hook's place in `hooks.json`: a hash
  means approved. A hook with a hash and no `enabled` still runs (2026-11-02), so only `enabled = false` counts as
  off. An unapproved or turned-off hook is a `problem`, since the setup isn't done. The hash comes from an internal
  Codex function, so `kb` checks that one is there, not that it matches: after a change to kb's hook it can read as
  approved until Codex asks again.

**Setup.** `scripts/install.sh` installs `kb` from the clone with `uv tool install`, then runs `kb doctor --fix`.
The install is not editable, so agents keep the installed `kb` while the clone switches branches; rerunning the
script after a pull updates `kb` and the skill copies. It runs in the user's own terminal, because an agent's sandbox
can't write the agents' settings. `kb doctor` is the one place that knows what setup means: a setup UI, if one comes
later, shows its checks. `scripts/install.sh --hooks start,stop` (the default), `start`, `stop` or `none` chooses
the hooks: it passes the choice to `kb doctor --fix --hooks`, which records it as `hooks` under `[notes]` in
`~/.config/agent-fabric/config.toml`, installs those hooks and removes kb's entries for the others. Later
`kb doctor` runs, and the script rerun without `--hooks`, keep to the recorded choice; with none recorded, both.

**What `kb doctor` prints.** An agent reads the report as often as I do: it runs `kb doctor` when a note fails (the
checks only read, so they work in its sandbox), or reads what `install.sh` printed. So the report follows AXI's
principles for agent-facing CLIs ([axi.md](https://axi.md/)): one TOON document (spec 4.4) with the counts first,
one row per check, an explicit "nothing to do", and next steps that say who takes them. Setup steps are always the
user's, because an agent's sandbox can't change the agents' settings; the closing `help` line asks the agent to
explain them in plain words. TOON quotes a value with a comma, a colon or a quote in it.

```text
summary: "3 of 9 checks need fixing: 2 by kb doctor --fix, 1 by hand"
checks[9]{status,check,path,detail}:
  ok,kb,~/.local/bin/kb,on PATH
  ok,inbox,~/.local/share/agent-fabric/inbox,writable
  ok,skill,~/.claude/skills/kb-note/SKILL.md,for Claude Code and Cursor CLI
  missing,skill,~/.agents/skills/kb-note/SKILL.md,for Codex and Copilot CLI
  ok,sandbox,~/.claude/settings.json,Claude Code can write the inbox
  problem,sandbox,~/.codex/config.toml,kb can't add the inbox to sandbox_workspace_write.writable_roots without changing other lines
  ok,hooks,~/.claude/settings.json,"start and stop; last ran 2026-11-02 17:04"
  missing,hooks,~/.codex/hooks.json,start and stop
  ok,hooks,~/.cursor/hooks.json,start and stop; hasn't run yet
next[2]{by,step}:
  user,"In your own terminal, not in an agent's session, run kb doctor --fix (or rerun scripts/install.sh)"
  user,Add /home/me/.local/share/agent-fabric/inbox to sandbox_workspace_write.writable_roots in ~/.codex/config.toml
help: "These steps are for the user: an agent's sandbox can't change the agents' settings. Agents: explain them to the user in plain words."
```

- **Statuses:** `ok`, `fixed`, `missing` and `outdated` (both of which `--fix` fixes), and `problem` (by hand). Exit
  0 when every row is `ok` or `fixed`, 1 otherwise.
- **`kb` row:** whether the first `kb` on `PATH` is the one `install.sh` installed, since agents run whichever comes
  first. It replaces `install.sh`'s own warning.
- **Hooks rows** say whether the hooks run, not only whether they're in the file: Codex's approval, and when a hook
  last ran, by the markers. A right file with no run behind it drew a note doubting a hook that worked (2026-11-02).
- **`sync` row:** whether Copilot CLI's session sync is off (§7), where Copilot CLI is installed. `--fix` doesn't
  change it: what leaves the machine is my choice, not part of setting up notes.
- **`install.sh`** keeps uv quiet, so what it prints is this report.

**Setup per agent.** Claude Code's and Codex's sandboxes allow writes only in the project and temp folders, so the
inbox folder goes on each one's write list (Claude Code: `sandbox.filesystem.allowWrite` in
`~/.claude/settings.json`; Codex: `sandbox_workspace_write.writable_roots` in `~/.codex/config.toml`). A sandbox can
only grant writes to a folder that exists, so `kb doctor --fix` creates the inbox first. It changes nothing else in
these files, and one it can't edit safely it reports with the path to add by hand. Cursor CLI's sandbox needs the
same where it runs (it doesn't start on the devbox); Copilot CLI has no OS sandbox. So that `kb note` doesn't ask for
approval every time, it also goes on each agent's command allow list, except in Claude Code with
`sandbox.autoAllowBashIfSandboxed`, which runs sandboxed commands without asking; `kb doctor` doesn't check the allow
lists yet.

The hooks go into the four hooks files in the table above. `kb doctor` knows its own entries by their command and
changes no other, so the user's hooks survive every run. Copilot CLI's `kb.json` holds only kb's hooks.

## 5. Processes, ports, locations

| Unit (systemd --user) | Command | Bind |
|---|---|---|
| `agentsview.service` (sandboxed; user or system unit, §4.1) | `agentsview serve --no-browser --no-update-check --port <port> --replace` | `127.0.0.1:<port>` (8080 by default) |
| `fabricd.service` | tracker, observer, digester, panel | 127.0.0.1:8090 |
| `llama-panel.service` | `llama-server` with E4B | 127.0.0.1:8091 |
| `fabric-ingest.timer` | `kb ingest` daily at 03:00, on `codex` and later on `local` (which starts a llama-server on 8092), §4.7. The run's gate decides whether there's work and a run adds to the branch waiting for review, so a daily run costs no more than a weekly one and review stays weekly. `Persistent=true`, so a machine asleep at 03:00 runs it on waking. `kb doctor --fix` writes it once a project is allowed, with a `PATH` of the folders of the tools ingest runs and of node, since a unit gets none of a shell's (codex lives under nvm), and the shell's `XDG_*_HOME` and `CODEX_HOME`. That `PATH` depends on the shell, so a unit counts as current while its own still finds the tools. A run that finds another holding ingest's lock exits 75, which the unit counts as a success, and a timer the user disabled or masked stays off, even when `--fix` rewrites its units. `kb doctor` asks systemd whether it's on and running and how its last run went, which needs no marker: a reboot forgets a failure, and the next failed run shows it again | — |
| `fabric-prune.timer` | `kb prune` weekly, which writes only when a page meets its trigger (§4.7; `kb lint` runs in `kb ingest`, before its gate) | — |
| `fabric-backup.timer` | daily SQLite backup of agentsview archive and `state.db`, and a `git bundle` of the wiki, which has no remote | — |

| Path | Contents | In git |
|---|---|---|
| `~/.config/agent-fabric/config.toml` | settings (allow lists, idle timeout, ports, backends, project aliases) | no |
| `~/.local/share/agent-fabric/state.db` | cursors, loops, flags | no (backed up) |
| `~/.local/share/agent-fabric/index.db` | search index | no (rebuildable) |
| `~/.local/share/agent-fabric/quarantine/` | digests blocked by gitleaks | no |
| `~/.local/share/agent-fabric/local/` | digests of projects not on the allow list | no (backed up) |
| `~/.local/share/agent-fabric/inbox/` | notes from `kb note`; `done/` once ingested (§4.10) | no (backed up) |
| `~/.local/share/agent-fabric/hooks/` | one marker per agent session, for the hooks, `kb note` and `kb doctor`; removed after 7 days without a hook (§4.10) | no |
| `~/.agentsview/` | agentsview config + archive | no (backed up) |
| `~/wiki/` | wiki repo | yes |

## 6. Stack

Python 3.13+ with uv; `httpx` + `httpx-sse` (adapter), `starlette` + `uvicorn` (panel), `mcp` official SDK (MCP
server), `pydantic` (schemas), stdlib `sqlite3` (FTS5). External binaries: agentsview (CI-built, pinned),
llama.cpp `llama-server` (SYCL or Vulkan build), `gitleaks`, `git`, `codex`. Dev: ruff, mypy, pytest, pinned per
project. CI: GitHub Actions with the `vet` gate (§4.1) and `lint` (shellcheck, shfmt, actionlint, zizmor; ruff,
mypy and pytest on Python 3.13 and 3.14).

## 7. Security and privacy

- Everything binds to 127.0.0.1. No auth needed while loopback-only; agentsview's `--require-auth` if that changes.
- Redaction before write, gitleaks gate, pre-commit backstop (§4.5). The wiki repo is not treated as secret-safe.
- Allow list: only listed projects are published (§4.5).
- The code repo is public: no session data, config, digests or wiki content ever goes into it, and its CI has no
  access to the wiki repo. Fixtures for tests are synthetic or scrubbed recordings.
- Untrusted content: the ingest model has no tools, and can only pick from the options of typed questions, so no model
  text reaches a claim; `kb prune`'s summaries are the only model text in the wiki, written with no tools, cited and
  reviewed (§4.7); MCP output is labelled historical; nothing is auto-injected (the `kb-note` skill and its hooks are
  instructions, not history).
- Hosted ingest and `kb prune` send the notes of allowed projects, and ingest also excerpts of the files they cite, to
  the model's vendor, after the input gate (§4.7). Refs are read only from git objects of the note's own project, so a
  note can't get `~/.ssh` or an untracked file sent. The `local` backend sends nothing off the machine.
- Agent notes (§4.10) can carry a prompt injection from whatever the agent read. They are labelled untrusted, never
  injected into prompts (the start hook gives only their count and the project id), and reach the wiki only through
  reviewed ingest. A note filed with `--machine` is listed in every project's sessions, so text written in one
  project reaches agents working in all the others, still under the untrusted header. The inbox is writable from the
  agents'
  sandboxes, so a note is not tamper-proof; that is acceptable for advice that is checked before it becomes canon.
  Content screening doesn't catch a plain false note (arXiv 2608.21230: it refused 0 of 360), so the ref evidence and
  my review are the defence, and nobody has measured how well that works.
- SLM tools: none at first. Read-only tools may follow, such as reading the project's docs or `kb search`, with
  these limits: read-only; paths limited to the project root and `~/wiki`; secret-looking files denied (`.env*`,
  `*.pem`, `id_*`, `*credentials*`); size caps; no shell, no network. Tool output is untrusted, like the
  transcript. SLM text never reaches the wiki; the only exception is verbatim transcript quotes, checked by
  substring match.
- agentsview is built with telemetry compiled out and runs with egress denied except loopback (§4.1). Its archive
  holds Cursor auth tokens → local disk only, backups encrypted (age) if they leave the machine.
- Copilot session sync is off. By default Copilot CLI and the Copilot app copy local sessions to the GitHub
  account, unredacted and outside the allow list. Set `"remoteExport": false` in `~/.copilot/settings.json`
  (phase 0) and don't turn on remote control (`remoteSessions` or `/remote on`), which re-enables export whatever
  that setting says. Repo-level Copilot settings can't override it: keys not on their list are ignored. On the devbox
  the key sat only in `~/.copilot/config.json`, which doesn't count: GitHub's config reference says "Settings left in
  `config.json` are ignored" (read 2026-11-03), and Copilot CLI 1.0.105's own header in that file says user settings
  belong in `settings.json`. So `kb doctor` reads `settings.json` alone and reports a `problem` unless `remoteExport`
  is `false` there, or if `remoteSessions` is `true`; `--fix` leaves the setting to me. Scripted runs
  (`scripts/spike/hook-lab.sh`) also pass `--sync-session=off`, which the reference gives for a single run: 1.0.105
  says `--remote-export` was renamed to it, and still accepts `--no-remote-export` without listing it. The docs don't
  say whether turning sync off deletes copies already synced, so check github.com once.

## 8. Failure modes

| Failure | Effect | Handling |
|---|---|---|
| agentsview down | no new events | panel shows "capture offline"; agents still write files; agentsview catches up on restart |
| agentsview upgrade changes API | adapter errors | pinned version; contract tests gate upgrades |
| sandbox blocks a read agentsview needs | an agent's sessions missing | `agentsview doctor` and per-agent session counts in `kb doctor`; widen only that path |
| agentsview can't write `~/.cursor/chats` | every Cursor CLI session fails to sync | Cursor CLI's own `CaptureSource` (§4.1); never give agentsview write access to agent data |
| Codex compresses old rollouts to `.jsonl.zst` | v0.45.0 can't read them; sessions not yet synced are missed | pin v0.46.0 or newer (§4.1) |
| Claude Code deletes its tool-result files | archived output can be lost on a re-parse in v0.45.0 | pin v0.46.0 or newer |
| headless runs in bulk (benchmarks, scripts) | wiki flooded with digests nobody reads | local-only unless the project opts in (§4.3) |
| MCP client starts its own daemon | unconfined second daemon | `--server` in every MCP registration; the unit's `--replace` takes over from one that started first |
| Cursor IDE updates lag | panel late for Cursor | measure in spike; add Cursor hooks (`afterAgentResponse`, `afterFileEdit`) as a second source if needed |
| llama-server down/slow | no SLM bullets | rules-only mode, flagged in panel header |
| gitleaks hit | digest not committed | quarantine + panel notice |
| the disk is lost | the wiki goes with it, having no remote | daily `git bundle` in the backup (§5) |
| ingest branch left unmerged | wiki stale | later runs append to the same branch; `kb doctor` warns after 7 days |
| Codex login expired, or the model call fails | no ingest; notes wait | `OnFailure=` marker reported by `kb doctor`; the notes stay pending |
| gitleaks missing or failing | nothing can be checked | `kb ingest` stops before any model is asked; `kb doctor` reports a missing one |
| ingest branch checked out, or moved during a run | a run can't add to it safely | the run stops and says so, with the branch as it was; the notes wait |
| a backend returns an invalid answer | no decision | one retry, then it goes on the review list and reaches no page (§4.7) |
| a confident wrong answer | noise filed, or a useful note dropped | review, least certain first; a dropped note is listed for review, and keeps its file (§4.7) |
| a summary gets a connection wrong | a false lead that reads as fact | each sentence cites its claims and it's marked as model-written; review; deleting it loses nothing (§4.7) |
| the hosted run still has a tool | a note could make it read a file | the first real run checks this (§4.7, §11) |
| vendor prunes/migrates history | source files gone | agentsview archive + daily backup keep the raw layer |
| own automated runs captured | feedback loop | `codex exec --ephemeral`; ignore `is_automated` sessions |
| sandbox blocks the inbox write | `kb note` exits 1, the note is lost | the error names the folder to add to the sandbox's write list; `kb doctor` reports a missing or unwritable inbox and a write list without it, and `--fix` adds it (§4.10) |
| agents note routine progress | inbox noise | the skill says most sessions need none; ingest drops it; tighten the description |
| `status` note never resolved | later agents warned about a fixed problem | `kb inbox` marks it possibly stale after 14 days; `kb lint` lists it |
| agent misnames itself (model name for tool name) | wrong `agent` on the note | the environment wins over a model name in `--agent`; a tool name in `--agent` wins, for an agent started by another; the hooks' marker wins over a flag it can disprove (§4.10) |
| agents never call `kb note` | no notes | the start and stop hooks remind them (§4.10); measured in the phase 1 trial; reword the skill description |
| the stop hook asks at the wrong moment | an extra turn per session; notes written to please it | once per session, then not within 8 hours, or 1 after a compaction; not before 10 minutes; ingest drops thin notes; `--hooks` leaves it out |
| a hook fails or hangs | the agent's session start or turn end held up | any error prints nothing and exits 0; a 10-second timeout per entry |
| a Codex hook not yet approved | no reminders in Codex | `kb doctor` says to approve it in Codex's `/hooks` |
| injected or wrong note | bad advice | untrusted label; `kb` gathers its ref evidence for the model and for me; review gate |

## 9. Phases

**Phase 0 — spike (1–2 days).** On the desktop:
1. agentsview captures one session each from Cursor IDE, VS Code Copilot Chat and Codex, with tool calls and results.
2. `agentsview session watch` lag during live Cursor IDE and Copilot Chat sessions (target ≤ 10 s).
3. `agentsview mcp --profile memory` from Codex finds a Cursor session.
4. Record API responses as adapter fixtures; check `/api/v1/events` semantics and the turn-end heuristic.
5. `llama-bench` E2B / E4B / 26B-A4B on SYCL vs Vulkan, and the decision-model candidates (§4.7, §10).
6. CI workflow builds agentsview with the `vet` gate and the Ubuntu 24.04 smoke test; install it on the desktop;
   run the two `systemd-run` probes (§4.1) to choose a user or system unit; confirm checks 1–3 still pass with the
   sandboxed unit.
7. Failure detection: a failing command and a failed edit in each agent's fixtures come out as `failed`, with the
   right exit code where there is one.
8. Set Copilot `remoteExport` to `false` (§7) and confirm that a new Copilot CLI session doesn't show up on
   github.com.

`scripts/spike/phase0.sh` walks through items 1–8 on the desktop: it runs the checks it can (probes, lag timing,
API recording, failure detection, llama.cpp `b11539` benchmarks) and says what to do in each agent. Results
(timings, counts, settings; no session text) go to `~/.local/share/agent-fabric/spike/results.md`; the test
sessions' recordings stay in `spike/recordings/` for the phase 1 fixtures, scrubbed first (§7).

Exit: go/no-go on agentsview per agent; chosen GPU backend.

The CLI agents come first (§2). Items 1 and 7 for them ran on the devbox on 2026-10-31 (capture check, §4.1): go for
Claude Code, Codex and Copilot CLI; no-go for Cursor CLI, which gets its own reader in phase 1. Item 2 (watch lag) and
item 3 (cross-agent search) are still to do for the CLIs. Items 1–3 and 7 for the IDE agents move to phase 3.

**Phase 1 — digests and query.** First `kb note`, `kb inbox`, the `kb-note` skill and its hooks (§4.10): they need
neither agentsview nor fabricd, so a one-week trial in one or two projects runs while the rest is built. Notes-only
`kb ingest` and `kb lint` on the `codex` backend follow, for the same reason (§4.7). They are built and first ran on a
real model on 2026-11-12; their daily timer followed (§5), and `kb lint`'s summary check comes with `kb prune`. Then
CI with the `vet` gate for this repo; the agentsview pin bumped to v0.46.x (from 2026-11-06, §4.1); adapter,
normalizer, tracker, digester with allow list/redaction/gate/commit, for the CLI agents: agentsview for Claude Code,
Codex and Copilot CLI, `CursorChatSource` for Cursor CLI; wiki repo skeleton, `kb` CLI + `kb mcp`; a prototype session
for the panel layout, then the panel with rules only (incl. open loops and carry-over).

**Phase 2 — models.** SLM bullets in the panel after it passes the eval (§10); `kb ingest` takes digests too (§11);
the comparison with the `local` backend, then nightly on it (§10); `kb prune` and `kb retro`.

**Phase 3 — breadth.** The IDE agents (Cursor IDE, VS Code Copilot Chat), starting with phase 0 items 1–3 and 7 for
them on the desktop; per-agent slash commands for catch-up, Cursor hooks if lag demands it, semantic search
(agentsview's or qmd) if FTS isn't enough.

## 10. Testing and evaluation

- Digest: golden-file tests from recorded sessions per agent; regeneration must be byte-identical.
- Rules: unit test per rule on diff/command fixtures, positive and negative (including `docs-drift`); weekly review
  of panel false positives.
- Loops: fixtures where the same skip appears in two sessions (one loop, two evidence entries), a marked line moves
  (same loop), a marker is removed (closed), and a closed marker comes back (reopened).
- Failure detection: per-agent fixtures of failing shell commands, failed edits and MCP tool errors, including
  Codex code-mode scripts (`Script failed`), Copilot's `errored` edits with no stored error text, and commands that
  end in `; echo EXIT:$?`.
- Cursor CLI: `store.db` fixtures from synthetic sessions (the capture check's three turns, `-p` and interactive);
  the reader must give the same messages and tool results as the JSON blobs, in conversation order.
- Headless runs: fixtures of `claude -p`, `codex exec` and `copilot -p` sessions; each gets a local digest only unless
  its project is on `[publish] headless`.
- `kb note`, `kb inbox`, `kb doctor`, `kb hook`: each case is a test in `tests/`. `scripts/check.sh` runs them with
  CI's other checks, before each commit; `scripts/check.sh --red REV` names new tests that pass against older code,
  which don't show the change. In an agent's sandbox, `pre-commit` can't stash other unstaged changes in its read-only
  `~/.cache/pre-commit`, and the commit fails: `PRE_COMMIT_HOME="$TMPDIR/pre-commit" git commit` gives it a folder it
  can write. By hand, inside the Claude Code and Codex sandboxes with and without the inbox on the write list: the
  note is written, or exit 1 names the folder.
- Hooks in a real agent: `scripts/spike/hook-lab.sh <cursor|copilot|codex> [--stop]` runs the agent on this clone's
  `kb` in a throwaway repo and data folder, and prints the markers, how `kb note` signs there and the hook runs. What
  it relies on (2026-11-02, Codex 2026-11-03): all three follow `XDG_DATA_HOME`; Copilot CLI's and Codex's hooks follow
  a `PATH` prefix, and Copilot CLI ignored a project hooks file; Cursor CLI's ignore the prefix and run a project
  `.cursor/hooks.json` as well as the user's. For Codex the lab uses the user's approved hooks file, and Codex's
  `state_5.sqlite` can keep a row for a lab session after the lab's cleanup.
  `--stop` backdates the marker's `started` rather than wait 10 minutes, changes the repo's origin, and drives Cursor
  through a private tmux server, as `-p` runs don't get the question. The marker taking up the new origin shows the
  stop hook did it only in Cursor CLI, whose `--resume` ran no start hook: Copilot CLI's resume runs its start hook,
  which renames the marker too. Cursor CLI's stop hook gets `workspace_roots`, so it names the project from
  `~/.cursor`, where user hooks run (2026-11-03). On resume, Codex, Copilot CLI and Cursor CLI keep a session's
  marker, and Claude Code gets a new one.
- What a hook did, in each agent's own record of the session: Claude Code `~/.claude/projects/<folder>/<id>.jsonl`;
  Codex `~/.codex/sessions/<y>/<m>/<d>/rollout-*.jsonl`; Copilot CLI `~/.copilot/session-state/<id>/events.jsonl`
  (`hook.start` and `hook.end`, with each hook's output); Cursor CLI `~/.cursor/chats/<md5 of the folder>/<id>/store.db`
  (blobs, §4.1).
- Notes trial (phase 1, one week, one or two projects): notes per session, how many I rate useful, duplicates,
  sessions where an agent should have left a note and didn't, and how often the stop hook's question led to a
  useful note.
- Project identity: the same repo as a main checkout, a linked worktree, a second clone, an SSH and an HTTPS remote,
  and a folder with no remote, each resolving to the expected id; an alias merges two ids.
- Redaction: fixture corpus of fake secrets in every supported shape; test fails if gitleaks finds anything after
  redaction.
- SLM: labelled set of ~30 real sessions from phase 1 (plus a few ImpossibleBench trajectories). Score recall of
  decisions and reason links, precision of bullets, latency per batch. Initial adoption bar: ≥ 70% decision recall,
  ≤ 1 wrong bullet per 10, ≤ 10 s per batch.
- `kb ingest` and `kb prune`: tests in `tests/` with a fake backend, for the gate and the trigger, the manifest,
  resolved notes, the evidence, each way an answer can fail, a run that dies before its commit, a summary sentence
  citing no claim, and no claim ever changing. The first real run is mine.
- Does the wiki pay off? Keep 10 real questions, and answer them with `rg` over notes and then over pages. If pages
  don't win after a month, that's a finding: a compiled wiki isn't shown to beat good retrieval (arXiv 2605.18490). Try
  a second, less documented project too, since agents here move knowledge into the repo by hand (§4.10).
- Moving from `codex` to `local`: shadow runs on the same frozen batches, with both results unlabelled, plus 30 seeded
  traps scored by script: a real ref that contradicts its note, a duplicate, a contradiction, an injection.
  Score each backend by actions I marked wrong (§4.7), including the notes it dropped that the other kept.
- How many notes that takes: the first days' rate gives at most 1.33 eligible notes a day, so two weeks is 19 notes,
  with a 15% chance of telling 20% wrong from 40%. 150 notes give 96% but take 16 weeks (exact paired test, assuming
  independent notes). The switch reverses with one setting and I still review everything, so the bar is a screen for
  disaster: about 40 real notes and the traps, at most 10% wrong and within 10 points of `codex`, and a valid answer
  within one retry at least 98% of the time. After that, one batch a week also goes through `codex` as an audit. This
  catches a clearly worse model, not a slightly worse one. The thresholds are a first guess.

## 11. Open questions

Decided in review (2026-10-23): the name is agent-fabric with the CLI `kb`; prompts stay verbatim, and an allow
list decides what is published (§4.5); the idle timeout is a config value (§4.3); the panel layout is settled in
the prototype session (§4.8); the minimum desktop is Ubuntu 24.04 (§4.1, glibc); project ids come from the
`origin` remote, then the main checkout, then agentsview's project, with aliases in config (§4.4); Copilot
session sync is off (§7). Decided 2026-10-31: the CLI agents come first, the IDE agents in phase 3 (§2); headless
runs get a local digest only unless the project is on `[publish] headless` (§4.3); agents leave notes with
`kb note` and the `kb-note` skill, independent of agentsview (§4.10). Decided 2026-11-01: the skill goes in
`~/.claude/skills/` (Claude Code, Cursor CLI) and `~/.agents/skills/` (Codex, Copilot CLI), correcting
`~/.codex/skills/`, which Copilot CLI never read; a start hook and a stop hook remind agents of it, and the install
script can leave either out (§4.10). Decided 2026-11-04: each machine keeps its own local wiki, with no remote and no
sync (§2); ingest starts with notes on the `codex` backend, its model only answers typed questions, and it moves to a
local model after §10's comparison; `kb prune` adds cited summaries on a trigger and never removes a claim (§4.7).

Open (2026-11-01):

- What marks a Copilot or Cursor run as headless (§4.1).
- Which variables Copilot CLI and Cursor CLI set that identify the session, so that a note can link to its digest.
  The hooks' marker names the agent but not the session (§4.10). Codex: `CODEX_THREAD_ID` (2026-11-01).
- Whether `kb note` should look at which programs started it (`copilot`, `cursor-agent`) before the marker (§4.10).
  That names the caller itself, with no 8-hour window and no both-at-work case, but needs a check that an agent's
  shell can see its parents. For if the trial finds wrong signatures.
- Whether the Cursor IDE reads `~/.cursor/hooks.json`, and VS Code's Copilot Chat `~/.copilot/hooks/`: their chats
  would then be signed Cursor CLI or Copilot CLI (§4.10), though the design treats the IDE as a separate agent.
- Whether Cursor CLI reads `~/.agents/skills/` too (it would then see two copies), and whether Codex still reads it
  with `CODEX_HOME` set (§4.10).
- Whether Cursor CLI follows `CLAUDE_CONFIG_DIR` (§4.10 assumes it doesn't; Copilot CLI no longer reads `~/.claude/`).
- The rest of the hooks' live check (§4.10). All four agents have run both hooks and asked the stop question
  (2026-11-02). Still unseen, all in Cursor CLI: whether its agent acts on the start reminder (so far it was only
  given a command to run); whether it adds `cursor_version` to the input of Claude Code's hooks (that entry stayed
  silent, as it would if Cursor never ran it); and where it runs hooks relative to its sandbox (`sandbox.mode` is
  `disabled` on the test box, so the markers it wrote there don't settle it).
- The Cursor CLI reader: message order from the `store.db` blob tree, timestamps, and encrypted blobs (§4.1).

Open (2026-11-04):

- Which decision model passes §10 on each machine. The candidates' published scores are their authors' own.
- Digests in ingest. A digest isn't a claim, so filing its text as one won't carry over (§4.7).
- What `kb prune` runs on after `codex`, since it needs a model that writes.

Open (2026-11-12), from the first ten days of a real inbox: 43 notes in four projects. The notes were specific and
checkable, only one was filler, and 9 resolved an earlier one, two of them correcting it with new measurements.
Decided: a note about another repo names it with `kb note --project`, a fact about the machine is filed for every
project with `kb note --machine`, and the start hook counts `status` notes apart from the rest (§4.10). Open (§4.10):

- Whether notes reach other agents. 41 of the 43 were Claude Code's, every resolution was too, and nothing records
  a read. A few real work sessions of Codex and Copilot CLI in a project with open notes settle it: whether they run
  `kb inbox`, act on a note and leave their own (§10). Until then the inbox is shown to work as one agent's
  memory across sessions, not yet across agents.
- Open notes piling up before ingest. One project reached 17 open notes within a week. Most were gotchas and
  decisions: ingest had never run, as no project was on the allow list and `fabric-ingest.timer` wasn't built, and the
  first run took 22 notes from four projects (2026-11-12). Of the project's 3 `status` notes, one was an unchecked
  assumption, which the skill now calls a gotcha, and one recorded a task's progress that the repo's task list didn't
  have yet, which the skill now sends to that list. Since then the timer runs ingest daily (§5), and its gate, 3 notes
  or one 7 days old, is the trigger by count, so notes wait at most a day past it. Open: whether that keeps a busy
  project's count down, which a few weeks of the timer show.

The phase 0 spike (§9) settles the remaining unknowns.
