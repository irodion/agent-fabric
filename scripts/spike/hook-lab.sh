#!/usr/bin/env bash
# Run a real agent's hooks against this clone's kb, in a throwaway git repo and data folder, and show what they did:
# the markers, how `kb note` signs a note there, and the hook runs in the agent's log (docs/design.md §10). It needs
# the agent's servers and folders, so it runs outside an agent's sandbox. The agent gets a plain prompt and no shell.
# Each turn spends a model request, and a premium request in Copilot CLI.
# Usage: scripts/spike/hook-lab.sh <cursor|copilot|codex> [--stop]
#   --stop  also get the stop question: make the session 11 minutes old, change the repo's origin and take another turn
set -euo pipefail

usage() {
	sed -n 's/^# Usage: /usage: /p; s/^#   /  /p' "$0" >&2
	exit 2
}

agent=${1:-}
case $agent in
cursor | copilot | codex) ;;
*) usage ;;
esac
stop=false
case ${2:-} in
"") ;;
--stop) stop=true ;;
*) usage ;;
esac

repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
[[ -x $repo/.venv/bin/kb ]] || {
	echo "hook-lab: no $repo/.venv/bin/kb; run uv sync" >&2
	exit 1
}
[[ -w $HOME/.$agent ]] || {
	echo "hook-lab: can't write ~/.$agent; run this outside the sandbox" >&2
	exit 1
}

prompt="Reply with the single word: ok. Do not run any commands."
lab=$(cd "$(mktemp -d "${TMPDIR:-/tmp}/kb-lab-XXXXXX")" && pwd -P)
data=$lab/data
proj=$lab/proj
socket=$(basename "$lab")
chats=$HOME/.cursor/chats/$(printf %s "$proj" | md5sum | cut -d' ' -f1) # Cursor's folder for a project is its md5

