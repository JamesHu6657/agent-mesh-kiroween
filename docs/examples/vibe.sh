# Mistral Vibe (`vibe`), launched by Mesh for each task. Vibe has no effort flag.
mesh adapter probe vibe --user
mesh agent create --user --name vibe1 --adapter vibe \
  --allowed-model <model-id> --default-model <model-id> \
  --max-concurrency 1
# optional spend/turn limits passed to vibe:
#   --option max_turns=40 --option max_price_usd=2 --option max_tokens=200000
#   --option trust_workdir=true
