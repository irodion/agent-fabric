#!/usr/bin/env bash
# Install kb from this clone and set this machine up for agent notes (docs/design.md §4.10, §4.11): `uv tool install`,
# then `kb setup`, which asks for the workspaces, their allow lists and backends, and the hooks, and applies them with
# `kb doctor --fix`: that creates each workspace's inbox, copies the kb-note skill into the agents' skills folders,
# adds the inboxes to Claude Code's and Codex's sandbox write lists, installs kb's hooks in each agent, and, once a
# project is allowed in a workspace, installs its fabric-ingest@ timer to run kb ingest daily. Run it in your own
# terminal, not in an agent's sandbox, which can't write those settings. Rerun it after pulling to update kb, the
# skill and the hooks: pressing Enter at each question keeps what's recorded. What it prints at the end is kb
# doctor's report, a TOON document an agent can read and explain.
# Usage: scripts/install.sh [--hooks start,stop|start|stop|none]
#   --hooks  skip kb setup's questions and install these hooks (default: both, or what was recorded); without a
#            terminal, kb doctor --fix runs in place of kb setup too
set -euo pipefail

usage() {
	sed -n 's/^# Usage: /usage: /p; s/^#   /  /p' "$0" >&2
	exit 2
}

hooks=()
while (($#)); do
	case $1 in
	--hooks)
		(($# >= 2)) || usage
		hooks=(--hooks "$2")
		shift 2
		;;
	--hooks=*)
		hooks=(--hooks "${1#--hooks=}")
		shift
		;;
	*) usage ;;
	esac
done
if ((${#hooks[@]})); then
	case ${hooks[1]} in
	start,stop | stop,start | start | stop | none) ;;
	*) usage ;;
	esac
fi

root=$(cd "$(dirname "$0")/.." && pwd)
if ! command -v uv >/dev/null; then
	# The same shape as kb doctor's report (docs/design.md §4.10).
	cat <<'REPORT'
summary: install.sh can't install kb without uv
next[1]{by,step}:
  user,"Install uv (https://docs.astral.sh/uv/getting-started/installation/), then run scripts/install.sh again"
help: "These steps are for the user: an agent's sandbox can't change the agents' settings. Agents: explain them to the user in plain words."
REPORT
	exit 1
fi

# Not --editable: agents keep the installed kb while this clone switches branches. Quiet: the report is the output.
# A tool install resolves afresh, past uv.lock and exclude-newer, so the lock's versions go in as constraints: what
# the vet gate checked is what runs.
pins=$(mktemp)
trap 'rm -f "$pins"' EXIT
uv export --directory "$root" --frozen --no-dev --no-emit-project --no-hashes --quiet -o "$pins"
uv tool install --reinstall --quiet --constraints "$pins" "$root"
kb="$(uv tool dir --bin)/kb"
if ((${#hooks[@]} == 0)) && [[ -t 0 && -t 1 ]]; then
	status=0
	"$kb" setup || status=$?
	# 3: kb setup can't edit the config, and changed nothing; the new kb still updates the skill copies and hooks,
	# and the report names what's wrong. Any other exit stands, such as 130 where the user left.
	((status == 3)) || exit "$status"
	echo "Applying the choices recorded before, with kb doctor --fix:" >&2
fi
# ${hooks[@]+...}: bash before 4.4 (macOS has 3.2) calls an empty array unbound under set -u.
"$kb" doctor --fix ${hooks[@]+"${hooks[@]}"}
