# KV Cache Quantization Benchmark (llama.cpp, ROCm)

Measures the **context-accuracy and speed impact** of llama.cpp KV cache
quantization (`q4_0` vs `q8_0`) for a given model, without touching the live
stack: throwaway containers, spare port, `.env`/`compose.yml` never modified.

Tested on: **Qwen3.8-27B-UD-Q3_K_XL** (64 layers, GQA), 16 GiB VRAM GPU,
`ghcr.io/ggml-org/llama.cpp:server-rocm`, flash-attn on, no MTP/mmproj.

## Method: needle-in-a-haystack (NIAH)

1. Build one synthetic filler corpus ("archive log") at a target token size,
   verified with the server's own `/tokenize` (no char-per-token guessing).
2. Insert N=6 needle facts (4-digit access codes) at depths ~2%–95%.
3. Greedy decoding (temp 0, top_k 1), thinking disabled — only variable
   between runs is the KV cache type.
4. Query 1 prefills the full context; queries 2–N reuse the cached prefix
   (real-world pattern, and isolates KV read quality from prefill).
5. Ask order is shuffled per seed so retrieval order ≠ insertion order.

Rigor caveats (important):
- 6 needles, 1 seed per reported run → **directional, not statistical**.
  Rerun with `SEEDS=42,43,44,45` for a stronger signal.
- Both quants missed the *same* needle (deepest early-position one), which
  points at position/task difficulty, not quantization.
- No f16 KV ground-truth run (doesn't fit in VRAM at these ctx sizes).

## Results (2026-09-11, Qwen3.8-27B-UD-Q3_K_XL, ROCm)

Identical server-verified corpora per ctx (48k: 48,917 tok; 96k: 98,061 tok).
VRAM includes model weights (~12.8 GB) + KV + compute buffers; total VRAM 17.10 GB.

| ctx  | KV    | VRAM used        | accuracy | gen t/s | prefill t/s (first / cached) |
|------|-------|------------------|----------|---------|------------------------------|
| 48k  | q8_0  | 14.76 GB         | 5/6      | 12.09   | 339 / 159                    |
| 48k  | q4_0  | 13.99 GB         | 5/6      |  9.05   | 339 / 157                    |
| 96k  | q8_0  | 16.71 GB         | 5/6      |  9.37   | 224 /  96                    |
| 96k  | q4_0  | (q8 run fits, so q4 fits) | 5/6 |  6.31   | 223 /  97                    |

### Findings
1. **Accuracy: no measurable difference** at 48k and 96k (same 5/6, same miss).
2. **Speed: q8 is meaningfully faster on this ROCm stack** —
   +34% gen t/s at 48k (12.1 vs 9.0), +48% at 96k (9.4 vs 6.3).
   Prefill is equal (compute-bound, KV size irrelevant at these sizes).
3. **VRAM slope** (measured): q8 ≈ 41 MB per 1k ctx tokens, q4 ≈ 24 MB per 1k.
   - q8 fits to ~96k ctx on 16 GiB (16.71 GB used; 80k leaves ~1 GB).
   - q4 fits to ~118k (this matches the live 118k @ q4 config, ~15.7 GB).
4. Consistency: both quants miss the same needle → misses are task/position
   effects in this corpus, not KV-precision artifacts (at these lengths).

### Practical takeaway
On this ROCm setup, q4_0 KV shows no accuracy advantage being used here —
it costs ~30% generation speed vs q8 for extra context headroom. If you can
afford the VRAM, q8_0 is the better default; drop to q4_0 only when you need
the extra ctx capacity (~1.7x more ctx for the same KV VRAM).

## Files
- `bench_kv.py`  — the benchmark (stdlib only; runs under `ulimit -v 2097152`)
- `run.sh`       — boots test servers, runs all (ctx x cache x seed) combos
- `summarize.py` — aggregates `results/*.json` into a comparison table
- `results/`     — raw per-run JSON + `summary.json` (this report's data)

## Usage
```bash
cd benchmarks/kv-quant
./run.sh                    # defaults: ctx 49152 98304, q8_0 q4_0, seed 42
SEEDS=42,43,44 ./run.sh     # more seeds = more statistical weight
CTXS=98304 EXTRA_FLAGS="--spec-type draft-mtp --spec-draft-n-max 2" ./run.sh
python3 summarize.py        # print comparison table
```

Requires: docker (rocm image present), `rocm-smi`, python3. The live
llama-server must be stopped first — only one llama.cpp instance fits in VRAM.