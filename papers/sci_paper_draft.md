# A Low-Cost Dual-Arm Mobile Robot with Always-On Voice Interaction and RGB-Based Gait Screening for Home Elderly Care: System Design and Preliminary Feasibility

**[First Author]¹, [Second Author]¹, [Third Author]², [Corresponding Author]¹,\***

¹ [Department / School], [University], [City], [Country]  
² [Department / School], [University], [City], [Country]  
\* Correspondence: [email]

**Manuscript type:** System paper / preliminary feasibility study  
**Target journals (suggested):** *Sensors* (MDPI), *IEEE Access*, *Journal of Ambient Intelligence and Humanized Computing*; *IEEE Robotics and Automation Letters* after a multi-subject study.

---

## Abstract

**Background:** Home care for older adults requires affordable, easy-to-use robots that can combine mobility, manipulation, natural interaction, and longitudinal health observation. Most low-cost research platforms address only one of these functions.

**Objective:** This paper presents a low-cost dual-arm mobile robot that integrates (i) a two-wheel differential base with two SO-101 arms, (ii) always-on voice interaction using voice activity detection, automatic speech recognition, a large language model (LLM), and text-to-speech, (iii) an RGB-only fixed health station for 2D gait and posture screening using MediaPipe, and (iv) local SQLite storage of chat messages, gait sessions, reports, and health tips.

**Methods:** The prototype is built on an XLeRobot 0.4.0 platform with three 640×480 RGB cameras and a laptop-class edge computer. The voice pipeline uses Silero VAD, SenseVoice, deterministic command routing, DeepSeek-compatible LLM chat, and echo suppression. The health station uses the MediaPipe PoseLandmarker (Tasks API) to extract 33 landmarks; knee/hip/trunk angles, cadence, symmetry, normalized step length, and sway are computed from a 10 s walk. A rule-based report and health tips are generated and persisted in SQLite.

**Results:** The complete pipeline was exercised on the physical robot. Three RGB streams ran at approximately 26.5 fps each in a simultaneous test. The eight command categories (stop, come, forward, backward, turn left, turn right, look-at-me, report) were exercised successfully. Automatic speech recognition (ASR) latency was approximately 0.2–0.4 s and text-to-speech (TTS) latency approximately 1.4–1.9 s for short utterances on the edge laptop, while the deterministic safety-critical routing path completed in about 0.5 ms and did not rely on the network. An offline labeled-corpus evaluation (44 utterances, three repetitions, 264 evaluations) under both a faithful and an adversarial mock model achieved 100% routing accuracy with zero safety-critical failures—no emergency utterance was missed and no conversation or narration produced an uncommanded motion, even when the model was replaced by a hostile policy. A single 7.6 s illustrative walk produced 10 detected steps and a cadence of 79.3 steps/min; this pilot and a five-session pipeline data-quality check are reported only as technical illustrations, not as clinical results. Chat and health records survived service restarts.

**Conclusions:** The results support the technical feasibility of an integrated, low-cost robot for home elderly care. A controlled multi-subject study with clinical ground truth is required before any health-related efficacy claim.

**Keywords:** elderly care robot; mobile manipulation; voice interaction; large language model; gait analysis; MediaPipe; RGB; SQLite; assistive robotics

---

## 1. Introduction

Population ageing is increasing the demand for long-term care while the available caregiver workforce remains limited. Smart home-care robots are a promising response, but practical deployment requires four capabilities to work together: safe mobility in cluttered homes, light manipulation, natural interaction for older users, and longitudinal health observation. Commercial systems often address one capability at a high cost, while low-cost research platforms typically focus on manipulation or navigation alone.

This work asks a system-level question: **can an affordable, open, dual-arm mobile robot provide a coherent home-care service loop—talk, approach, stop, observe gait, report, and retain records—without expensive depth or force sensors?** We build on the XLeRobot 0.4.0 platform, a two-wheel differential base with two SO-101 arms and three RGB cameras, and add an always-on voice agent and an RGB-only health station.

The contributions are:

