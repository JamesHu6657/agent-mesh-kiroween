# Inbox agent: any process that can run shell commands (Claude Code, Cursor, Codex, a bot).
# Mesh never starts it; it asks Mesh for work over the local socket.
mesh agent create --user --name worker1 --adapter none --delivery inbox --max-concurrency 1
mkdir -p ~/.config/agent-mesh && chmod 700 ~/.config/agent-mesh
mesh agent token issue worker1 --user   # prints mesh_at_... once
# Save the token outside every workspace and repo, mode 0600:
#   (umask 077; printf '%s\n' 'mesh_at_...' > ~/.config/agent-mesh/worker1.token)
T=~/.config/agent-mesh/worker1.token
# Then give the agent this loop (in its instructions):
mesh inbox wait --timeout 10m --token-file "$T"        # blocks until a message arrives
mesh inbox ack <msg_id> --token-file "$T"
mesh task start <task_id> --token-file "$T"            # prints run_id, fence, workspace
#   work in the printed workspace directory, then:
mesh task heartbeat <task_id> --run <run_id> --fence <fence> --token-file "$T"   # at least every 120 s
mesh task submit <task_id> --run <run_id> --fence <fence> --summary "what was done" --token-file "$T"
#   or: mesh task fail <task_id> --run <run_id> --fence <fence> --reason "..." --token-file "$T"
