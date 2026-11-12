# Kiro CLI, launched by Mesh for each task (`kiro-cli chat --no-interactive`).
mesh adapter probe kiro --user
mesh adapter models kiro --user
mesh agent create --user --name kiro1 --adapter kiro \
  --allowed-model <model-id> --default-model <model-id> \
  --max-concurrency 2
# optional: a Kiro agent profile for the run
#   --option agent='"my-kiro-agent"'