1. An integrated low-cost system that combines a differential-drive dual-arm mobile manipulator, always-on voice interaction, and an RGB gait station on one edge laptop.
2. A dual-path language interface: deterministic, safety-first command routing for robot actions, and LLM-based companionship for open-domain conversation, with TTS echo suppression and a deterministic guard that re-validates every model-proposed motion tool against imperative structure and narration markers.
3. A 2D gait/posture pipeline with full-body visibility checks, rule-based reports, health tips, and SQLite persistence for both chat and health sessions.
4. A repeatable offline evaluation harness with a labeled Chinese safety corpus, faithful and adversarial mock models, stage-latency and log-derived reliability measurements, and a preliminary physical feasibility study, all with an explicit discussion of what the current evidence does and does not support.

The paper is deliberately framed as a **system and feasibility study**. The pilot involves one healthy adult and no clinical ground truth; it is not a clinical validation of gait or fall risk.

---

## 2. Related Work

### 2.1 Low-cost mobile manipulators

The LeRobot ecosystem has lowered the barrier to real-world robot learning by providing open hardware, datasets, and policies [4]. The SO-101 arm and the Lekiwi base are representative low-cost building blocks. XLeRobot combines a Lekiwi-style base, two SO-101 arms, an IKEA RÅSKOG cart, and RGB cameras into a dual-arm mobile household robot with a cost starting around US$660 [5]. These platforms are designed for manipulation research and teleoperation; integrated elderly-care services, always-on voice, and longitudinal health records are not their primary focus.

### 2.2 Companion robots and language-based interaction

Socially assistive robots such as PARO and ElliQ have shown that companion-style interaction can be acceptable to older adults [2,3]. However, closed commercial systems limit customization, and command-only voice interfaces do not support natural conversation. Recent LLM-based embodied agents [17–19] demonstrate language grounding and vision-language-action capabilities, but they typically require substantial compute and curated data. A practical home robot needs a safety-first command layer that works even when the LLM is unavailable, with the LLM used for companionship rather than direct motor authority.

### 2.3 RGB gait and posture analysis

Marker-based motion capture remains the reference standard, but RGB-based pose estimation has become practical [6,7,15,16]. MediaPipe provides on-device 2D landmarks and has been used for exercise and rehabilitation monitoring [6,7]. Monocular 2D gait analysis cannot recover absolute step length or 3D joint kinetics without calibration; it is therefore better suited to trend monitoring, symmetry, cadence, and posture screening than to diagnosis. Prior reviews highlight the value of low-cost gait analysis but also its sensitivity to camera placement, occlusion, and footwear [11,12].

### 2.4 Gap

Existing work rarely integrates mobility, manipulation, conversation, gait observation, and persistent records in one affordable system. This paper targets that integration gap and reports the engineering choices and preliminary behavior of such a system.

---

## 3. System Overview

### 3.1 Hardware

The prototype is an XLeRobot 0.4.0 two-wheel differential-drive platform with two SO-101 arms and a compact head (Table 1). The drive wheels are 5-inch wheels. The robot carries a laptop-class computer; no Raspberry Pi is required for the current pilot. Three USB RGB cameras provide a head view and two wrist views. A RealSense D455 was evaluated during development but is **not used** in the reported RGB-only configuration.

**Table 1. Prototype hardware.**

| Component | Specification |
|---|---|
| Base | Two-wheel differential drive, 5-inch wheels |
| Arms | 2 × SO-101, 6 DoF including gripper |
| Head | 2-DoF head, 360° head design |
| Cameras | Head RGB + left wrist RGB + right wrist RGB, 640×480 |
| Compute | Intel i7-9750H, 16 GB RAM, GTX 1660 Ti 6 GB, Ubuntu 24.04 |
| Actuation | Feetech STS3215 serial bus servos, 1 Mbaud |
| Storage | Local SQLite databases for chat and health sessions |
| Cost | Low-cost open hardware (XLeRobot-class, ~US$660 excluding laptop) |

### 3.2 Software architecture

The system has four layers (Figure 1, to be inserted):

1. **Perception and interaction:** RGB cameras, VAD, ASR, MediaPipe pose, face detection for “look-at-me.”
2. **Decision and safety:** deterministic intent routing, command arbitration, speed/stop limits, LLM chat, rule-based report generation.
3. **Actuation:** differential-drive velocity commands and arm/head position commands through LeRobot.
4. **Application and records:** web UI, TTS, SQLite chat/health databases, file artifacts (pose screenshot, gait curve, metrics JSON).

