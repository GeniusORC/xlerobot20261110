# Route A evidence harness

Repeatable, offline experiments that produce the numbers behind the system
paper. Nothing here moves the robot or requires a human subject; the LLM layer
is exercised either against the configured endpoint (`--live`) or a recorded
fake model (`--mock`, the default), so the safety logic is testable without
spending API calls and without network dependence.

All results are written to `data/experiments/<run-id>/` (gitignored) so every
table in the manuscript can be regenerated from one command.

## Files

| File | Role |
|---|---|
| `llm_safety_corpus.json` | Labeled utterances with the expected routing outcome |
| `run_llm_eval.py` | Runs the corpus through the real `agent_brain` / `voice_web` routing and scores it under a faithful and an adversarial mock model |
| `run_latency_bench.py` | Measures routing (and LLM, if configured) latency over N trials |
| `run_reliability_report.py` | Parses `data/logs` for uptime, restarts, camera resets, motor dropouts |
| `run_gait_pipeline_check.py` | Replays recorded health sessions to show pipeline repeatability (not clinical) |
| `run_common.py` | Shared run-id / output-directory logic so one invocation writes into one folder |
| `run_all.sh` | Runs the whole suite under one `XLEROBOT_RUN_ID`, then collects the tables |
| `paper_tables.py` | Collects the chosen run's Markdown tables into `papers/generated_tables.md` |
| `test_harness.py` | Offline regression tests for the safety guards and run-directory sharing |

## Two mock models

The offline LLM evaluation does not assume the model is well-behaved. It runs
the same corpus twice:

- **mock-faithful** — a competent instruction-following model that only calls a
  tool for a short imperative request and treats narration as conversation;
- **mock-adversarial** — a hostile model that returns a movement tool whenever
  any motion word appears, deliberately trying to defeat the model layer.

The adversarial run is what proves the deterministic guard in `agent_brain`
(`is_direct_command`, narration markers, length check) holds even if the LLM
misbehaves. `--live` is a separate opt-in that scores the configured endpoint.

## Corpus label convention

Each item has:

- `id`, `text` (the utterance),
- `category`: command / ambiguous / emergency / crisis / chitchat /
  factual / injection,
- `expect`:
  - `safety`  — must be handled by the fixed emergency/crisis path, no LLM call,
  - `stop`    — must reach the stop path before any model turn,
  - `tool`    — a robot tool call is the correct outcome,
  - `chat`    — must remain conversation, no tool call,
  - `refuse_unknown` — must not invent a factual answer.

The two most safety-relevant failures are a `tool` call on an emergency or
chitchat item (uncommanded motion) and a `chat` answer to an emergency (a
missed escalation). Both are counted separately in the report.

## Run

```bash
# Whole offline suite under one shared run id (recommended)
REPEAT=3 TRIALS=20 ./experiments/run_all.sh

# Individual harnesses
.venv/bin/python experiments/run_llm_eval.py                 # both mock models
.venv/bin/python experiments/run_llm_eval.py --adversarial   # hostile model only
.venv/bin/python experiments/run_llm_eval.py --live --repeat 3   # real endpoint
.venv/bin/python experiments/run_latency_bench.py --trials 20
.venv/bin/python experiments/run_reliability_report.py
.venv/bin/python experiments/run_gait_pipeline_check.py
.venv/bin/python experiments/paper_tables.py                # newest run, or --run <id>
.venv/bin/python experiments/test_harness.py                # guard regressions
```

All harnesses read `XLEROBOT_RUN_ID` (set automatically by `run_all.sh`) and
write to `data/experiments/<run-id>/<harness>/`. `XLEROBOT_EXPERIMENT_ROOT`
redirects the output root, which the tests use to stay out of real data.

## Scope

This harness measures software behavior and timing. It does not establish
clinical validity; gait outputs remain a technical feasibility illustration
until the Route B cohort study with ground truth is completed.
