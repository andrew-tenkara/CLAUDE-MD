#!/usr/bin/env bash
# ── USS Tenkara — XO Tools ──────────────────────────────────────────
#
# Helper commands for the Mini Boss (XO) to manage the TUI dashboard
# from the command line. The TUI watches .sortie/ files, so writing
# to them is the communication channel.
#
# Usage:
#   bash xo-tools.sh <command> [args...]
#
# Commands:
#   set-status <worktree> <status> [reason]   — Override agent status
#   dismiss <worktree> [reason]               — Force RECOVERED + cleanup
#   board                                     — Show current board state
#   board-json                                — Board state as JSON
#   sentinel-status                           — Sentinel health check
#   kick-sync                                 — Force dashboard to re-sync
#   clear-stale                               — Dismiss all RECOVERED agents
#   queue-list                                — Show mission queue
#   queue-remove <ticket-id>                  — Remove from queue
#   server-cmd                                — Show current dev server command
#   server-cmd set <command>                  — Set dev server command
#   server-cmd detect                         — Re-run auto-detection

set -euo pipefail

# ── Resolve directories ──────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_LIB="$(cd "$SCRIPT_DIR/../lib" && pwd)"
PROJECT_DIR="${SORTIE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
SORTIE_DIR="$PROJECT_DIR/.sortie"

# ── Helpers ──────────────────────────────────────────────────────────