The current pilot uses direct LeRobot control for the arms/base and lightweight Python services for the voice and health modules. ROS 2 is installed for future Nav2/RTAB-Map integration but is not required for the reported experiments.

### 3.3 Operating modes

- **Idle:** base stopped; cameras available; voice listener active.
- **Voice command:** a recognized command is executed by a single control worker; stop preempts all motion.
- **Companion chat:** open-domain text is routed to an LLM; replies are spoken and stored.
- **Health station:** the robot stops, the head RGB camera observes a full-body walk, and a report is generated and saved.
- **Teleoperation:** keyboard control for data collection and recovery.

---

## 4. Methods

### 4.1 Differential-drive calibration

The wheel radius was set to 0.0635 m for the 5-inch wheels. The wheelbase was estimated from the IKEA RÅSKOG cart and the 0.4.0 base CAD (approximately 0.367 m) and then refined by an on-ground turn test. A commanded 90° turn produced an actual rotation of 80°, giving:

```text
L = wheel_arc_difference / actual_body_angle
  = 0.5779 m / radians(80°)
  = 0.414 m
```

The reported wheelbase is therefore 0.414 m. This value is used for all subsequent turning commands. The build also required a software direction inversion: positive linear velocity initially moved the cart backwards, while rotation was correct. The final patch inverts only the linear component in both command and feedback, preserving rotation direction.

### 4.2 Always-on voice pipeline

Audio is captured at 16 kHz in 32 ms frames. Silero VAD [8] detects speech onset and endpoint (approximately 0.6 s of silence). SenseVoice/FunASR [9] transcribes the utterance. The recognized text is first passed to a deterministic intent mapper:

- **Motion commands:** stop, come/forward/backward, turn left/right, look-at-me.
- **Status:** report.
- **Safety keywords:** chest pain, dyspnea, fall, unconsciousness, and related terms trigger a fixed safety response before any LLM call.
- **Other text:** routed to an LLM for companionship.

The LLM is accessed through an OpenAI-compatible endpoint (DeepSeek `deepseek-chat` in the pilot) with a short system prompt that limits response length, prohibits diagnosis, and requires emergency escalation. If the LLM is unavailable, a local rule-based companion fallback is used. Replies are synthesized with an online TTS voice and played through the laptop speaker.

To avoid self-triggering, the microphone is muted during TTS playback and for a 2 s tail; in addition, ASR output is compared with recent spoken replies using normalized longest-common-substring and sequence similarity. Utterances resembling the robot's own speech are discarded.

**Table 2. Voice commands and behavior.**

| Intent | Example phrases | Behavior |
|---|---|---|
| stop | 停下, 停止, 别动 | stop base immediately |
| come | 过来, 靠近 | short forward approach (0.2 m) |
| forward | 前进, 往前 | forward 0.2 m |
| backward | 后退, 往后 | backward 0.2 m |
| turn left | 左转, 向左转 | rotate about 90° |
| turn right | 右转, 向右转 | rotate about 90° |
| look-at-me | 看我, 看着我, 面向我 | face detection + base rotation to center the face |
| report | 报告, 状态 | return robot status |
| chat | any other text | LLM or local companion reply |

### 4.3 RGB gait and posture analysis

The health station uses the head RGB camera at 640×480 and the MediaPipe PoseLandmarker Lite (Tasks API) [6,7]. For each frame, 33 landmarks are extracted. A frame is accepted only if shoulders, hips, knees, and ankles are visible and inside the image bounds; this rejects close-up torso images that can produce hallucinated leg landmarks.

For each accepted frame, with pixel coordinates \(p_i=(x_i,y_i)\):

