# KV Cache Quantization Benchmark (llama.cpp, ROCm)

Measures the **context-accuracy and speed impact** of llama.cpp KV cache
quantization (`q4_0` vs `q8_0`) for a given model, without touching the live
stack: throwaway containers, spare port, `.env`/`compose.yml` never modified.

Tested on two 27B models (64 layers, GQA), 16 GiB VRAM GPU,
`ghcr.io/ggml-org/llama.cpp:server-rocm`:
- **Qwen3.8-27B-UD-Q3_K_XL** — flash-attn on, no MTP.
- **Qwen3.8-27B-GSQ-RCO-IQ3_S-MTP** — flash-attn on, MTP on, mmproj on CPU
  (matches the live stack).

## Method: needle-in-a-haystack (NIAH)

1. Build one synthetic filler corpus ("archive log") at a target token size,
   verified with the server's own `/tokenize` (no char-per-token guessing).
2. Insert N needle facts (4-digit access codes; default N=12) at spread
   depths ~2%–95%; each result records the needle's depth.
3. Greedy decoding (temp 0, top_k 1), thinking disabled — only variable
   between runs is the KV cache type.
4. Query 1 prefills the full context; queries 2–N reuse the cached prefix
   (real-world pattern, and isolates KV read quality from prefill).
5. Ask order is shuffled per seed so retrieval order ≠ insertion order.

Rigor caveats (important):
- Default 12 needles, 1 seed per reported run → **directional, not statistical**.
  Rerun with `SEEDS=42,43,44,45`; `summarize.py` averages seeds and prints a
  per-depth hit map.
- Both quants missed the *same* needle (deepest early-position one), which
  points at position/task difficulty, not quantization.
- No f16 KV ground-truth run (doesn't fit in VRAM at these ctx sizes).

## Results (2026-09-11, Qwen3.8-27B-UD-Q3_K_XL, ROCm)

Identical server-verified corpora per ctx (48k: 48,917 tok; 96k: 98,061 tok).
VRAM includes model weights (~12.8 GB) + KV + compute buffers; total VRAM 17.10 GB.
(This table uses decimal GB = bytes/1e9; the newer table below uses GiB = bytes/1024³.)

| ctx  | KV    | VRAM used        | accuracy | gen t/s | prefill t/s (first / cached) |
|------|-------|------------------|----------|---------|------------------------------|
| 48k  | q8_0  | 14.76 GB         | 5/6      | 12.09   | 339 / 159                    |
| 48k  | q4_0  | 13.99 GB         | 5/6      |  9.05   | 339 / 157                    |
| 96k  | q8_0  | 16.71 GB         | 5/6      |  9.37   | 224 /  96                    |
| 96k  | q4_0  | (q8 run fits, so q4 fits) | 5/6 |  6.31   | 223 /  97                    |

### Findings
1. **Accuracy: no measurable difference** at 48k and 96k (same 5/6, same miss).
2. **Speed (no MTP): q8 is meaningfully faster on this ROCm stack** —
   +34% gen t/s at 48k (12.1 vs 9.0), +48% at 96k (9.4 vs 6.3).
   Prefill is equal (compute-bound, KV size irrelevant at these sizes).
   **With MTP enabled this gap disappears** — see the GSQ-RCO section below.
3. **VRAM slope** (measured): q8 ≈ 41 MB per 1k ctx tokens, q4 ≈ 24 MB per 1k.
   - q8 fits to ~96k ctx on 16 GiB (16.71 GB used; 80k leaves ~1 GB).
   - q4 fits to ~118k (this matches the live 118k @ q4 config, ~15.7 GB).
4. Consistency: both quants miss the same needle → misses are task/position
   effects in this corpus, not KV-precision artifacts (at these lengths).

### Practical takeaway
With no MTP, q8_0 buys ~30% generation speed over q4_0 at equal accuracy, at
the cost of context headroom. With MTP on (the live GSQ-RCO config) that
speed advantage vanishes and accuracy stays identical, so **q4_0 is the
better default**: ~1.7x the context for the same KV VRAM at no measurable
cost.

## Results (2026-09-20, Qwen3.8-27B-GSQ-RCO-IQ3_S-MTP, MTP on, ROCm)

`./run.sh` at a single ctx, so both cache types see the *identical* corpus and
needle depths (12 needles, seed 42). VRAM is the server-up reading before the
first decode (GiB = bytes / 1024³; the card is 15.92 GiB).

| ctx  | KV   | VRAM      | accuracy | gen t/s | prefill t/s (first / cached) |
|------|------|-----------|----------|---------|------------------------------|
| 88k  | q8_0 | 15.48 GiB | 11/12    | 13.16   | 225 / 114                    |
| 88k  | q4_0 | 14.17 GiB | 11/12    | 13.65   | 224 / 113                    |
| 135k | q4_0 | 15.43 GiB | 5/6*     | 11.20   | 168 /  81                    |

\* 135k row uses 6 needles and predates the `depth` field (single run at the
live ctx), so it has no per-depth map.

### Findings
1. **No accuracy difference at equal ctx:** both 11/12, and both miss the
   *same* needle — the one at depth 0.02 (the first corpus line). The per-depth
   hit maps are identical, so the one miss is a position effect, not KV
   precision.
2. **MTP equalizes generation speed:** q4 13.65 vs q8 13.16 t/s at 88k — no
   meaningful gap (contrast the no-MTP numbers above).
3. **q8 ceiling on 16 GiB is ~88k with MTP** (90k OOMs); q4 fits 135k. q8
   therefore costs ~47k of context and buys neither accuracy nor speed.

## Files
- `bench_kv.py`  — the benchmark (stdlib only; runs under `ulimit -v 2097152`)
- `run.sh`       — boots test servers, runs all (ctx x cache x seed) combos
- `summarize.py` — aggregates `results/*.json` (same-ctx A/B pairing, seed
  averaging, per-depth hit map; `--json-out` also writes `summary.json`)
- `results/`     — raw per-run JSON + `summary.json` (this report's data)

## Usage
```bash
cd benchmarks/kv-quant
./run.sh                    # defaults: ctx 88064, q8_0 q4_0, 12 needles, seed 42, MTP on
SEEDS=42,43,44 ./run.sh     # more seeds = more statistical weight
QKV=q4_0 CTXS="88064 135168" ./run.sh   # override ctx set and/or cache types
python3 summarize.py        # print comparison table (run.sh already does this)
```

Requires: docker (rocm image present), `rocm-smi`, python3. The live
llama-server must be stopped first — only one llama.cpp instance fits in VRAM.