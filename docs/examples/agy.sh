# Antigravity CLI (`agy`), launched by Mesh for each task.
mesh adapter probe agy --user
mesh adapter models agy --user
# Give the effort either in the model id suffix (-low/-medium/-high/...) or with
# --allowed-effort/--default-effort, not both.
mesh agent create --user --name agy1 --adapter agy \
  --allowed-model <model-id> --default-model <model-id> \
  --max-concurrency 1
# optional: --option sandbox=true   --option agent='"name"'