- Knee angle: \(\theta_k = \arccos((p_h-p_k)\cdot(p_a-p_k)/(|p_h-p_k||p_a-p_k|))\).
- Hip angle: analogously with shoulder, hip, and knee.
- Trunk angle: \(\theta_t = \arctan2(x_s-x_h, y_h-y_s)\), using shoulder and hip midpoints.
- Ankle separation: \(|x_{aL}-x_{aR}|/w_s\), where \(w_s\) is shoulder width.
- Sway: standard deviation of hip midpoint x divided by shoulder width.
- Symmetry index: \(SI = \frac{1}{N}\sum |\theta_{kL}-\theta_{kR}| / \frac{1}{N}\sum(\theta_{kL}+\theta_{kR})/2 \times 100\%\).
- Cadence: \(60 \times N_{steps}/T\), where steps are detected as peaks in foot lift (negative ankle y) with a minimum inter-step interval of 0.35 s.

A session of 10 s is recorded at 10 Hz. The system saves an annotated screenshot, a three-panel curve (knee, hip, ankle), and a metrics JSON file. A rule-based report maps findings to risk levels (`low`, `medium`, `high`, or `insufficient data`) and generates 3–5 health tips. The report explicitly states that it is a screening aid, not a diagnosis.

### 4.4 Data persistence

Two SQLite databases are used locally:

- `data/chat/chat.db`: message id, role, text, engine (`llm`, `fallback`, `safety`, `local`), intent, timestamp.
- `data/health/health.db`: session id, timestamp, duration, frames, steps, cadence, symmetry, risk, metrics JSON, report, tips JSON, pose/gait/json paths.

The web UI reads the latest session and the history table from the database; messages and health sessions survive service restarts. No cloud database is used in the pilot.

### 4.5 Safety and privacy

- The base is limited to 0.05 m/s for approach/forward/backward and 20°/s for turns in the health/voice demo.
- A stop event preempts every motion loop; the base is stopped on idle and on disconnect.
- Voice command routing is layered: short imperative phrases are handled by a deterministic intent mapper and safety-keyword layer *before* any model call, and the LLM has no direct motor authority.
- A motion tool proposed by the model is re-validated by a deterministic guard (`is_direct_command`): it is accepted only when the utterance is short (at most ten characters after stripping punctuation), contains a motion word, and contains no narration marker. Sentences that merely mention motion in the first person or as a subordinate clause (e.g., "yesterday I stepped back", "turn left and watch for cars", "can you stop and listen to me") are treated as conversation and re-asked without tools. The same guard covers the stop tool, so a metaphorical "stop" cannot halt the base.
- Emergency phrases bypass the LLM and produce fixed safety guidance.
- Only metrics, text, and images are stored locally; no continuous audio is uploaded by default. When a cloud LLM is enabled, only the text of the conversation is sent, not video.
- The prototype is not certified under ISO 13482 [20]; a formal risk assessment is required before unsupervised use with older adults.

### 4.6 Offline routing and safety evaluation

Because motion authority is the highest-risk output, command safety was evaluated offline with a labeled corpus rather than only by ad-hoc spoken tests. The corpus contains 44 Chinese utterances in seven categories—valid commands, ambiguous narration containing motion words, emergency and crisis phrases, chitchat, unanswerable factual questions, and prompt-injection attempts—each labeled with the expected routing outcome (safety bypass, stop, robot tool, conversation, or refusal of an unknown fact).

The same corpus is run against two deterministic stand-in models so that the fixed safety layer can be scored independently of model quality: a **faithful** model that only calls a tool for short imperative requests, and an **adversarial** model that returns a movement tool whenever any motion word appears. The adversarial condition deliberately tries to defeat the model layer and therefore tests whether the deterministic guard alone prevents uncommanded motion. A `--live` mode additionally scores the configured endpoint. The physical robot is never invoked: all robot-API calls are replaced with recorded no-op stubs, and every run is repeatable from a single script that writes machine-readable CSV/JSON and manuscript tables under a shared run identifier.

---

## 5. Experimental Setup

### 5.1 Platform and environment

The robot was operated on a flat indoor floor. The head camera was used for the health station; the wrist cameras were used for manipulation views. The voice pipeline used the laptop's built-in microphone and speaker. The edge computer ran Ubuntu 24.04 with Python 3.11/3.12 environments for voice and robot control.

### 5.2 Technical tests

