#!/usr/bin/env python3
"""Voice + robot web demo: 停下 / 过来 / 报告.

Runs in the voice environment (.venv). Robot control is provided by
scripts/robot_api.py on port 8090; camera streams by scripts/camera_web.py
on port 8080.

  source scripts/env.sh voice
  python scripts/voice_web.py --port 8082
"""
import argparse
import os
import re
import subprocess
import tempfile
import time
import traceback
import uuid

import requests
from flask import Flask, jsonify, request, send_from_directory

from voice_loop import tts, transcribe, vad_trim

ROBOT_API = os.environ.get("ROBOT_API", "http://127.0.0.1:8090")
AUDIO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

VAD = None
ASR = None


def load_models():
    global VAD, ASR
    from silero_vad import load_silero_vad
    from funasr import AutoModel

    print("loading VAD + SenseVoice...", flush=True)
    VAD = load_silero_vad()
    ASR = AutoModel(
        model="iic/SenseVoiceSmall",
        trust_remote_code=True,
        device="cpu",
        disable_update=True,
    )
    print("models ready", flush=True)


def map_intent(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ("停下", "停止", "停", "别动", "不要动", "停住")):
        return "stop"
    if any(k in t for k in ("过来", "到这里", "来一下", "过来一下", "靠近")):
        return "come"
    if any(k in t for k in ("报告", "汇报", "状态", "情况", "报一下")):
        return "report"
    return "unknown"


def robot_command(intent: str) -> dict:
    try:
        r = requests.post(f"{ROBOT_API}/command", json={"intent": intent}, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": f"robot API: {type(e).__name__}: {e}"}


def robot_status() -> dict:
    try:
        r = requests.get(f"{ROBOT_API}/status", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"connected": False, "error": str(e)}


def speech_file(text: str) -> str | None:
    name = f"reply_{uuid.uuid4().hex}.wav"
    mp3 = os.path.join(AUDIO_DIR, name.replace(".wav", ".mp3"))
    wav = os.path.join(AUDIO_DIR, name)
    try:
        tts(text, mp3, wav)
        return name
    except Exception as e:
        print("TTS failed:", type(e).__name__, e, flush=True)
        return None


def handle_intent(intent: str, text: str = "") -> dict:
    if intent == "unknown":
        reply = "我没听清，请说停下、过来或报告。"
        return {"text": text, "intent": intent, "reply": reply, "audio": speech_file(reply)}
    if intent == "stop":
        result = robot_command("stop")
        reply = "好的，已停止。"
        return {"text": text, "intent": intent, "reply": reply, "robot": result,
                "audio": speech_file(reply)}
    if intent == "come":
        result = robot_command("come")
        reply = "我过来了，请注意安全。"
        return {"text": text, "intent": intent, "reply": reply, "robot": result,
                "audio": speech_file(reply)}
    if intent == "report":
        result = robot_command("report")
        status = robot_status()
        if status.get("connected"):
            reply = "机器人在线，底盘已停止，双臂和双轮已连接。"
        else:
            reply = "机器人未连接，请检查串口和电源。"
        return {"text": text, "intent": intent, "reply": reply,
                "robot": result, "status": status, "audio": speech_file(reply)}
    return {"text": text, "intent": intent, "reply": "未知意图"}


@app.get("/")
def index():
    return HTML


@app.post("/api/command")
def api_command():
    data = request.get_json(force=True, silent=True) or {}
    intent = str(data.get("intent", "")).strip().lower()
    if intent not in ("stop", "come", "report"):
        return jsonify({"error": "intent must be stop|come|report"}), 400
    return jsonify(handle_intent(intent))


@app.post("/api/voice")
def api_voice():
    if VAD is None or ASR is None:
        return jsonify({"error": "models not ready"}), 503
    f = request.files.get("audio")
    if f is None:
        return jsonify({"error": "missing audio file"}), 400
    tmp = tempfile.mkdtemp(prefix="voice_web_")
    src = os.path.join(tmp, "in.webm")
    wav = os.path.join(tmp, "in.wav")
    f.save(src)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-ar", "16000", "-ac", "1", wav],
            check=True,
        )
        speech, segments = vad_trim(wav, VAD)
        if len(speech) < 16000 * 0.2:
            reply = "我没有听到说话，请再说一遍。"
            return jsonify({"text": "", "intent": "unknown", "reply": reply,
                            "vad_segments": len(segments), "audio": speech_file(reply)})
        text = transcribe(ASR, speech, tmp)
        intent = map_intent(text)
        out = handle_intent(intent, text)
        out["vad_segments"] = len(segments)
        return jsonify(out)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.get("/api/status")
def api_status():
    return jsonify(robot_status())


@app.get("/audio/<name>")
def audio(name):
    return send_from_directory(AUDIO_DIR, name)


HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>XLeRobot Voice Demo</title>
<style>
body{background:#111;color:#eee;font-family:system-ui,sans-serif;margin:0;padding:18px}
h1{font-size:22px;margin:0 0 14px}
.grid{display:grid;grid-template-columns:1.1fr 1fr;gap:16px}
.card{background:#1d1d1f;border-radius:12px;padding:16px}
button{font-size:18px;padding:14px 20px;margin:6px;border:0;border-radius:10px;cursor:pointer}
.stop{background:#d33;color:#fff}.come{background:#2a8;color:#fff}.report{background:#36c;color:#fff}
.mic{background:#eee;color:#111}
button:disabled{opacity:.5;cursor:default}
#log{white-space:pre-wrap;font-size:14px;line-height:1.5;min-height:120px}
#status{font-size:13px;color:#9c9}
img{width:100%;background:#000;border-radius:8px;margin-top:8px}
small{color:#888}
input{font-size:16px;padding:10px;border-radius:8px;border:0;width:60%}
</style></head><body>
<h1>XLeRobot 语音控制</h1>
<div class="grid">
  <div class="card">
    <button class="stop" onclick="send('stop')">停下</button>
    <button class="come" onclick="send('come')">过来</button>
    <button class="report" onclick="send('report')">报告</button>
    <hr>
    <button id="mic" class="mic" onclick="toggleMic()">🎤 开始录音</button>
    <span id="micState"><small>浏览器麦克风</small></span>
    <p><input id="text" placeholder="或输入：停下 / 过来 / 报告">
       <button onclick="sendText()">发送</button></p>
    <div id="log">准备就绪。</div>
    <div id="status">status: ...</div>
  </div>
  <div class="card">
    <strong>实时相机</strong>
    <img id="cam_head" src="">
    <small>head /dev/video0</small>
    <img id="cam_left" src="">
    <small>left /dev/video6</small>
    <img id="cam_right" src="">
    <small>right /dev/video2</small>
  </div>
</div>
<script>
const camBase = location.protocol + '//' + location.hostname + ':8080';
document.getElementById('cam_head').src = camBase + '/stream/head?ts=' + Date.now();
document.getElementById('cam_left').src = camBase + '/stream/left?ts=' + Date.now();
document.getElementById('cam_right').src = camBase + '/stream/right?ts=' + Date.now();
const logEl=document.getElementById('log');
function log(s){ logEl.textContent = s + "\n" + logEl.textContent; }
function play(a){ if(a){ const x=new Audio('/audio/'+a); x.play().catch(()=>{}); } }
async function send(intent){
  log('发送意图: '+intent);
  try{
    const r=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({intent})});
    const j=await r.json(); log(JSON.stringify(j,null,2)); play(j.audio);
  }catch(e){ log('错误: '+e); }
}
function sendText(){
  const t=document.getElementById('text').value.trim();
  if(!t) return;
  const intent = /停|别动/.test(t) ? 'stop' : (/过来|靠近/.test(t) ? 'come' : (/报告|状态|汇报/.test(t) ? 'report' : ''));
  if(intent) send(intent); else log('未识别意图: '+t);
}
let rec=null, chunks=[], stream=null;
async function toggleMic(){
  const btn=document.getElementById('mic'), st=document.getElementById('micState');
  if(rec && rec.state==='recording'){ rec.stop(); return; }
  try{
    stream=await navigator.mediaDevices.getUserMedia({audio:true});
    chunks=[]; rec=new MediaRecorder(stream,{mimeType:'audio/webm'});
    rec.ondataavailable=e=>{ if(e.data.size) chunks.push(e.data); };
    rec.onstop=async()=>{
      btn.textContent='🎤 开始录音'; st.textContent='识别中...';
      const blob=new Blob(chunks,{type:'audio/webm'});
      const fd=new FormData(); fd.append('audio',blob,'voice.webm');
      try{
        const r=await fetch('/api/voice',{method:'POST',body:fd});
        const j=await r.json(); log(JSON.stringify(j,null,2)); play(j.audio);
      }catch(e){ log('识别错误: '+e); }
      st.textContent='浏览器麦克风';
      stream.getTracks().forEach(t=>t.stop());
    };
    rec.start(); btn.textContent='⏹ 停止录音'; st.textContent='录音中...';
  }catch(e){ st.textContent='麦克风不可用: '+e; }
}
async function poll(){
  try{
    const r=await fetch('/api/status'); const j=await r.json();
    document.getElementById('status').textContent='robot: '+JSON.stringify(j);
  }catch(e){ document.getElementById('status').textContent='robot: offline'; }
}
setInterval(poll,2000); poll();
</script></body></html>
"""


def main():
    global ROBOT_API
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8082)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--robot-api", default=ROBOT_API)
    args = ap.parse_args()
    ROBOT_API = args.robot_api
    load_models()
    print(f"voice web on http://{args.host}:{args.port}", flush=True)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
