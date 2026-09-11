#!/usr/bin/env bash
# KV cache quantization accuracy benchmark runner (llama.cpp / ROCm)
#
# Boots throwaway llama-server containers on port 8184 with different
# --cache-type-k/--cache-type-v and runs bench_kv.py against each.
# Does NOT touch the live stack (.env / compose.yml are never modified).
#
# Usage:  ./run.sh [CTX ...]   (default: 49152 98304)
#         MODEL, PORT, IMG can be overridden via env.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IMG="${IMG:-ghcr.io/ggml-org/llama.cpp:server-rocm}"
MODEL="${MODEL:-Qwen3.8-27B-UD-Q3_K_XL.gguf}"
PORT="${PORT:-8184}"
NEEDLES="${NEEDLES:-6}"
SEEDS="${SEEDS:-42}"          # comma-separated list of seeds
CTXS="${CTXS:-49152 98304}"   # space-separated list of ctx sizes
QKV="${QKV:-q8_0 q4_0}"       # space-separated list of kv cache types
EXTRA_FLAGS="${EXTRA_FLAGS:-}" # e.g. "--spec-type draft-mtp --spec-draft-n-max 2"
OUT="$HERE/results"
mkdir -p "$OUT"

run_server () { # $1 = ctx, $2 = cache-type
  local ctx="$1" ct="$2"
  docker rm -f llama-kvtest >/dev/null 2>&1 || true
  docker run -d --name llama-kvtest \
    --device /dev/kfd --device /dev/dri \
    --group-add 992 --group-add 44 \
    --shm-size 8gb \
    -p 127.0.0.1:${PORT}:8080 \
    -v /opt/stacks/llama-cpp/models:/models:ro \
    "$IMG" \
    -m "/models/$MODEL" \
    --host 0.0.0.0 --port 8080 \
    --n-gpu-layers 99 \
    --ctx-size "$ctx" \
    --parallel 1 \
    --cache-type-k "$ct" --cache-type-v "$ct" \
    --flash-attn on \
    --jinja --metrics --temp 0 $EXTRA_FLAGS
  for i in $(seq 1 90); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/health" 2>/dev/null)" = "200" ] && break
    docker ps --filter name=llama-kvtest --format '{{.Status}}' | grep -q Exited && break
    sleep 10
  done
  [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/health" 2>/dev/null)" = "200" ]
  echo "server up: ctx=$ctx cache=$ct vram_used=$(rocm-smi --showmeminfo vram 2>/dev/null | awk '/Used/{print $NF}')"
}

for ctx in $CTXS; do
  for ct in $QKV; do
    tag="${ct}@${ctx}"
    echo "=== running $tag ==="
    if ! run_server "$ctx" "$ct"; then
      echo "server failed to start for $tag (likely VRAM) - skipping" | tee -a "$OUT/summary.log"
      continue
    fi
    for seed in ${SEEDS//,/ }; do
      bash -c "ulimit -v 2097152; python3 '$HERE/bench_kv.py' \
        --server http://127.0.0.1:${PORT} --tag '${tag}_s${seed}' \
        --target-tokens $((ctx * 95 / 100)) --server-ctx '$ctx' \
        --needles '$NEEDLES' --seed '$seed' \
        --out '$OUT/${tag}_s${seed}.json'"
    done
  done
done
docker rm -f llama-kvtest >/dev/null 2>&1 || true
echo "done. results in $OUT"