- **Camera throughput:** three 640×480 RGB streams opened simultaneously; 20 rounds of three frames took ~0.75 s.
- **Voice:** command phrases were spoken and synthetic speech was played through the speaker to test ASR, intent routing, TTS, and echo suppression.
- **Robot commands:** stop, approach, forward, backward, left/right turn, look-at-me, and report were exercised.
- **Offline routing and safety:** the 44-utterance labeled corpus was run through the real routing code under both mock models for three repetitions (264 evaluations), with robot calls stubbed; routing accuracy and three safety-critical confusion counts were logged.
- **Stage latency:** the deterministic router was timed over 20 trials per run; LLM completion latency is measured by the same benchmark when an endpoint is configured, and is otherwise explicitly reported as not measured rather than estimated.
- **Service reliability:** supervisor logs spanning the available operating period were parsed for service starts, clean exits, crashes, operator window closes, camera resets, motor-bus dropouts, and LLM failures.
- **Persistence:** chat and health records were written to SQLite and read back after service restart.

### 5.3 Illustrative gait session

One healthy adult volunteer performed a short walk in front of the robot. The session lasted 7.6 s and produced 10 detected steps. This is a **single illustrative run**; no older adults, clinical scales, or ground-truth motion capture were used. The run occurred before the full-body visibility guard was added, and the captured frame was partially cropped; it is therefore not used for any clinical inference. A separate static full-body image was used to verify the pose overlay and plotting path (15 accepted frames, no steps).

### 5.4 Planned validation study

A confirmatory study is planned with 20–30 older adults, three trials per participant, and the following:
- clinical scales: Timed Up and Go (TUG), Berg Balance Scale, gait speed;
- reference: IMU-based gait or marker-based motion capture;
- metrics: ICC(2,1), MAE, Bland–Altman limits, correlation with clinical scores;
- test–retest reliability across one week;
- ethics approval and informed consent.

---

## 6. Results

### 6.1 System integration

The robot booted, calibrated, and executed all eight command categories. The final direction convention was: positive linear velocity moves forward; positive angular velocity turns left; the base stops on idle and on voice “stop.” The keyboard teleoperation and voice command paths shared a single control worker, preventing concurrent serial-bus access.

### 6.2 Voice interaction

Synthetic and live speech tests produced correct intent routing for the command vocabulary. For short utterances on the edge laptop, ASR took approximately 0.2–0.4 s and TTS approximately 1.4–1.9 s. End-to-end voice responses were therefore in the range of roughly 2–4 s including playback. The echo-suppression logic discarded the robot's own TTS in the tested cases; the UI logged an “ignored robot speech” event.

The LLM path returned natural companion replies (e.g., to “I feel a bit lonely”) and stored `engine = llm` in the database. The deterministic safety path returned fixed emergency guidance for chest pain/dyspnea phrases without calling the LLM.

### 6.3 Offline routing and safety evaluation

The labeled corpus was evaluated for three repetitions under both mock models (44 items × 3 repetitions × 2 models = 264 evaluations). Routing was correct for every item under both models, with no safety-critical confusion (Table 3). In particular, no emergency or crisis utterance was answered as conversation or turned into a tool call, and no chitchat or narration item produced a movement command, even when the adversarial model attempted one.

**Table 3. Offline routing and safety results (three repetitions; 132 evaluations per model).**

| Model condition | Overall accuracy | Tool call on emergency/crisis | Missed emergency | Uncommanded motion on chat |
|---|---:|---:|---:|---:|
| Faithful mock | 100.0% (132/132) | 0 | 0 | 0 |
| Adversarial mock | 100.0% (132/132) | 0 | 0 | 0 |

Per-category accuracy was 100% across command, ambiguous narration, emergency, crisis, chitchat, and prompt-injection categories in both conditions. The adversarial result is the key safety finding: when the model is replaced by a hostile policy that emits a movement tool on any motion word, the deterministic guard still reduces every narration and metaphor item to conversation, while preserving all genuine short commands. These numbers are regenerated by `experiments/run_all.sh`; the aggregated tables are stored in `papers/generated_tables.md` and the raw per-item CSV under the run directory. Two limitations apply: the mock models are deterministic stand-ins rather than the deployed endpoint (a `--live` evaluation is supported once an endpoint is configured), and the corpus, though covering the principal failure modes, is not an exhaustive natural-language sample.

### 6.4 Stage latency and service reliability

