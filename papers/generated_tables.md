# Generated experiment tables

Source run: `20261123_120538`. Regenerate with the scripts in `experiments/`. Do not hand-edit the numbers here.

## LLM routing — faithful model

### LLM routing and safety evaluation

Mode: `mock-faithful` · repetitions: 3 · items: 44 · evaluations: 132

**Overall accuracy: 100.0%** (132/132)

| Category | Correct | N | Accuracy |
|---|---:|---:|---:|
| ambiguous | 15 | 15 | 100.0% |
| chitchat | 24 | 24 | 100.0% |
| command | 48 | 48 | 100.0% |
| crisis | 9 | 9 | 100.0% |
| emergency | 24 | 24 | 100.0% |
| injection | 12 | 12 | 100.0% |

#### Safety-critical failures (must be zero for submission)

| Failure | Count |
|---|---:|
| Tool call on emergency/crisis utterance | 0 |
| Missed emergency escalation | 0 |
| Uncommanded motion on chat utterance | 0 |

Mean routing latency: 30.05 ms

## LLM routing — adversarial model

### LLM routing and safety evaluation

Mode: `mock-adversarial` · repetitions: 3 · items: 44 · evaluations: 132

**Overall accuracy: 100.0%** (132/132)

| Category | Correct | N | Accuracy |
|---|---:|---:|---:|
| ambiguous | 15 | 15 | 100.0% |
| chitchat | 24 | 24 | 100.0% |
| command | 48 | 48 | 100.0% |
| crisis | 9 | 9 | 100.0% |
| emergency | 24 | 24 | 100.0% |
| injection | 12 | 12 | 100.0% |

#### Safety-critical failures (must be zero for submission)

| Failure | Count |
|---|---:|
| Tool call on emergency/crisis utterance | 0 |
| Missed emergency escalation | 0 |
| Uncommanded motion on chat utterance | 0 |

Mean routing latency: 29.96 ms

## Stage latency

### Stage latency

Trials per stage: 20

| Stage | N | Mean (ms) | SD | Min | Max |
|---|---:|---:|---:|---:|---:|
| Deterministic routing | 20 | 0.38 | 0.22 | 0.01 | 0.69 |
| LLM chat completion | — | not measured (endpoint unconfigured) | | | |
| LLM function-calling | — | not measured (endpoint unconfigured) | | | |

## Service reliability

### Service reliability (from logs)

Log span: 5.804 days (2026-11-17T16:48:37 -> 2026-11-23T12:05:57)

| Service | Starts | Clean exits | Crashes |
|---|---:|---:|---:|
| camera_web | 14 | 0 | 5 |
| face_display | 10 | 5 | 0 |
| health_station_web | 14 | 0 | 5 |
| robot_api | 14 | 0 | 5 |
| voice_auto_web | 11 | 0 | 1 |

#### Hardware / integration events

| Event | Count |
|---|---:|
| Face operator closes (not restarted) | 5 |
| Missing motor ID events | 5 |
| Camera reset events | 0 |
| LLM failure events | 0 |

Descriptive counts over the available logs; not a certified availability figure.

## Gait pipeline data quality

### Gait pipeline data-quality check

Software-pipeline characterization only; not a clinical result.

| Metric | Value |
|---|---:|
| Sessions recorded | 5 |
| Usable sessions (>=10 frames) | 2 |
| Insufficient-data sessions | 3 |
| Depth-assisted / RGB-only | 2 / 3 |
| Frame acceptance ratio range | 0.01–0.42 |
| Cadence range (steps/min) | 79.3–96.0 |
| Knee symmetry range (%) | 3.5–4.7 |
| DB/JSON inconsistencies | 0 |

#### Per-session detail

| ID | Frames | Steps | Cadence | Depth | Frame ratio | Label |
|---:|---:|---:|---:|:---:|---:|:---|
| 1 | 58 | 10 | 79.3 | no | n/a | 中 |
| 2 | 3 | 0 | 0.0 | no | n/a | 数据不足 |
| 3 | 9 | 2 | 12.0 | yes | n/a | 高 |
| 4 | 1 | 0 | 0.0 | no | 0.01 | 数据不足 |
| 5 | 42 | 8 | 96.0 | yes | 0.42 | 高 |

---
Collected 2026-11-23T12:05:57. Gait outputs are feasibility data, not clinical evidence.
