#!/usr/bin/env python3
"""Minimal voice loop: arecord -> Silero VAD -> SenseVoice -> edge-tts -> aplay.

Usage:
  python scripts/voice_loop.py --record 5
  python scripts/voice_loop.py --input data/audio/tts.wav
"""
import argparse, asyncio, os, re, subprocess, sys, tempfile, time
import numpy as np
import soundfile as sf
import torch
from scipy.signal import resample_poly

CAPTURE_DEV = "plughw:2,0"
PLAYBACK_DEV = "plughw:2,0"
SR = 16000


def record(path: str, seconds: int, device: str = CAPTURE_DEV) -> None:
    cmd = ["arecord", "-q", "-D", device, "-f", "S16_LE", "-r", str(SR),
           "-c", "1", "-d", str(seconds), path]
    subprocess.run(cmd, check=True)


def load_16k(path: str) -> np.ndarray:
    x, sr = sf.read(path, dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    if sr != SR:
        x = resample_poly(x, SR, sr).astype("float32")
    return x


def vad_trim(path: str, vad) -> tuple[np.ndarray, list]:
    from silero_vad import get_speech_timestamps
    x = load_16k(path)
    ts = get_speech_timestamps(torch.from_numpy(x), vad, sampling_rate=SR,
                               return_seconds=False, min_speech_duration_ms=200,
                               min_silence_duration_ms=250)
    if not ts:
        return x[:0], []
    start, end = ts[0]["start"], ts[-1]["end"]
    return x[start:end], ts


def transcribe(model, wav: np.ndarray, tmpdir: str) -> str:
    p = os.path.join(tmpdir, "trim.wav")
    sf.write(p, wav, SR)
    res = model.generate(input=p, language="zh", use_itn=True, batch_size_s=60)
    text = res[0]["text"] if res else ""
    # strip SenseVoice tags like <|zh|><|HAPPY|><|Speech|><|withitn|>
    text = re.sub(r"<\|[^|]*\|>", "", text).strip()
    return text


async def _tts(text: str, mp3: str) -> None:
    import edge_tts
    await edge_tts.Communicate(text, "zh-CN-XiaoxiaoNeural").save(mp3)


def tts(text: str, mp3: str, wav: str) -> None:
    asyncio.run(_tts(text, mp3))
    x, sr = sf.read(mp3, dtype="float32")
    sf.write(wav, x, sr)


def play(wav: str, device: str = PLAYBACK_DEV) -> None:
    subprocess.run(["aplay", "-q", "-D", device, wav], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", type=int, default=0, help="record N seconds from mic")
    ap.add_argument("--input", default="", help="use an existing wav instead of mic")
    ap.add_argument("--reply", default="", help="fixed reply text (default echoes ASR)")
    ap.add_argument("--no-play", action="store_true")
    ap.add_argument("--capture-dev", default=CAPTURE_DEV)
    ap.add_argument("--playback-dev", default=PLAYBACK_DEV)
    args = ap.parse_args()

    tmpdir = tempfile.mkdtemp(prefix="voice_loop_")
    src = args.input
    if args.record:
        src = os.path.join(tmpdir, "mic.wav")
        print(f"[1/4] recording {args.record}s from {args.capture_dev} ...", flush=True)
        t0 = time.time()
        record(src, args.record, args.capture_dev)
        print(f"      recorded {time.time()-t0:.1f}s -> {src}", flush=True)
    if not src:
        ap.error("need --record N or --input FILE")

    print("[2/4] loading VAD + SenseVoice ...", flush=True)
    from silero_vad import load_silero_vad
    from funasr import AutoModel
    vad = load_silero_vad()
    model = AutoModel(model="iic/SenseVoiceSmall", trust_remote_code=True,
                      device="cpu", disable_update=True)

    t0 = time.time()
    speech, ts = vad_trim(src, vad)
    print(f"      VAD: {len(ts)} segment(s), {len(speech)/SR:.2f}s speech, {time.time()-t0:.2f}s", flush=True)
    if len(speech) < SR * 0.2:
        print("      no speech detected; aborting.")
        return 2

    t0 = time.time()
    text = transcribe(model, speech, tmpdir)
    print(f"[3/4] ASR ({time.time()-t0:.2f}s): {text}", flush=True)
    if not text:
        return 3

    reply = args.reply or text
    mp3 = os.path.join(tmpdir, "reply.mp3")
    wav = os.path.join(tmpdir, "reply.wav")
    t0 = time.time()
    tts(reply, mp3, wav)
    print(f"      TTS ({time.time()-t0:.2f}s): {reply}", flush=True)

    if not args.no_play:
        t0 = time.time()
        play(wav, args.playback_dev)
        print(f"[4/4] played ({time.time()-t0:.2f}s)", flush=True)
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
