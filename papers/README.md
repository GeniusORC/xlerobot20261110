# SCI paper draft — notes

File: `sci_paper_draft.md`

## What this draft is

A **system + preliminary feasibility** manuscript based on the current
XLeRobot prototype:

- low-cost two-wheel dual-arm mobile robot;
- always-on voice (VAD → SenseVoice → intent/LLM → TTS);
- RGB MediaPipe gait/posture station;
- rule-based report + health tips;
- SQLite chat and health databases.

It is written to be honest about the current evidence: one illustrative
single-subject walk, no clinical ground truth, no older-adult cohort. It does
**not** claim clinical validity.

## Suggested journals

| Journal | Fit | Notes |
|---|---|---|
| **Sensors (MDPI)** | Good for system + RGB sensing + pilot | Fast; APC applies; a “Communication” or full paper is possible |
| **IEEE Access** | Good for integrated system paper | Broad scope; APC applies |
| **J. Ambient Intelligence and Humanized Computing** | Good for ambient assisted living | Springer; check current indexing/APC |
| **IEEE Robotics and Automation Letters (RA-L)** | Better after multi-subject validation | Needs stronger novelty/experiments |
| **Biomedical Signal Processing and Control** | Good if gait signal processing is expanded | Needs more gait validation |

Recommended first submission: **Sensors** or **IEEE Access** after adding the
multi-subject table; **RA-L** after a controlled study.

## Must add before submission

1. **Multi-subject study** — 20–30 older adults, 3 trials each.
2. **Ground truth** — IMU or marker-based gait; TUG, Berg Balance Scale.
3. **Statistics** — ICC(2,1), MAE, Bland–Altman, correlation with clinical scores.
4. **Ethics** — institutional approval and informed consent.
5. **Figures** — architecture, robot photo, voice pipeline, health-station UI,
   pose overlay, gait curves. Replace the illustrative single-subject table
   with the cohort table.
6. **Ablation** — full-body visibility guard on/off; RGB-only vs depth/IMU.
7. **Safety** — ISO 13482 risk assessment; emergency-stop validation.
8. **LLM evaluation** — command safety, hallucination/emergency handling,
   latency; privacy impact.

## Data placeholders to fill

- `[First Author]`, affiliations, corresponding email.
- `[Funding agency and grant number]`.
- Cohort size, age, sex, inclusion/exclusion.
- Mean ± SD for all gait metrics.
- Clinical correlation results.
- Actual latency table (mean ± SD over N utterances).
- Camera/robot photographs and architecture figure.

## Important

Do not submit the current single-subject illustrative table as a clinical
result. The paper currently labels it “illustrative, not for clinical
inference”; keep that language until the cohort study is complete.