copilot_session() { # the folder Copilot CLI keeps for this project's session
	local yaml
	yaml=$(grep -lx "cwd: $proj" "$HOME"/.copilot/session-state/*/workspace.yaml 2>/dev/null | head -1 || true)
	[[ -n $yaml ]] && dirname "$yaml"
}

codex_rollouts() { # the logs Codex keeps for this project's session: each names its folder, encoded as JSON
	local cwd
	cwd=$(jq -n --arg cwd "$proj" '$cwd')
	find "$HOME/.codex/sessions" -name 'rollout-*.jsonl' -newer "$lab" \
		-exec grep -lF "\"cwd\":$cwd" {} + 2>/dev/null || true
}

cleanup() { # everything here is named by the lab's unique path, so a real session is never touched
	local pid session
	cd /
	tmux -L "$socket" kill-server 2>/dev/null || true
	# Cursor leaves one running per project.
	for pid in $(pgrep -f '[w]orker-server' || true); do
		if [[ $(readlink "/proc/$pid/cwd" || true) == "$lab"* ]]; then
			kill "$pid" || true
		fi
	done
	rm -rf "$chats"
	find "$HOME/.cursor/projects" -maxdepth 1 -iname "*$socket*" -exec rm -rf {} + 2>/dev/null || true
	session=$(copilot_session || true)
	if [[ -n $session ]]; then
		rm -rf "$session"
	fi
	if [[ $agent == codex ]]; then
		codex_rollouts | xargs -r -d '\n' rm -f
	fi
	rm -rf "$lab"
}
trap cleanup EXIT

mkdir -p "$data/agent-fabric/inbox" "$proj/src"
git -C "$proj" init -q
git -C "$proj" remote add origin git@github.com:example/kb-lab.git
cd "$proj"

if [[ $agent == cursor ]]; then
	# Cursor's hooks don't follow a PATH prefix, so a project file names this clone's kb. The user's hooks file fires
	# too, with the installed kb, on the same data folder. Copilot CLI ignored a project file and does follow PATH.
	mkdir .cursor
	jq -n --arg kb "$repo/.venv/bin/kb" '{version: 1, hooks: {
		sessionStart: [{command: "\($kb) hook start --agent cursor"}],
		stop: [{command: "\($kb) hook stop --agent cursor"}]}}' >.cursor/hooks.json
fi

session_id() {
	case $agent in
	cursor) find "$chats" -mindepth 1 -maxdepth 1 -printf '%f\n' | head -1 ;;
	copilot) basename "$(copilot_session)" ;;
	codex) head -1 "$(codex_rollouts | head -1)" | jq -r .payload.id ;;
	esac
}

take_turn() { # [session id to resume]
	local id=${1:-}
	case $agent in
	cursor) XDG_DATA_HOME=$data timeout 90 cursor-agent -p --trust ${id:+--resume "$id"} "$prompt" ;;
	copilot) XDG_DATA_HOME=$data PATH=$repo/.venv/bin:$PATH timeout 90 copilot ${id:+--resume="$id"} -p "$prompt" ;;
	# With stdin an open pipe, as in an agent's shell, `codex exec` waits for its end before it starts.
	codex) XDG_DATA_HOME=$data PATH=$repo/.venv/bin:$PATH timeout 90 codex exec ${id:+resume "$id"} "$prompt" </dev/null ;;
	esac
}

markers() {
	local marker
	for marker in "$data"/agent-fabric/hooks/*.json; do
		[[ -e $marker ]] || {
			echo "no markers: the hook didn't run"
			return
		}
		printf '%s  %s\n' "$(basename "$marker" | cut -c1-16)" "$(cat "$marker")"
	done
}

clean_kb() { # kb in a shell with none of the agent's variables
	env -i HOME="$HOME" XDG_DATA_HOME="$data" PATH="$repo/.venv/bin:/usr/bin:/bin" kb "$@"
}

sign() { # label [kb note arguments]: how the note is signed
	local label=$1 out
	shift
	if out=$(clean_kb note --kind gotcha "$@" "lab note: $label" 2>&1); then
		printf '%-15s %s\n' "$label" "$(sed -n 's/^agent: //p' "$(tail -1 <<<"$out" | cut -d' ' -f2)")"
	else
		printf '%-15s exit %s: %s\n' "$label" "$?" "$(cut -c1-90 <<<"$out")"
	fi
}

hook_log() { # Copilot CLI records each hook's output; Cursor's store.db is blobs, so its markers have to do
	[[ $agent == copilot ]] || return 0
	jq -r 'select(.type == "hook.end") |
		"\(.data.hookType) success=\(.data.success) \(.data.output // {} | tostring | .[0:150])"' \
		"$(copilot_session)/events.jsonl"
}

backdate() { # past the 10 minutes the stop hook waits for
	local marker
	for marker in "$data"/agent-fabric/hooks/"$agent"-*.json; do
		jq --arg started "$(date -u -d '-11 minutes' +%FT%T+00:00)" '.started = $started' "$marker" >"$marker.new"
		mv "$marker.new" "$marker"
	done
}

asked() {
	grep -q '"asked": true' "$data"/agent-fabric/hooks/"$agent"-*.json 2>/dev/null
}

followed() { # the marker took up the origin changed before the last turn
	grep -q 'kb-lab-moved' "$data"/agent-fabric/hooks/"$agent"-*.json 2>/dev/null
}

interactive_cursor_turn() { # `-p` runs don't get the stop question: drive a real session in a private tmux server
	local id=$1
	pane() { tmux -L "$socket" capture-pane -p -t lab 2>/dev/null; }
	tmux -L "$socket" new-session -d -s lab -x 150 -y 45 -c "$proj" \
		"XDG_DATA_HOME='$data' cursor-agent --resume '$id'"
	for _ in {1..15}; do
		if pane | grep -qiE 'plan, search|add a follow|ask anything|follow-up'; then break; fi
		sleep 2
	done
	tmux -L "$socket" send-keys -t lab "$prompt"
	sleep 1
	tmux -L "$socket" send-keys -t lab Enter
	for _ in {1..50}; do
		if asked; then break; fi
		sleep 2
	done
	sleep 20 # its answer to the question
	pane | grep -v '^[[:space:]]*$' | tail -12 | cut -c1-140
	tmux -L "$socket" kill-server 2>/dev/null || true
}

echo "=== $agent, this clone's kb, in $lab"
take_turn 2>&1 | tail -3 | cut -c1-140 || echo "the agent exited with an error"
echo "--- markers after the start hook"
markers
echo "--- kb note in a clean shell, in the lab's repo"
sign plain
(cd src && sign "from src")
sign "--agent Codex" --agent Codex
echo "--- kb inbox"
clean_kb inbox | grep -E '^n[0-9a-f]{6}' | cut -c9-100
echo "--- hook runs in the agent's log"
hook_log

if $stop; then
	id=$(session_id)
	echo "=== another turn in session $id, 11 minutes on, with the repo's origin changed"
	backdate
	git -C "$proj" remote set-url origin git@github.com:example/kb-lab-moved.git
	if [[ $agent == cursor ]]; then
		interactive_cursor_turn "$id"
	else
		take_turn "$id" 2>&1 | tail -6 | cut -c1-140 || echo "the agent exited with an error"
	fi
	echo "--- markers after the stop hook"
	markers
	echo "--- kb note in a clean shell, under the new origin"
	sign "--agent Codex" --agent Codex
	echo "--- hook runs in the agent's log"
	hook_log
	if asked; then echo "the stop question was asked"; else echo "the stop question was NOT asked"; fi
	if followed; then echo "the marker followed the new origin"; else echo "the marker did NOT follow the new origin"; fi
fi
