#!/usr/bin/env bash
# Install kb from this clone and set this machine up for agent notes (docs/design.md §4.10): `uv tool install`,
# then `kb doctor --fix`, which creates the inbox, copies the kb-note skill into the agents' skills folders, adds
# the inbox to Claude Code's and Codex's sandbox write lists, and installs kb's hooks in each agent. Run it in your
# own terminal, not in an agent's sandbox, which can't write those settings. Rerun it after pulling to update kb,
# the skill and the hooks. What it prints is kb doctor's report, a TOON document an agent can read and explain.
# Usage: scripts/install.sh [--hooks start,stop|start|stop|none]
#   --hooks  which hooks to install (default: both, or what an earlier --hooks recorded)
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
uv tool install --reinstall --quiet "$root"
# ${hooks[@]+...}: bash before 4.4 (macOS has 3.2) calls an empty array unbound under set -u.
"$(uv tool dir --bin)/kb" doctor --fix ${hooks[@]+"${hooks[@]}"}
