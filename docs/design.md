# agent-fabric — design

Status: draft v0.4, 2026-10-31 (v0.3 had two review rounds; v0.4 puts the CLI agents first and adds the CLI
capture check, §4.1). Name: agent-fabric; CLI: `kb`.
Background research: `~/code/reports/Cross agent coding memory harness.md`.

## 1. Goal

Switch freely between coding agents without losing the history of what was done and decided: first the CLI agents
(Claude Code, Codex, Copilot CLI, Cursor CLI), later the IDE agents (Cursor IDE, VS Code Copilot Chat). Concretely:

1. Every session leaves a deterministic, redacted **digest** in a Markdown wiki repo on GitHub.
2. A **live panel** beside the agent lists decisions, risky actions and open loops ("disabled test, claimed
   temporary — verify later") while the session runs.
3. An LLM periodically folds digests into curated **wiki pages** (manual trigger + nightly), reviewed before merge.
4. Agents and I **pull** history on demand through one CLI (`kb`) and one MCP server. Nothing is injected into
   agent prompts automatically.

### Non-goals

- Replacing or wrapping the agents. They run in their native UIs, untouched.
- Blocking or approving agent actions. The panel is advisory only.
- Multi-user or multi-machine sync (one Linux desktop for now).
- Parsing vendor storage formats ourselves (delegated to agentsview, §4.1).

## 2. Context and decisions so far

| Topic | Decision | Why |
|---|---|---|
| Host | Linux desktop (Ubuntu 24.04 or newer), Core Ultra 7 265H (Arc 140T iGPU), 64 GB RAM; all agents run locally on it | IDE chat history lives where the IDE runs |
| Agents first | The CLI agents: Claude Code, Codex, Copilot CLI, Cursor CLI; the IDE agents in phase 3 | Current focus (2026-10-31) |
| Capture | agentsview, built on CI from a pinned tag, sandboxed, behind an adapter; Cursor CLI through its own reader | agentsview parses Claude Code, Codex, Copilot CLI, Cursor IDE and VS Code Copilot Chat on Linux; for Cursor CLI it drops tool results and fails under a read-only home (§4.1) |
| Supply chain | SafeDep `vet` gate on agentsview's dependencies and on ours from the first commit | One reviewed build per version; malware and fresh-package checks before anything runs on the desktop |
| Publishing | Allow list of projects; everything else stays local | Client work never reaches GitHub by default |
| Repos | `agent-fabric` (code) public, MIT; the wiki repo private | Code is shareable; session data never is |
| Session end | Idle timeout, not session-end hooks | Cursor's `sessionEnd` is unreliable; Codex/Claude give hooks 1–3 s |
| Compaction | Deterministic extraction; no LLM in the digest | Facts are extractable; only "why" needs a model |
| Retrieval | Pull only (CLI/MCP), manually triggered | Avoids context pollution and memory-poisoning via injection |
| Wiki ingest | Manual (`kb ingest`) and nightly, onto a review branch | Review gate before anything becomes canon |
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
 │ wiki repo (~/wiki, private)  │<──────│ kb ingest (manual/nightly)│
 │ raw/ digests · wiki/ pages   │ branch│ codex exec | local 26B    │
 └──────────────┬───────────────┘       └───────────────────────────┘
                │ push (timer)
             GitHub

 Query: kb CLI / `kb mcp` (wiki, digests, loops)  +  `agentsview mcp --profile memory` (raw transcripts)
```

One long-running Python daemon (`fabricd`) hosts tracker, observer, digester and panel. Ingest is a separate
short-lived job. The `kb` CLI and `kb mcp` read the same local files.

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
4. `fabric-push.timer` pushes `main` every 30 min. Failures retry; commits accumulate locally.

Local-only digests go through step 1 as well, so `kb` output never shows raw secrets.

Single writer: all commits to `main` go through `fabricd` holding a lock file; ingest writes only to its branch in
a separate worktree.

### 4.6 Wiki repo

One private repo for all projects (`~/wiki`, remote on GitHub).

```
AGENTS.md          schema + rules for the ingest LLM (page types, linking, citation, "replace don't append")
index.md           generated catalogue (never hand-edited)
log.md             append-only, one line per digest/ingest (merge=union in .gitattributes)
raw/sessions/...   digests (§4.4)
wiki/projects/<project>.md       overview, current state, conventions, recurring problems
wiki/decisions/<project>/<slug>.md   decision, alternatives, rationale, sessions that support it
wiki/concepts/<slug>.md          cross-project knowledge ("pnpm workspace quirks")
wiki/retros/<project>/<date>.md  retro findings (§4.7)
wiki/archive/...                 pages retired by pruning (§4.7)
```

Every claim in `wiki/` links to the digest(s) it came from. Per-directory entry counts stay under GitHub's 3,000
via the year/month sharding. The wiki only grows unless it is pruned; lint and prune are in §4.7.

### 4.7 Ingest, lint and prune, retro

`kb ingest [--project P] [--since DATE] [--backend codex|local] [--dry-run]`; `fabric-ingest.timer` runs it at
03:00.

1. Pending = digests whose `(path, revision)` is not yet in a merged ingest (ledger in `state.db`).
2. Use worktree `~/.local/share/agent-fabric/ingest-wt` on branch `ingest/<yyyy-mm-dd>`. If an unmerged ingest
   branch exists, keep appending to it instead of starting a new one.
3. Run the backend with `AGENTS.md` as instructions and the pending digests as input. It may only touch `wiki/`,
   `index.md` and `log.md`.
   - `codex` (default): `codex exec -C <worktree> -s workspace-write --ephemeral -o <summary.md> -` with the prompt
     on stdin. `--ephemeral` keeps ingest runs out of the captured history.
   - `local`: Gemma 4 26B-A4B on llama-server, driven by a small Python loop (read pages → propose full page
     replacements as JSON → apply). For a two-week comparison against `codex` on the same batches.
4. Redaction gate (§4.5) over every changed file, then commit on the branch. Optionally push the branch and open a PR.
5. I review (tuicr or the PR) and merge. On merge the ledger marks those digests ingested. A deleted, unmerged
   branch returns its digests to pending.

The ingest LLM sees digests only, plus the existing wiki pages; for detail it can call `agentsview mcp`.

**Lint and prune.** Without pruning the wiki fills up with stale and duplicate pages. This follows the "lint"
operation in Karpathy's gist, in two steps:

- `kb lint` (deterministic; nightly after ingest, also part of `kb doctor`) reports broken links, claims citing
  missing digests, orphan pages, oversized pages, pages whose newest citation is older than 90 days, decisions
  contradicted by a newer decision page, and near-duplicate titles.
- `kb prune` (manual + monthly timer) is an LLM pass on branch `prune/<yyyy-mm>` that works from the lint report.
  It merges duplicates, marks superseded decisions, condenses long pages and moves pages of projects inactive for
  6 months to `wiki/archive/`. It goes through the same review and merge as ingest.

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

The LLM (same backends as ingest) turns these into candidates ordered by severity, each citing digests, and writes
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

`index.db` (rebuildable from the wiki repo + `state.db`): FTS5 tables over digests and wiki pages (porter tokenizer
for prose, trigram for paths/commands/errors), plus relational tables from digest front matter (`sessions`,
`files`, `commands`, `tests`, `loops`).

| CLI | MCP tool (`kb mcp`, stdio, read-only) | Returns |
|---|---|---|
| `kb catchup [--project .]` | `catchup(project)` | open loops + last 3 digests' summaries + project page excerpt |
| `kb search <q> [--project] [--since] [--kind]` | `search(query, project?, since?, kind?)` | ranked snippets with paths |
| `kb sessions [--file F] [--failing-tests] [--since]` | `list_sessions(...)` | structured session rows |
| `kb show <path\|session[#ordinal]>` | `get_page(path)` | full digest or wiki page; with `#ordinal`, that message's full output from agentsview |
| `kb loops [--all]` | `open_loops(project?)` | loops with evidence |
| `kb loop close <id>` | — | (CLI only; agents can't close loops) |
| `kb ingest`, `kb lint`, `kb prune`, `kb retro`, `kb reindex`, `kb doctor` | — | maintenance (§4.7) |

Project defaults to the git root of the current directory. MCP results are labelled as historical notes (untrusted
content), mirroring OpenHands' `<UNTRUSTED_CONTENT>` wrapping.

Registration (manual, once): Codex `~/.codex/config.toml` `[mcp_servers.kb]`, Cursor `~/.cursor/mcp.json`, VS Code
user `mcp.json`; each gets `kb` and `agentsview` (`mcp --profile memory --server http://127.0.0.1:<port>`, §4.1).
The manual trigger is me telling the agent "run `kb catchup`" or "check kb for …"; per-agent slash commands can
wrap that later.

## 5. Processes, ports, locations

| Unit (systemd --user) | Command | Bind |
|---|---|---|
| `agentsview.service` (sandboxed; user or system unit, §4.1) | `agentsview serve --no-browser --no-update-check --port <port> --replace` | `127.0.0.1:<port>` (8080 by default) |
| `fabricd.service` | tracker, observer, digester, panel | 127.0.0.1:8090 |
| `llama-panel.service` | `llama-server` with E4B | 127.0.0.1:8091 |
| `fabric-ingest.timer` | `kb ingest` at 03:00 (starts a 26B llama-server on 8092 if `--backend local`) | — |
| `fabric-push.timer` | push wiki `main` every 30 min | — |
| `fabric-prune.timer` | `kb lint` nightly after ingest; `kb prune` monthly | — |
| `fabric-backup.timer` | daily SQLite backup of agentsview archive and `state.db` | — |

| Path | Contents | In git |
|---|---|---|
| `~/.config/agent-fabric/config.toml` | settings (allow lists, idle timeout, ports, backends, project aliases) | no |
| `~/.local/share/agent-fabric/state.db` | cursors, loops, ingest ledger, flags | no (backed up) |
| `~/.local/share/agent-fabric/index.db` | search index | no (rebuildable) |
| `~/.local/share/agent-fabric/quarantine/` | digests blocked by gitleaks | no |
| `~/.local/share/agent-fabric/local/` | digests of projects not on the allow list | no (backed up) |
| `~/.agentsview/` | agentsview config + archive | no (backed up) |
| `~/wiki/` | wiki repo | yes |

## 6. Stack

Python 3.13+ with uv; `httpx` + `httpx-sse` (adapter), `starlette` + `uvicorn` (panel), `mcp` official SDK (MCP
server), `pydantic` (schemas), stdlib `sqlite3` (FTS5). External binaries: agentsview (CI-built, pinned),
llama.cpp `llama-server` (SYCL or Vulkan build), `gitleaks`, `git`, `codex`. Dev: ruff, mypy, pytest, pinned per
project. CI: GitHub Actions with the `vet` gate (§4.1) and `lint` (shellcheck, shfmt, actionlint, zizmor; ruff,
mypy and pytest join it with the Python core).

## 7. Security and privacy

- Everything binds to 127.0.0.1. No auth needed while loopback-only; agentsview's `--require-auth` if that changes.
- Redaction before write, gitleaks gate, pre-commit backstop (§4.5). Private repo is not treated as secret-safe.
- Allow list: only listed projects are published (§4.5).
- The code repo is public: no session data, config, digests or wiki content ever goes into it, and its CI has no
  access to the wiki repo. Fixtures for tests are synthetic or scrubbed recordings.
- Untrusted content: the ingest LLM is sandboxed to its worktree; MCP output is labelled historical; nothing is
  auto-injected.
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
  that setting says. Repo-level Copilot settings can't override it: keys not on their list are ignored. Copilot CLI
  1.0.105 on the devbox keeps the key in `~/.copilot/config.json`, a file whose header says it is managed
  automatically, so `kb doctor` checks both files and warns if the setting is missing or `true`. Test and scripted
  runs also pass `--no-remote-export`. The docs don't say whether turning sync off deletes copies already synced, so
  check github.com once.

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
| push fails | commits pile up locally | timer retries |
| ingest branch left unmerged | wiki stale | nightly appends to same branch; `kb doctor` warns after 7 days |
| vendor prunes/migrates history | source files gone | agentsview archive + daily backup keep the raw layer |
| own automated runs captured | feedback loop | `codex exec --ephemeral`; ignore `is_automated` sessions |

## 9. Phases

**Phase 0 — spike (1–2 days).** On the desktop:
1. agentsview captures one session each from Cursor IDE, VS Code Copilot Chat and Codex, with tool calls and results.
2. `agentsview session watch` lag during live Cursor IDE and Copilot Chat sessions (target ≤ 10 s).
3. `agentsview mcp --profile memory` from Codex finds a Cursor session.
4. Record API responses as adapter fixtures; check `/api/v1/events` semantics and the turn-end heuristic.
5. `llama-bench` E2B / E4B / 26B-A4B on SYCL vs Vulkan.
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

**Phase 1 — digests and query.** CI with the `vet` gate for this repo; the agentsview pin bumped to v0.46.x
(from 2026-11-06, §4.1); adapter, normalizer, tracker, digester with allow list/redaction/gate/commit/push, for
the CLI agents: agentsview for Claude Code, Codex and Copilot CLI, `CursorChatSource` for Cursor CLI; wiki repo
skeleton, `kb` CLI + `kb mcp`; a prototype session for the panel layout, then the panel with rules only (incl. open
loops and carry-over).

**Phase 2 — models.** SLM bullets in the panel after it passes the eval (§10); `kb ingest` manual + nightly with
`codex` backend; two-week comparison with `local` backend; `kb lint`, `kb prune` and `kb retro`.

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
- Project identity: the same repo as a main checkout, a linked worktree, a second clone, an SSH and an HTTPS remote,
  and a folder with no remote, each resolving to the expected id; an alias merges two ids.
- Redaction: fixture corpus of fake secrets in every supported shape; test fails if gitleaks finds anything after
  redaction.
- SLM: labelled set of ~30 real sessions from phase 1 (plus a few ImpossibleBench trajectories). Score recall of
  decisions and reason links, precision of bullets, latency per batch. Initial adoption bar: ≥ 70% decision recall,
  ≤ 1 wrong bullet per 10, ≤ 10 s per batch.
- Ingest: compare `codex` vs `local` pages on the same batches by review effort (edits needed before merge).

## 11. Open questions

Decided in review (2026-10-23): the name is agent-fabric with the CLI `kb`; prompts stay verbatim, and an allow
list decides what is published (§4.5); the idle timeout is a config value (§4.3); the panel layout is settled in
the prototype session (§4.8); the minimum desktop is Ubuntu 24.04 (§4.1, glibc); project ids come from the
`origin` remote, then the main checkout, then agentsview's project, with aliases in config (§4.4); Copilot
session sync is off (§7). Decided 2026-10-31: the CLI agents come first, the IDE agents in phase 3 (§2).

Open (2026-10-31):

- Headless runs stay local unless a project opts in (§4.3). Proposed, not yet confirmed.
- What marks a Copilot or Cursor run as headless (§4.1).
- The Cursor CLI reader: message order from the `store.db` blob tree, timestamps, and encrypted blobs (§4.1).

The phase 0 spike (§9) settles the remaining unknowns.
