# Architecture

One orchestrator, two layers, three ID kinds — and one rule that never
bends: **the literal layer is immutable**.

## Compile layer — `run_meeting.py` (9 stages, fully local)

| stage | what happens |
|---|---|
| 1 inventory | event dirs under `input/`; one event = one run |
| 2 audio prepare | ffmpeg convert + optional RNN denoise, idempotent cache |
| 3 ASR | language probe per event: English → whisper full-file; Chinese/mixed → Qwen3-ASR 20s windows **plus** whisper auto as cross-check |
| 4 literal records | per-track hybrid assembly → `R000nnn` (verbatim, timestamps) — **never rewritten afterwards** |
| 5 evidence | each record split to evidence atoms `A000nnn` |
| 5b relevance | lexical candidates + local-LLM review; off-topic talk is *annotated*, never deleted |
| 6 reconcile | LLM groups evidence into topical units `U000nnn`; checkpoints are content-fingerprinted — unchanged input is free on rerun |
| 6b fidelity audit | every claim re-checked against its evidence for mistranslation / garble / intent-insertion / citation gaps; repairs re-audited; unresolved issues downgrade certainty, not silently pass |
| 7 notes | markdown notes become a second evidence source linked to units: supports / partial / conflict / unknown |
| 8-9 package | SQLite (FTS5 trigram) + human-readable reports + quality gate |

### Per-track ASR arbitration (the interesting part)

zh/en code-switching breaks single-engine ASR: whisper auto *hallucinates*
English on pure-Chinese audio; Qwen transliterates English audio. Each
track is routed independently:

```
no whisper transcript            → qwen (or whisper if qwen absent)
whisper agrees audio is Chinese  → qwen (zh quality)
script conflict (whisper says latin, Qwen produced zh):
    obvious hallucination loop   → qwen            (no LLM needed)
    otherwise                    → local-LLM arbitration, zh-safe default
both engines see latin           → whisper
```

The hallucination check is a cheap signature score (repetition loops,
generic filler like "Thank you."/"[Music]", uniqueness collapse) — see
`tests/test_hallucination_score.py`.

### Fidelity audit guard

Repairs may only *remove distortion*, never information: key details
(numbers, latin tokens) present in the original claim and in the evidence
pool must survive a repair; anything lost is rejected back to a residual
defect with certainty downgraded.

## Delivery layer — `core/scripts/mcp_meeting_server.py`

Zero-dependency stdio JSON-RPC. Four read-only tools (see `MCP_SETUP.md`),
three-tier search recall with multi-token merge and CJK shingle fallback,
`*_private` event exclusion, protocol-version echo negotiation. State:
none — it just opens `outputs/<event>/meeting.db`.

## Identifiers

- `R000nnn` record — verbatim sentence(s) + source file + timestamps
- `A000nnn` evidence — one atom of the literal record a unit cites
- `U000nnn` unit — one claim, certainty, topic path, `evidence_ids`

A wrong claim can always be walked back to the exact sentence and second
it came from. That walk-back is the product.