_find_worktree() {
    local identifier="$1"
    # Direct path
    if [ -d "$identifier/.sortie" ]; then
        echo "$identifier"
        return
    fi
    # Search .claude/worktrees/
    local wt_dir="$PROJECT_DIR/.claude/worktrees/$identifier"
    if [ -d "$wt_dir" ]; then
        echo "$wt_dir"
        return
    fi
    # Search by ticket ID in worktree names
    for d in "$PROJECT_DIR"/.claude/worktrees/*/; do
        if [ -d "$d" ] && [[ "$(basename "$d")" == *"$identifier"* ]]; then
            echo "${d%/}"
            return
        fi
    done
    # Try git worktree list
    git -C "$PROJECT_DIR" worktree list --porcelain 2>/dev/null | grep "^worktree " | awk '{print $2}' | while read wt; do
        if [[ "$wt" == *"$identifier"* ]]; then
            echo "$wt"
            return
        fi
    done
    echo ""
}

_timestamp() {
    date +%s
}

# ── Commands ─────────────────────────────────────────────────────────

cmd_set_status() {
    local identifier="${1:?Usage: set-status <worktree|ticket> <status> [reason]}"
    local status="${2:?Usage: set-status <worktree|ticket> <status> [reason]}"
    local reason="${3:-XO override}"
    local worktree
    worktree=$(_find_worktree "$identifier")

    if [ -z "$worktree" ]; then
        echo "ERROR: Could not find worktree for '$identifier'"
        echo "Available worktrees:"
        git -C "$PROJECT_DIR" worktree list 2>/dev/null || echo "  (none)"
        return 1
    fi

    status=$(echo "$status" | tr '[:lower:]' '[:upper:]')

    # Validate status
    case "$status" in
        AIRBORNE|IDLE|RECOVERED|ON_APPROACH|MAYDAY|AAR|SAR|PREFLIGHT) ;;
        *) echo "ERROR: Invalid status '$status'. Valid: AIRBORNE, IDLE, RECOVERED, ON_APPROACH, MAYDAY, AAR, SAR, PREFLIGHT"; return 1 ;;
    esac

    mkdir -p "$worktree/.sortie"
    echo "{\"set_status\": \"$status\", \"reason\": \"$reason\", \"source\": \"XO\"}" > "$worktree/.sortie/command.json"
    echo "✓ Set $identifier → $status ($reason)"
    echo "  Written: $worktree/.sortie/command.json"
    echo "  Dashboard will pick this up on next sync cycle (~5s)"
}

cmd_dismiss() {
    local identifier="${1:?Usage: dismiss <worktree|ticket> [reason]}"
    local reason="${2:-dismissed by XO}"
    local worktree
    worktree=$(_find_worktree "$identifier")

    if [ -z "$worktree" ]; then
        echo "ERROR: Could not find worktree for '$identifier'"
        return 1
    fi

    # Set RECOVERED via command.json
    mkdir -p "$worktree/.sortie"
    echo "{\"set_status\": \"RECOVERED\", \"reason\": \"$reason\", \"source\": \"XO\"}" > "$worktree/.sortie/command.json"

    # Also touch session-ended so it sticks across sync cycles
    touch "$worktree/.sortie/session-ended"

    echo "✓ Dismissed $identifier"
    echo "  Written: command.json (RECOVERED) + session-ended"
    echo "  Dashboard will pick this up on next sync cycle (~5s)"
    echo "  Then hit Z in the TUI to remove from board"
}

cmd_board() {
    echo "═══ USS TENKARA — FLIGHT DECK ═══"
    echo ""

    # Read sortie state
    python3 -c "
import sys, json
sys.path.insert(0, '$SKILL_LIB')
try:
    from read_sortie_state import read_sortie_state
    state = read_sortie_state(project_dir='$PROJECT_DIR')
    if not state.agents:
        print('  No agents on deck.')
    else:
        for a in state.agents:
            ctx = a.context or {}
            fuel = max(0, 100 - int(ctx.get('used_percentage', 50)))
            model = a.model or '?'
            status = a.status or '?'
            fs = a.flight_status or ''
            phase = a.flight_phase or ''
            # Check sentinel
            sentinel = ''
            try:
                import pathlib
                ss = json.loads((pathlib.Path(a.worktree_path) / '.sortie/sentinel-status.json').read_text())
                sentinel = f'  sentinel:{ss[\"status\"]}'
            except: pass
            ended = ' SESSION-ENDED' if a.session_ended else ''
            print(f'  {a.ticket_id:<12} {status:<12} fuel:{fuel:>3}%  model:{model:<7} {fs} {phase}{sentinel}{ended}')
            if a.worktree_path:
                print(f'    └─ {a.worktree_path}')
except Exception as e:
    print(f'  Error reading state: {e}')
" 2>/dev/null

    echo ""

    # Mission queue
    local queue_dir="$SORTIE_DIR/mission-queue"
    if [ -d "$queue_dir" ]; then
        local count
        count=$(ls "$queue_dir"/*.json 2>/dev/null | wc -l | tr -d ' ')
        if [ "$count" -gt 0 ]; then
            echo "═══ MISSION QUEUE ($count) ═══"
            for f in "$queue_dir"/*.json; do
                local tid title priority model
                tid=$(python3 -c "import json; d=json.load(open('$f')); print(d.get('id','?'))" 2>/dev/null)
                title=$(python3 -c "import json; d=json.load(open('$f')); print(d.get('title','?')[:50])" 2>/dev/null)
                priority=$(python3 -c "import json; d=json.load(open('$f')); print(d.get('priority',2))" 2>/dev/null)
                model=$(python3 -c "import json; d=json.load(open('$f')); print(d.get('model','sonnet'))" 2>/dev/null)
                echo "  P$priority  $tid  [$model]  $title"
            done
        fi
    fi
}

cmd_board_json() {
    python3 -c "
import sys, json
sys.path.insert(0, '$SKILL_LIB')
from read_sortie_state import read_sortie_state
state = read_sortie_state(project_dir='$PROJECT_DIR')
agents = []
for a in state.agents:
    ctx = a.context or {}
    agents.append({
        'ticket_id': a.ticket_id,
        'status': a.status,
        'fuel': max(0, 100 - int(ctx.get('used_percentage', 50))),
        'model': a.model,
        'flight_status': a.flight_status,
        'flight_phase': a.flight_phase,
        'session_ended': a.session_ended,
        'worktree': a.worktree_path,
    })
print(json.dumps(agents, indent=2))
" 2>/dev/null
}

cmd_sentinel_status() {
    echo "═══ SENTINEL STATUS ═══"
    local hb="$SORTIE_DIR/sentinel-heartbeat.json"
    if [ -f "$hb" ]; then
        python3 -c "
import json, time
d = json.load(open('$hb'))
age = int(time.time()) - d.get('ts', 0)
pid = d.get('pid', '?')
watching = d.get('watching', [])
status = '● ALIVE' if age < 60 else '✗ STALE'
print(f'  {status}  PID:{pid}  age:{age}s  watching:{len(watching)} agents')
print(f'  Classifier: deterministic (rule-based)')
for w in watching:
    print(f'    └─ {w}')
" 2>/dev/null
    else
        echo "  ✗ No heartbeat file — sentinel not running"
    fi

    echo ""
    echo "Per-agent sentinel status:"
    git -C "$PROJECT_DIR" worktree list --porcelain 2>/dev/null | grep "^worktree " | awk '{print $2}' | while read wt; do
        local ss="$wt/.sortie/sentinel-status.json"
        if [ -f "$ss" ]; then
            python3 -c "
import json, time
d = json.load(open('$ss'))
age = int(time.time()) - d.get('timestamp', 0)
fresh = '●' if age < 90 else '○'
print(f'  {fresh} $(basename "$wt")  {d.get(\"status\",\"?\")}  {d.get(\"phase\",\"\")}  ({age}s ago)')
" 2>/dev/null
        fi
    done
}

cmd_kick_sync() {
    # Touch a watched file to trigger the debounced refresh
    mkdir -p "$SORTIE_DIR"
    touch "$SORTIE_DIR/managed-servers.json"
    echo "✓ Kicked sync — dashboard will refresh within ~1s"
}

cmd_clear_stale() {
    echo "Scanning for session-ended worktrees..."
    local cleared=0
    git -C "$PROJECT_DIR" worktree list --porcelain 2>/dev/null | grep "^worktree " | awk '{print $2}' | while read wt; do
        if [ -f "$wt/.sortie/session-ended" ]; then
            local name
            name=$(basename "$wt")
            echo "  ✓ $name — already session-ended"
            cleared=$((cleared + 1))
        fi
    done

    # Also mark any agents with no active process
    for wt_dir in "$PROJECT_DIR"/.claude/worktrees/*/; do
        if [ -d "$wt_dir/.sortie" ] && [ ! -f "$wt_dir/.sortie/session-ended" ]; then
            # Check if there's actually a claude process running in this worktree
            local has_process=false
            if pgrep -f "$(basename "$wt_dir")" > /dev/null 2>&1; then
                has_process=true
            fi
            if [ "$has_process" = false ]; then
                touch "$wt_dir/.sortie/session-ended"
                echo "  ✓ $(basename "$wt_dir") — marked session-ended (no active process)"
            fi
        fi
    done

    cmd_kick_sync
}