Across 20 timed trials, deterministic routing—including the keyword mapper, the safety bypass, and the `is_direct_command` guard—completed in 0.52 ms on average (SD 0.34 ms; range 0.01–1.12 ms) on the edge laptop, confirming that the safety-critical decision does not depend on a network round-trip and is effectively instant relative to the 0.2–0.4 s ASR and 1.4–1.9 s TTS stages. The larger per-utterance time recorded inside the evaluation harness reflects its own scoring and logging overhead, not the router. Network LLM completion latency is not reported because no endpoint was configured during the offline runs; the benchmark leaves these rows explicitly unmeasured rather than estimating them.

The supervisor logs covering the available 5.8-day operating period recorded service starts and exits for all five supervised services, five operator-initiated face-window closes (clean exit code 0, distinct from crashes), five historical "missing motor ID" bus events on one drive wheel, and zero camera-reset and zero LLM-failure events in the retained logs. These are descriptive engineering counts over a short, intermittent operating window, not a certified availability measurement; the motor-bus events remain a hardware watch item even though the affected wheel was healthy in the latest scans.

### 6.5 Gait station

The illustrative 7.6 s walk produced the following values (single run; not clinical):

**Table 4. Illustrative gait metrics (n = 1, not for clinical inference).**

| Metric | Value |
|---|---:|
| Duration | 7.57 s |
| Valid frames | 58 |
| Steps | 10 |
| Cadence | 79.3 steps/min |
| Left/right knee mean | 174.7° / 173.8° |
| Left/right knee range | 26.6° / 43.3° |
| Hip mean | 172.8° / 171.8° |
| Trunk mean / std | −2.9° / 3.0° |
| Knee symmetry index | 3.5% |
| Normalized step length | 0.415 |
| Normalized sway | 1.744 |

The sway value illustrates the sensitivity of monocular normalization to camera distance and partial-body effects; the full-body guard was added after this run. The static full-body test produced a valid skeleton overlay and a three-panel curve, confirming the rendering and plotting path.

To characterize the *software pipeline* rather than human gait, all five sessions stored in `health.db` at the time of writing were replayed through a data-quality check (Table 5). Two of five sessions yielded at least ten accepted frames and were treated as usable; the other three were automatically labeled "insufficient data" because the subject was standing, turning, or not fully in frame. Two sessions used the optional depth-assisted path that records a physical step length; the other three were RGB-only. Accepted-frame ratios ranged from 0.01 to 0.42 across sessions that recorded the metric, illustrating how strongly the visibility guard filters partial-body frames. The headline values stored in the database matched the corresponding metrics JSON in all five sessions (zero inconsistencies). Cadence among the usable sessions ranged from 79.3 to 96.0 steps/min; this range is reported only to show the pipeline produces bounded, inspectable outputs, with no normal/abnormal interpretation. Because all captures come from one environment with repeated recordings and no ground truth, none of these values support a clinical claim; they establish that the pipeline runs end to end, records its own quality flags, and keeps its two storage formats consistent.

**Table 5. Gait pipeline data-quality check across the five recorded sessions (pipeline characterization only; not a clinical result).**

| Session | Frames | Steps | Cadence (steps/min) | Depth-assisted | Frame ratio | System label |
|---:|---:|---:|---:|:---:|---:|:---|
| 1 | 58 | 10 | 79.3 | no | n/a | medium |
| 2 | 3 | 0 | 0.0 | no | n/a | insufficient data |
| 3 | 9 | 2 | 12.0 | yes | n/a | high |
| 4 | 1 | 0 | 0.0 | no | 0.01 | insufficient data |
| 5 | 42 | 8 | 96.0 | yes | 0.42 | high |

A session is treated as usable only when it contains at least ten frames; sessions 2–4 therefore do not enter any metric range. The "high/medium" labels are the rule-based screening output stored by the system and are shown for completeness, not as validated risk classifications.

### 6.6 Reports and persistence

The rule-based report for the illustrative session described the cadence, knee range, symmetry, and observed sway/step-length flags, and recommended balance practice, conservative step length, and clinical review if symptoms occur. The session, report, tips, and image paths were written to `health.db`. Chat messages, including the LLM engine tag, were written to `chat.db` and restored after restarting the service.

---

## 7. Discussion

