#!/bin/sh
# Agent Deck simulate-mode test against real tmux panes (no FM-1 needed).
#
# Fixtures (tmux lists sessions alphabetically, so the a- prefix pins
# a-claude-approval to slot 0; the checks for the other two fixtures match
# their discovery lines by name, so unrelated tmux sessions on the machine
# may shift their slot numbers without failing the test):
#   slot 0  session "a-claude-approval": a script that prints a fake Claude
#           Code permission prompt, reads one line, and reports what it got.
#   session "b-spinner":   prints a changing line once a second
#           (heuristic state: WORKING).
#   session "c-idle":      a plain interactive shell (IDLE).
#
# The daemon is fed: select 0, approve, then quit. The approve action must
# send "1" + Enter to slot 0 (Claude Code approve sequence), whose fixture
# then prints RECEIVED:1 and a completion line, which must show up in the
# daemon's DETAIL frames for the selected agent.
#
# Usage: sh host/test_simulate.sh   (log lands in host/simulate-test.log)
set -u
cd "$(dirname "$0")/.."
LOG=host/simulate-test.log
TMP=$(mktemp -d)
SESS="a-claude-approval b-spinner c-idle"

kill_sessions() {
    for s in $SESS; do tmux kill-session -t "$s" 2>/dev/null; done
}
cleanup() {
    kill_sessions
    rm -rf "$TMP"
}
trap cleanup EXIT
kill_sessions

cat > "$TMP/approve.sh" <<'EOF'
#!/bin/bash
echo "Claude Code (fake fixture for Agent Deck test)"
echo "I want to edit host/agentdeck.py to add a feature."
echo "Do you want to proceed?"
echo " > 1. Yes"
echo "   2. No, and tell Claude what to do differently"
printf "choice: "
read -r ans
echo "RECEIVED:$ans"
if [ "$ans" = "1" ]; then echo "EDIT APPLIED - task complete"; fi
sleep 60
EOF
# The daemon's BLOCKED heuristic looks for the real prompt's selection glyph;
# the fixture prints it if the terminal handles UTF-8 (tmux panes do).
sed -i 's/" > 1\. Yes"/" ❯ 1. Yes"/' "$TMP/approve.sh"
chmod +x "$TMP/approve.sh"

cat > "$TMP/spin.sh" <<'EOF'
#!/bin/bash
i=0
while true; do i=$((i+1)); printf '\rworking on iteration %d   ' "$i"; sleep 1; done
EOF
chmod +x "$TMP/spin.sh"

tmux new-session -d -s a-claude-approval "bash $TMP/approve.sh"
tmux new-session -d -s b-spinner "bash $TMP/spin.sh"
tmux new-session -d -s c-idle
sleep 1

{
    sleep 2; echo "select 0"
    sleep 2; echo "approve"
    sleep 3; echo "quit"
} | python3 host/agentdeck.py --backend tmux --simulate > "$LOG" 2>&1

cat "$LOG"
echo "--- checks ---" | tee -a "$LOG"
fail=0
for pat in \
    "slot 0: a-claude-approval" \
    "b-spinner (id=" \
    "c-idle (id=" \
    "state=BLOCKED" \
    "state=WORKING" \
    "state=IDLE" \
    "approve -> slot 0" \
    "RECEIVED:1" \
    "EDIT APPLIED - task complete"
do
    if grep -qF "$pat" "$LOG"; then
        echo "PASS: $pat" | tee -a "$LOG"
    else
        echo "FAIL: $pat" | tee -a "$LOG"; fail=1
    fi
done
[ "$fail" -eq 0 ] && echo "SIMULATE TEST: ALL PASS" | tee -a "$LOG" \
                  || echo "SIMULATE TEST: FAILURES" | tee -a "$LOG"
exit "$fail"
