# Inbox agent: any process that can run shell commands (Claude Code, Cursor, Codex, a bot).
# Mesh never starts it; it asks Mesh for work over the local socket.
mkdir -p ~/.config/agent-mesh && chmod 700 ~/.config/agent-mesh
# Create the agent, write its token to a new 0600 file and print a worker cheat-sheet.
# (Email-driven or slow agents: add --ack-timeout 2h; the default ack window is 10 min.)
mesh agent onboard worker1 --out ~/.config/agent-mesh/worker1.token --user
T=~/.config/agent-mesh/worker1.token
# These are workflow steps, not a loop that executes a task automatically.
# Give them to the inbox agent, replacing the variables with returned values:
mesh inbox wait --timeout 10m --token-file "$T"        # blocks until a message arrives
mesh inbox ack "$MESSAGE_ID" --token-file "$T"
mesh task start "$TASK_ID" --token-file "$T"            # prints run_id, fence, workspace, session, heartbeat_every_s
export MESH_SESSION="$SESSION"   # the `session` from task start: this run's later commands must send it
# Set TASK_ID from the message; RUN_ID, FENCE and SESSION from task start. Work only in its
# returned workspace. Keep the lease alive while you work or run tests:
mesh task keepalive "$TASK_ID" --run "$RUN_ID" --fence "$FENCE" --token-file "$T" > keepalive.log 2>&1 &
#   (or send `mesh task heartbeat` yourself every <=40 s for the default 120-second lease)
mesh task submit "$TASK_ID" --run "$RUN_ID" --fence "$FENCE" --summary "what was done" --token-file "$T"
#   or: mesh task fail "$TASK_ID" --run "$RUN_ID" --fence "$FENCE" --reason "..." --token-file "$T"
#   keepalive exits by itself after submit/fail (exit 0); exit 9 means the lease was lost.