cmd_queue_list() {
    local queue_dir="$SORTIE_DIR/mission-queue"
    if [ ! -d "$queue_dir" ] || [ -z "$(ls "$queue_dir"/*.json 2>/dev/null)" ]; then
        echo "Mission queue is empty."
        return
    fi

    echo "═══ MISSION QUEUE ═══"
    for f in "$queue_dir"/*.json; do
        python3 -c "
import json
d = json.load(open('$f'))
tid = d.get('id', '?')
title = d.get('title', '?')[:55]
pri = d.get('priority', 2)
model = d.get('model', 'sonnet')
print(f'  P{pri}  {tid:<12}  [{model}]  {title}')
" 2>/dev/null
    done
}

cmd_queue_remove() {
    local ticket_id="${1:?Usage: queue-remove <ticket-id>}"
    local queue_file="$SORTIE_DIR/mission-queue/$ticket_id.json"
    if [ -f "$queue_file" ]; then
        rm "$queue_file"
        echo "✓ Removed $ticket_id from queue"
        cmd_kick_sync
    else
        echo "ERROR: $ticket_id not found in queue"
        echo "Available:"
        ls "$SORTIE_DIR/mission-queue/"*.json 2>/dev/null | xargs -I{} basename {} .json | sed 's/^/  /'
    fi
}

# ── Tail agent JSONL stream ───────────────────────────────────────────

cmd_tail_agent() {
    local identifier="${1:?Usage: tail-agent <worktree|ticket>}"
    local worktree
    worktree=$(_find_worktree "$identifier")

    if [ -z "$worktree" ]; then
        echo "ERROR: Could not find worktree for '$identifier'"
        return 1
    fi

    # Find the JSONL file for this worktree
    python3 -c "
import sys
sys.path.insert(0, '$SKILL_LIB')
from parse_jsonl_metrics import encode_project_path, CLAUDE_PROJECTS_DIR, find_latest_session_file
jsonl = find_latest_session_file('$worktree')
if jsonl:
    print(str(jsonl))
else:
    encoded = encode_project_path('$worktree')
    jsonl_dir = CLAUDE_PROJECTS_DIR / encoded
    print(f'NO_JSONL:{jsonl_dir}')
" 2>/dev/null | {
        read jsonl_path
        if [[ "$jsonl_path" == NO_JSONL:* ]]; then
            local dir="${jsonl_path#NO_JSONL:}"
            echo "No JSONL session file found yet."
            echo "Expected directory: $dir"
            echo "Watching for creation..."
            # Watch the directory for new files
            if [ -d "$dir" ]; then
                tail -f "$dir"/*.jsonl 2>/dev/null
            else
                echo "Directory doesn't exist yet. Agent may not have started."
            fi
        else
            echo "═══ TAILING: $jsonl_path ═══"
            echo "(Ctrl+C to stop)"
            echo ""
            # Show last 20 lines then follow
            tail -20f "$jsonl_path" | python3 -c "
import sys, json
for line in sys.stdin:
    try:
        d = json.loads(line.strip())
        t = d.get('type', '?')
        if t == 'assistant':
            msg = d.get('message', {})
            content = msg.get('content', [])
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'text':
                    text = block['text'][:120]
                    print(f'  ✦ {text}')
                elif isinstance(block, dict) and block.get('type') == 'tool_use':
                    name = block.get('name', '?')
                    inp = block.get('input', {})
                    fp = inp.get('file_path', inp.get('command', inp.get('pattern', '')))[:80]
                    print(f'  ⚙ {name}: {fp}')
        elif t == 'result':
            # tool result — show truncated
            pass
        else:
            print(f'  [{t}]')
    except (json.JSONDecodeError, KeyError):
        pass
" 2>/dev/null
        fi
    }
}

# ── Reassign model ───────────────────────────────────────────────────

cmd_reassign_model() {
    local identifier="${1:?Usage: reassign-model <worktree|ticket> <model>}"
    local model="${2:?Usage: reassign-model <worktree|ticket> <model>}"
    local worktree
    worktree=$(_find_worktree "$identifier")

    if [ -z "$worktree" ]; then
        echo "ERROR: Could not find worktree for '$identifier'"
        return 1
    fi

    model=$(echo "$model" | tr '[:upper:]' '[:lower:]')
    case "$model" in
        opus|sonnet|haiku) ;;
        *) echo "ERROR: Invalid model '$model'. Valid: opus, sonnet, haiku"; return 1 ;;
    esac

    mkdir -p "$worktree/.sortie"
    echo "$model" > "$worktree/.sortie/model.txt"
    echo "✓ Reassigned $(basename "$worktree") → $model"
    echo "  Written: $worktree/.sortie/model.txt"
    echo "  Note: Takes effect on next agent launch/resume, not mid-flight."
}

# ── Inject message ───────────────────────────────────────────────────

cmd_inject() {
    local identifier="${1:?Usage: inject <worktree|ticket> <message>}"
    shift
    local message="$*"
    if [ -z "$message" ]; then
        echo "ERROR: No message provided"
        echo "Usage: inject <worktree|ticket> <message>"
        return 1
    fi

    local worktree
    worktree=$(_find_worktree "$identifier")

    if [ -z "$worktree" ]; then
        echo "ERROR: Could not find worktree for '$identifier'"
        return 1
    fi

    mkdir -p "$worktree/.sortie"
    local ts
    ts=$(_timestamp)
    python3 -c "
import json
msg = {
    'type': 'xo_directive',
    'message': '''$message''',
    'source': 'XO',
    'timestamp': $ts,
    'priority': 'normal'
}
print(json.dumps(msg))
" > "$worktree/.sortie/xo-message.json"

    echo "✓ Message queued for $(basename "$worktree")"
    echo "  Note: Agent reads this on next .sortie/ check cycle."
    echo "  For stream-json agents, use the chat pane instead (D key in TUI)."
}

# ── Health check ─────────────────────────────────────────────────────

cmd_health() {
    echo "═══ USS TENKARA — HEALTH CHECK ═══"
    echo ""

    # 1. Sentinel
    echo "── SENTINEL ──"
    local hb="$SORTIE_DIR/sentinel-heartbeat.json"
    if [ -f "$hb" ]; then
        python3 -c "
import json, time
d = json.load(open('$hb'))
age = int(time.time()) - d.get('ts', 0)
pid = d.get('pid', '?')
watching = len(d.get('watching', []))
alive = age < 60
# Check if PID actually exists
import os
try:
    os.kill(int(pid), 0)
    pid_alive = True
except:
    pid_alive = False
if alive and pid_alive:
    print(f'  ● SENTINEL: alive (PID {pid}, heartbeat {age}s ago, watching {watching} agents)')
elif pid_alive:
    print(f'  ⚠ SENTINEL: PID {pid} exists but heartbeat stale ({age}s)')
else:
    print(f'  ✗ SENTINEL: dead (PID {pid} gone, heartbeat {age}s ago)')
print(f'  Classifier: deterministic (rule-based, no LLM)')
" 2>/dev/null
    else
        echo "  ✗ SENTINEL: no heartbeat file — not running"
    fi
    echo ""

    # 2. Dashboard TUI
    echo "── DASHBOARD ──"
    if pgrep -f "commander-dashboard" > /dev/null 2>&1; then
        local tui_pid
        tui_pid=$(pgrep -f "commander-dashboard" | head -1)
        echo "  ● TUI: running (PID $tui_pid)"
    else
        echo "  ✗ TUI: not running"
    fi
    echo ""

    # 3. Mini Boss
    echo "── MINI BOSS ──"
    local mb_status="/tmp/uss-tenkara/_prifly/miniboss-status"
    if [ -f "$mb_status" ]; then
        local status
        status=$(cat "$mb_status")
        if [ "$status" = "ACTIVE" ]; then
            echo "  ● MINI BOSS: active"
        else
            echo "  ○ MINI BOSS: $status"
        fi
    else
        echo "  ✗ MINI BOSS: no status file"
    fi
    echo ""

    # 4. Per-agent health
    echo "── AGENTS ──"
    local agent_count=0
    local healthy=0
    local stale=0
    local dead=0
    local orphaned=0

    git -C "$PROJECT_DIR" worktree list --porcelain 2>/dev/null | grep "^worktree " | awk '{print $2}' | while read wt; do
        # Skip main worktree
        if [ "$wt" = "$PROJECT_DIR" ]; then
            continue
        fi
        if [ ! -d "$wt/.sortie" ]; then
            continue
        fi

        local name
        name=$(basename "$wt")
        agent_count=$((agent_count + 1))

        local ended=false
        [ -f "$wt/.sortie/session-ended" ] && ended=true

        # Check for active claude process
        local has_process=false
        if pgrep -f "$name" > /dev/null 2>&1; then
            has_process=true
        fi

        # Check JSONL recency
        local jsonl_age="?"
        python3 -c "
import sys
sys.path.insert(0, '$SKILL_LIB')
from parse_jsonl_metrics import find_latest_session_file
import os, time
jsonl = find_latest_session_file('$wt')
if jsonl:
    age = int(time.time() - os.path.getmtime(str(jsonl)))
    print(age)
else:
    print(-1)
" 2>/dev/null | {
            read age
            jsonl_age="$age"

            # Check sentinel status
            local sentinel_status="?"
            local sentinel_age="?"
            if [ -f "$wt/.sortie/sentinel-status.json" ]; then
                python3 -c "
import json, time
d = json.load(open('$wt/.sortie/sentinel-status.json'))
print(d.get('status', '?'))
print(int(time.time()) - d.get('timestamp', 0))
" 2>/dev/null | {
                    read ss
                    read sa
                    sentinel_status="$ss"
                    sentinel_age="$sa"

                    # Determine health
                    local icon="●"
                    local note=""
                    if [ "$ended" = true ]; then
                        icon="✓"
                        note="session-ended"
                    elif [ "$has_process" = false ] && [ "$ended" = false ]; then
                        icon="⚠"
                        note="no process (orphaned?)"
                    elif [ "$jsonl_age" != "-1" ] && [ "$jsonl_age" -gt 300 ]; then
                        icon="○"
                        note="JSONL stale (${jsonl_age}s)"
                    else
                        note="OK"
                    fi

                    echo "  $icon $name  sentinel:$sentinel_status(${sentinel_age}s)  jsonl:${jsonl_age}s  process:$has_process  $note"
                }
            else
                local icon="○"
                local note="no sentinel status"
                if [ "$ended" = true ]; then
                    icon="✓"
                    note="session-ended"
                fi
                echo "  $icon $name  jsonl:${jsonl_age}s  process:$has_process  $note"
            fi
        }
    done

    echo ""

    # 5. Orphaned processes
    echo "── ORPHANED PROCESSES ──"
    local claude_procs
    claude_procs=$(pgrep -f "claude.*--model" 2>/dev/null | wc -l | tr -d ' ')
    local known_worktrees
    known_worktrees=$(git -C "$PROJECT_DIR" worktree list 2>/dev/null | wc -l | tr -d ' ')
    echo "  Claude processes: $claude_procs"
    echo "  Known worktrees: $known_worktrees"
    if [ "$claude_procs" -gt "$known_worktrees" ]; then
        echo "  ⚠ More processes than worktrees — possible orphans"
        echo "  Run: ps aux | grep 'claude.*--model' to investigate"
    else
        echo "  ● No orphans detected"
    fi

    echo ""
    echo "── DISK ──"
    local worktree_size
    worktree_size=$(du -sh "$PROJECT_DIR/.claude/worktrees" 2>/dev/null | awk '{print $1}')
    echo "  Worktrees: ${worktree_size:-0B}"
    local sortie_size
    sortie_size=$(du -sh "$SORTIE_DIR" 2>/dev/null | awk '{print $1}')
    echo "  .sortie: ${sortie_size:-0B}"
}

# ── Server command management ─────────────────────────────────────────

SORTIE_LIB_DIR="$(cd "$SCRIPT_DIR/../../sortie/lib" && pwd)"

cmd_server_cmd() {
    local subcmd="${1:-show}"
    shift 2>/dev/null || true

    local config_file="$SORTIE_DIR/server-cmd.json"

    case "$subcmd" in
        show|"")
            echo "═══ DEV SERVER COMMAND ═══"
            if [ -f "$config_file" ]; then
                python3 -c "
import json
d = json.load(open('$config_file'))
cmd = d.get('cmd', '(not set)')
src = d.get('detected_from', 'unknown')
pkg = d.get('pkg_mgr', '?')
install = d.get('install_cmd', '(none)')
detected = d.get('detected', False)
origin = 'auto-detected' if detected else 'user-provided'
print(f'  Command:    {cmd}')
print(f'  Install:    {install}')
print(f'  Pkg mgr:    {pkg}')
print(f'  Source:     {src} ({origin})')
print(f'  Config:     $config_file')
" 2>/dev/null
            else
                echo "  Not configured."
                echo "  Run: server-cmd detect   — to auto-detect"
                echo "  Run: server-cmd set <cmd> — to set manually"
            fi
            ;;

        set)
            local cmd_str="$*"
            if [ -z "$cmd_str" ]; then
                echo "Usage: server-cmd set <command>"
                echo "Examples:"
                echo "  server-cmd set 'pnpm run dev'"
                echo "  server-cmd set 'npm start'"
                echo "  server-cmd set 'python manage.py runserver'"
                echo "  server-cmd set 'go run ./cmd/server'"
                echo "  server-cmd set 'docker compose up'"
                return 1
            fi
            mkdir -p "$SORTIE_DIR"
            python3 -c "
import json
config = {
    'cmd': '''$cmd_str''',
    'install_cmd': '',
    'pkg_mgr': 'custom',
    'detected_from': 'user-provided',
    'detected': False
}
with open('$config_file', 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')
print('✓ Server command saved:')
print(f'  {config[\"cmd\"]}')
print(f'  Config: $config_file')
print()
print('V key will now use this command for all worktree dev servers.')
" 2>/dev/null
            ;;

        detect)
            echo "Running auto-detection against $PROJECT_DIR..."
            mkdir -p "$SORTIE_DIR"
            # Remove cached config so detection runs fresh
            rm -f "$config_file"
            python3 -c "
import sys
sys.path.insert(0, '$SORTIE_LIB_DIR')
from detect_server import detect_server_cmd
import json

result = detect_server_cmd('$PROJECT_DIR')
if result:
    print('✓ Auto-detected:')
    print(f'  Command:  {result[\"cmd\"]}')
    print(f'  Install:  {result.get(\"install_cmd\", \"(none)\")}')
    print(f'  Pkg mgr:  {result.get(\"pkg_mgr\", \"?\")}')
    print(f'  Source:   {result.get(\"detected_from\", \"unknown\")}')
    print(f'  Saved to: $config_file')
else:
    print('✗ Could not auto-detect server command.')
    print('  No package.json scripts, manage.py, pyproject.toml, Gemfile,')
    print('  go.mod, docker-compose.yml, or Makefile dev targets found.')
    print()
    print('  Set it manually:')
    print(\"  bash xo-tools.sh server-cmd set 'your-command-here'\")
" 2>/dev/null
            ;;

        *)
            echo "Usage: server-cmd [show|set|detect]"
            echo "  show               Show current dev server command"
            echo "  set <command>      Set dev server command manually"
            echo "  detect             Re-run auto-detection (clears cache)"
            return 1
            ;;
    esac
}

# ── Dispatch ─────────────────────────────────────────────────────────

cmd="${1:-help}"
shift 2>/dev/null || true

case "$cmd" in
    set-status)        cmd_set_status "$@" ;;
    dismiss)           cmd_dismiss "$@" ;;
    board)             cmd_board ;;
    board-json)        cmd_board_json ;;
    sentinel-status)   cmd_sentinel_status ;;
    kick-sync)         cmd_kick_sync ;;
    clear-stale)       cmd_clear_stale ;;
    queue-list)        cmd_queue_list ;;
    queue-remove)      cmd_queue_remove "$@" ;;
    tail-agent|tail)   cmd_tail_agent "$@" ;;
    reassign-model)    cmd_reassign_model "$@" ;;
    inject)            cmd_inject "$@" ;;
    health)            cmd_health ;;
    server-cmd)        cmd_server_cmd "$@" ;;
    help|--help|-h)
        echo "USS Tenkara — XO Tools"
        echo ""
        echo "Status & Control:"
        echo "  set-status <ticket> <status> [reason]   Override agent status"
        echo "  dismiss <ticket> [reason]               Force RECOVERED + session-ended"
        echo "  reassign-model <ticket> <model>         Change model (opus/sonnet/haiku)"
        echo "  inject <ticket> <message>               Queue a directive for the agent"
        echo ""
        echo "Visibility:"
        echo "  board                                   Show flight deck state"
        echo "  board-json                              Board state as JSON (pipeable)"
        echo "  tail-agent <ticket>                     Tail agent's JSONL stream live"
        echo "  sentinel-status                         Sentinel health check"
        echo "  health                                  Full system health report"
        echo ""
        echo "Dev Server:"
        echo "  server-cmd                              Show current dev server command"
        echo "  server-cmd set <command>                Set dev server command manually"
        echo "  server-cmd detect                       Re-run auto-detection (clears cache)"
        echo ""
        echo "Maintenance:"
        echo "  kick-sync                               Force dashboard re-sync"
        echo "  clear-stale                             Mark dead agents as ended"
        echo "  queue-list                              Show mission queue"
        echo "  queue-remove <ticket-id>                Remove from queue"
        echo ""
        echo "Valid statuses: AIRBORNE, IDLE, RECOVERED, ON_APPROACH, MAYDAY, AAR, SAR, PREFLIGHT"
        echo ""
        echo "Examples:"
        echo "  bash xo-tools.sh dismiss PR-608 'mission complete'"
        echo "  bash xo-tools.sh set-status PR-608 RECOVERED 'agent stuck'"
        echo "  bash xo-tools.sh tail-agent PR-608"
        echo "  bash xo-tools.sh reassign-model ENG-200 opus"
        echo "  bash xo-tools.sh inject PR-608 'Focus on the auth flow, skip tests for now'"
        echo "  bash xo-tools.sh health"
        echo "  bash xo-tools.sh board"
        echo "  bash xo-tools.sh clear-stale"
        ;;
    *)
        echo "Unknown command: $cmd"
        echo "Run 'bash xo-tools.sh help' for usage"
        exit 1
        ;;
esac