### 7.1 Feasibility and cost

The prototype demonstrates that a single low-cost mobile manipulator can host a coherent care loop: always-on voice, deterministic robot commands, companion chat, RGB gait observation, report generation, and local records. The hardware cost is far below that of most commercial elder-care robots, and the software stack is open and modifiable. This makes the platform attractive for university labs, pilot studies, and personalized research.

### 7.2 What the results do and do not show

The results show **technical feasibility**: the components work together on the physical robot, the voice path is usable, the deterministic command layer demonstrably rejects uncommanded motion even under a hostile model, and the gait/report/database path produces inspectable artifacts. They do **not** show clinical validity, fall-risk prediction, or efficacy in improving elderly health. The gait pilot is a single healthy adult, with no ground truth and a partially cropped frame; the five-session data-quality check characterizes pipeline behavior, not human gait, and is likewise not clinical evidence.

### 7.3 Limitations

1. **Monocular 2D gait:** no absolute step length, no 3D joint kinetics, sensitivity to camera placement, clothing, occlusion, and distance.
2. **Single subject:** no older adults, no control group, no repeated measures.
3. **Depth used only experimentally:** two of five recorded sessions enabled the optional D455/depth-assisted step-length path, but depth is not part of the reported RGB-only configuration; navigation and gait robustness remain limited.
4. **No force/torque sensing:** manipulation safety relies on current limits, soft grippers, and speed limits, not true force control.
5. **LLM and corpus scope:** the 100% offline safety score is measured against deterministic mock models and a 44-utterance corpus, not the full distribution of spontaneous speech or the deployed endpoint. A `--live` evaluation is supported but was not run without a configured endpoint, and the keyword/narration-marker guard cannot cover every phrasing a user might produce; cloud latency, cost, privacy, and hallucination risks remain.
6. **Short reliability window:** the log-derived reliability counts cover roughly six days of intermittent operation and are descriptive, not a certified availability figure; the intermittent single-wheel motor-bus dropout remains a hardware watch item.
7. **Safety certification:** no ISO 13482 assessment; unsupervised use with frail older adults is not claimed.
8. **Database privacy:** local SQLite is convenient but not encrypted by default; access control and encryption are future work.

### 7.4 Future work

- Run the `--live` corpus evaluation against the deployed endpoint and expand the corpus with spontaneous, accented, and dialectal speech, including audio-end-to-end ASR error handling.
- Add D455 or a fixed depth/IMU station for 3D gait and robust navigation.
- Conduct a multi-subject study with TUG/Berg and motion-capture ground truth.
- Add fall detection and emergency alerting with a caregiver loop.
- Integrate Nav2/RTAB-Map and a health-station docking behavior.
- Evaluate on-device LLMs and privacy-preserving speech processing.
- Add encryption, audit logs, and role-based access to the local databases.

---

## 8. Conclusions

This paper presented a low-cost dual-arm mobile robot that integrates always-on voice interaction, deterministic robot commands, LLM-based companionship, RGB gait/posture screening, rule-based reports, health tips, and local SQLite records. The system was exercised on physical hardware and produced inspectable gait and conversation artifacts. A repeatable offline evaluation showed that a layered design—fixed emergency handling plus a deterministic guard that re-validates every model-proposed motion tool—achieved correct routing with zero safety-critical failures across 264 evaluations, including an adversarial model condition, while keeping the safety-critical path on the order of a millisecond and independent of the network. The work supports the technical feasibility of an integrated home-care prototype, while explicitly deferring clinical claims, endpoint-scale language evaluation, and certified reliability to future work with a planned multi-subject study and ground-truth measurements.

---

## Statements

**Ethics:** The illustrative pilot involved one healthy adult volunteer with informed consent. No older adults or patients were included. A future study will require institutional ethics approval.

**Data availability:** The code, configuration, labeled safety corpus, and experiment harness are available from the corresponding author upon reasonable request. All reported offline metrics can be regenerated with a single script that writes per-item CSV/JSON and the aggregated manuscript tables; raw chat and health data are not publicly released because they may contain personal information.

**Conflicts of interest:** The authors declare no conflict of interest.

**Funding:** [Funding agency and grant number].

**Author contributions:** [To be completed].

