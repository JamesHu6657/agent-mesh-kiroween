# Factory droid, launched by Mesh for each task (`droid exec`).
mesh adapter probe droid --user           # records the installed version
mesh adapter models droid --user          # model ids this droid offers
mesh agent create --user --name droid1 --adapter droid \
  --allowed-model "$MODEL_ID" --default-model "$MODEL_ID" \
  --allowed-effort high --default-effort high \
  --max-concurrency 2
# optional: --binary /path/to/droid if it is not on PATH