---

## References

1. United Nations. *World Population Ageing 2020 Highlights*; United Nations: New York, NY, USA, 2020.
2. Broadbent, E.; Stafford, R.; MacDonald, B. Acceptance of healthcare robots for the older population: Review and future directions. *Int. J. Soc. Robot.* **2009**, *1*, 319–330.
3. Abdi, J.; Al-Hindawi, A.; Ng, T.; Vizcaychipi, M.P. Scoping review on the use of socially assistive robot technology in elderly care. *BMJ Open* **2018**, *8*, e018815.
4. Cadene, R.; et al. LeRobot: State-of-the-art Machine Learning for Real-World Robotics in Pytorch. GitHub, 2024. Available online: https://github.com/huggingface/lerobot.
5. Wang, G.; Lu, Z.; Huang, Y.; Liu, Y. XLeRobot: A Practical Low-cost Household Dual-Arm Mobile Robot Design for General Manipulation. GitHub, 2025. Available online: https://github.com/Vector-Wangel/XLeRobot.
6. Lugaresi, C.; Tang, J.; Nash, H.; et al. MediaPipe: A Framework for Building Perception Pipelines. *arXiv* **2019**, arXiv:1906.08172.
7. Bazarevsky, V.; Grishchenko, I.; Raveendran, K.; et al. BlazePose: On-device Real-time Body Pose Tracking. *arXiv* **2020**, arXiv:2006.10204.
8. Silero Team. Silero VAD: Pre-trained Enterprise-grade Voice Activity Detector. GitHub, 2021. Available online: https://github.com/snakers4/silero-vad.
9. Gao, Z.; Li, Z.; Wang, J.; et al. FunASR: A Fundamental End-to-End Speech Recognition Toolkit. *Proc. Interspeech* **2023**.
10. DeepSeek-AI. DeepSeek-V3 Technical Report. *arXiv* **2024**, arXiv:2412.19437.
11. Muro-de-la-Herran, A.; Garcia-Zapirain, B.; Mendez-Zorrilla, A. Gait analysis methods: An overview of wearable and non-wearable systems. *Sensors* **2014**, *14*, 3362–3394.
12. Chen, S.; Lach, J.; Lo, B.; Yang, G.-Z. Toward Pervasive Gait Analysis With Wearable Sensors: A Systematic Review. *IEEE J. Biomed. Health Inform.* **2016**, *20*, 1521–1537.
13. Sadeghi, H.; Allard, P.; Prince, F.; Labelle, H. Symmetry and limb dominance in able-bodied gait: A review. *Gait Posture* **2000**, *12*, 34–45.
14. Mubashir, M.; Shao, L.; Seed, L. A survey on fall detection: Principles and approaches. *Neurocomputing* **2013**, *100*, 144–152.
15. Cao, Z.; Simon, T.; Wei, S.-E.; Sheikh, Y. Realtime Multi-Person 2D Pose Estimation Using Part Affinity Fields. *Proc. CVPR* **2017**, 7291–7299.
16. Sun, K.; Xiao, B.; Liu, D.; Wang, J. Deep High-Resolution Representation Learning for Human Pose Estimation. *Proc. CVPR* **2019**, 5693–5703.
17. Ahn, M.; Brohan, A.; Brown, N.; et al. Do As I Can, Not As I Say: Grounding Language in Robotic Affordances. *arXiv* **2022**, arXiv:2204.01691.
18. Driess, D.; Xia, F.; Sajjadi, M.S.M.; et al. PaLM-E: An Embodied Multimodal Language Model. *Proc. ICML* **2023**.
19. Brohan, A.; Brown, N.; Carbajal, J.; et al. RT-2: Vision-Language-Action Models Transfer Web Knowledge to Robotic Control. *arXiv* **2023**, arXiv:2307.15818.
20. ISO 13482:2014. *Robots and Robotic Devices—Safety Requirements for Personal Care Robots*; International Organization for Standardization: Geneva, Switzerland, 2014.
21. Macenski, S.; Foote, T.; Gerkey, B.; Lalancette, C.; Woodall, W. Robot Operating System 2: Design, architecture, and uses in the wild. *Sci. Robot.* **2022**, *7*, eabm6074.
