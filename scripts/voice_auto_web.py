#!/usr/bin/env python3
"""Always-on voice + robot web agent.

No push-to-talk: a server-side microphone loop uses Silero VAD, then SenseVoice,
routes commands or chat, and speaks the reply through the laptop speakers.

  source scripts/env.sh voice
  python scripts/voice_auto_web.py --port 8082

Robot API: scripts/robot_api.py (8090)
Cameras:   scripts/camera_web.py (8080)
"""
import argparse
import difflib
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import traceback
from collections import deque
from datetime import datetime

import numpy as np
import soundfile as sf
import torch
from flask import Flask, jsonify, request

import face_client as face
import voice_web as vw

CAPTURE_DEV = os.environ.get("CAPTURE_DEV", "default")
PLAYBACK_DEV = os.environ.get("PLAYBACK_DEV", "default")
SAMPLE_RATE = 16000
FRAME = 512  # 32 ms

app = Flask(__name__)

CHAT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "chat")
os.makedirs(CHAT_DIR, exist_ok=True)
CHAT_DB = os.path.join(CHAT_DIR, "chat.db")
MSG_LOCK = threading.Lock()
LISTEN_ENABLED = threading.Event()
LISTEN_ENABLED.set()
LISTEN_PAUSED = threading.Event()
VAD_LOCK = threading.Lock()
ASR_LOCK = threading.Lock()
SHUTDOWN = False
ARECORD = None
LAST_SPOKEN = ""
LAST_SPOKEN_AT = 0.0
RECENT_SPOKEN = []  # (text, timestamp)

# Playback handle so the listener can cut the robot off mid-sentence.
PLAYBACK = {"proc": None, "barged": False}
BARGE = {"hits": 0}

# Anything that looks like a robot command must never be filtered as echo.
COMMAND_WORDS = ("停", "前进", "后退", "左转", "右转", "过来", "报告",
                 "看我", "靠近", "走", "别动", "开始", "结束")


def init_chat_db():
    with sqlite3.connect(CHAT_DB) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            engine TEXT,
            intent TEXT,
            created_at TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_messages_id ON messages(id)")
        db.commit()


def add_message(role, text, engine=None, intent=None):
    with MSG_LOCK, sqlite3.connect(CHAT_DB) as db:
        cur = db.execute(
            "INSERT INTO messages(role,text,engine,intent,created_at) VALUES(?,?,?,?,?)",
            (role, text, engine, intent, datetime.now().isoformat(timespec="seconds")),
        )
        db.commit()
        return {"id": cur.lastrowid, "role": role, "text": text,
                "engine": engine, "intent": intent}


def get_messages(after=0, limit=200):
    with sqlite3.connect(CHAT_DB) as db:
        rows = db.execute(
            "SELECT id,role,text,engine,intent,created_at FROM messages "
            "WHERE id>? ORDER BY id ASC LIMIT ?", (after, limit)
        ).fetchall()
    return [{"id": r[0], "role": r[1], "text": r[2], "engine": r[3],
             "intent": r[4], "created_at": r[5]} for r in rows]


def message_count():
    with sqlite3.connect(CHAT_DB) as db:
        return db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def play_audio(name):
    if not name:
        return
    path = os.path.join(vw.AUDIO_DIR, name)
    if not os.path.exists(path):
        return
    LISTEN_PAUSED.set()
    proc = None
    try:
        # Popen instead of run() so a barge-in can terminate playback.
        proc = subprocess.Popen(["aplay", "-q", "-D", PLAYBACK_DEV, path])
        PLAYBACK["proc"] = proc
        while proc.poll() is None:
            time.sleep(0.02)
    except Exception as e:
        print("aplay failed:", e, flush=True)
    finally:
        PLAYBACK["proc"] = None
        # Keep a short guard for the room tail, but skip it after a barge-in:
        # the user is already talking and must not lose the first syllable.
        if not PLAYBACK["barged"]:
            time.sleep(0.4)
        PLAYBACK["barged"] = False
        LISTEN_PAUSED.clear()


def _norm(t):
    t = re.sub(r"[\s。！？，、,.!?；;：:]+", "", t or "")
    return (t.replace("你", "您")
             .replace("得", "的")
             .replace("那", "哪"))


def _remember_spoken(text):
    now = time.time()
    RECENT_SPOKEN.append((text, now))
    del RECENT_SPOKEN[:-10]


def is_echo(text):
    """Ignore the robot's own TTS being picked up by the microphone.

    The old version used a 15 s window and a 0.70 similarity ratio, so a real
    answer that echoed the robot's wording was silently dropped ("已停止" ->
    user says "停止").  Now: 4 s window, 0.88 ratio, similar length, and a
    command is never treated as echo.
    """
    a = _norm(text)
    if any(k in a for k in COMMAND_WORDS):
        return False
    if len(a) < 3:
        # Punctuation, breath noise, single syllables.
        return True
    now = time.time()
    for spoken, ts in reversed(RECENT_SPOKEN):
        if now - ts > 4.0:
            continue
        b = _norm(spoken)
        if not b:
            continue
        if len(a) >= 4 and a in b:
            return True
        if abs(len(a) - len(b)) > max(2, int(0.4 * len(b))):
            continue
        if difflib.SequenceMatcher(None, a, b).ratio() > 0.88:
            return True
    return False


def process_text(text):
    text = (text or "").strip()
    if not text:
        return None
    out = vw.route_text(text)
    add_message("user", text)
    reply = out.get("reply", "")
    if reply:
        global LAST_SPOKEN, LAST_SPOKEN_AT
        LAST_SPOKEN = reply
        LAST_SPOKEN_AT = time.time()
        _remember_spoken(reply)
        add_message("bot", reply, engine=out.get("engine"), intent=out.get("intent"))
        play_audio(out.get("audio"))
    return out


def listen_loop():
    global ARECORD, SHUTDOWN
    pre = deque(maxlen=10)
    speech = []
    speaking = False
    silence = 0
    print("auto listener thread starting", flush=True)
    while not SHUTDOWN:
        if ARECORD is None or ARECORD.poll() is not None:
            try:
                ARECORD = subprocess.Popen(
                    ["arecord", "-q", "-D", CAPTURE_DEV, "-f", "S16_LE",
                     "-r", str(SAMPLE_RATE), "-c", "1", "-t", "raw"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                print(f"arecord started on {CAPTURE_DEV}", flush=True)
            except Exception as e:
                print("arecord start failed:", e, flush=True)
                add_message("sys", f"麦克风启动失败: {e}")
                time.sleep(1)
                continue
        try:
            data = ARECORD.stdout.read(FRAME * 2)
        except Exception:
            time.sleep(0.2)
            continue
        if not data or len(data) < FRAME * 2:
            continue
        x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0

        if LISTEN_PAUSED.is_set() or not LISTEN_ENABLED.is_set():
            if not LISTEN_ENABLED.is_set():
                # User switched listening off entirely.
                with VAD_LOCK:
                    vw.VAD.reset_states()
                pre.clear()
                speech = []
                speaking = False
                silence = 0
                continue
            # Paused only because the robot is speaking: keep the mic open so
            # the user can cut in.  PipeWire echo cancellation has already
            # removed our own TTS from this signal (~34 dB down).
            pre.append(x)
            with VAD_LOCK:
                prob = vw.VAD(torch.from_numpy(x), SAMPLE_RATE).item()
            BARGE["hits"] = BARGE["hits"] + 1 if prob > 0.60 else 0
            if BARGE["hits"] >= 4:
                proc = PLAYBACK["proc"]
                if proc is not None and proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    print("barge-in: stopped playback", flush=True)
                    face.emotion("listening", hold=8)
                PLAYBACK["proc"] = None
                PLAYBACK["barged"] = True
                BARGE["hits"] = 0
                LISTEN_PAUSED.clear()
                speaking = True
                speech = list(pre)
                silence = 0
            continue

        with VAD_LOCK:
            prob = vw.VAD(torch.from_numpy(x), SAMPLE_RATE).item()

        if not speaking:
            pre.append(x)
            if prob > 0.5:
                speaking = True
                speech = list(pre)
                silence = 0
                print("speech start", flush=True)
                face.emotion("listening", hold=8)
        else:
            speech.append(x)
            if prob < 0.35:
                silence += 1
            else:
                silence = 0
            if silence >= 19 or len(speech) > 375:
                audio = np.concatenate(speech)
                speaking = False
                speech = []
                pre.clear()
                with VAD_LOCK:
                    vw.VAD.reset_states()
                if len(audio) >= int(0.35 * SAMPLE_RATE):
                    tmp = tempfile.mkdtemp(prefix="auto_voice_")
                    try:
                        with ASR_LOCK:
                            text = vw.transcribe(vw.ASR, audio, tmp)
                        print("ASR:", text, flush=True)
                        text = text.strip()
                        if len(_norm(text)) < 2:
                            add_message("sys", "已忽略噪声/气声")
                        elif is_echo(text):
                            add_message("sys", "已忽略机器人自己的语音")
                        else:
                            face.emotion("thinking", hold=12)
                            process_text(text)
                    except Exception as e:
                        print("ASR failed:", type(e).__name__, e, flush=True)
                        add_message("sys", f"识别失败: {e}")
                silence = 0


@app.get("/")
def index():
    return HTML


@app.get("/api/messages")
def api_messages():
    after = int(request.args.get("after", 0))
    return jsonify({"messages": get_messages(after), "listening": LISTEN_ENABLED.is_set(),
                    "paused": LISTEN_PAUSED.is_set()})


@app.post("/api/chat")
def api_chat():
    data = request.get_json(force=True, silent=True) or {}
    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify({"error": "empty text"}), 400
    return jsonify(process_text(text) or {})


@app.post("/api/command")
def api_command():
    data = request.get_json(force=True, silent=True) or {}
    intent = str(data.get("intent", "")).strip().lower()
    if intent not in ("stop", "come", "report", "forward", "backward", "turn_left", "turn_right", "look"):
        return jsonify({"error": "unknown intent"}), 400
    out = vw.handle_intent(intent)
    add_message("user", f"[按钮] {intent}")
    if out.get("reply"):
        _remember_spoken(out["reply"])
        add_message("bot", out["reply"], engine="local", intent=intent)
        play_audio(out.get("audio"))
    return jsonify(out)


@app.post("/api/listen")
def api_listen():
    data = request.get_json(force=True, silent=True) or {}
    enabled = bool(data.get("enabled", True))
    if enabled:
        LISTEN_ENABLED.set()
    else:
        LISTEN_ENABLED.clear()
    return jsonify({"listening": enabled})


@app.get("/api/status")
def api_status():
    st = vw.robot_status()
    st["listening"] = LISTEN_ENABLED.is_set()
    st["paused"] = LISTEN_PAUSED.is_set()
    return jsonify(st)


HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>XLeRobot 养老陪伴</title>
<style>
body{background:#111;color:#eee;font-family:system-ui,sans-serif;margin:0;padding:18px}
h1{font-size:22px;margin:0 0 14px}
.grid{display:grid;grid-template-columns:1.05fr 1fr;gap:16px}
.card{background:#1d1d1f;border-radius:12px;padding:16px}
button{font-size:16px;padding:12px 16px;margin:4px;border:0;border-radius:10px;cursor:pointer}
.stop{background:#d33;color:#fff}.come{background:#2a8;color:#fff}.report{background:#36c;color:#fff}
button:disabled{opacity:.5;cursor:default}
#chat{height:390px;overflow-y:auto;display:flex;flex-direction:column;gap:8px;padding:4px}
.msg{max-width:85%;padding:9px 12px;border-radius:12px;line-height:1.45;white-space:pre-wrap}
.user{align-self:flex-end;background:#2b5f9e;color:#fff}
.bot{align-self:flex-start;background:#2a2a2e;color:#eee}
.sys{align-self:center;color:#888;font-size:12px}
.row{display:flex;gap:8px;margin-top:10px}
input{font-size:16px;padding:11px;border-radius:9px;border:0;flex:1;min-width:0}
#status{font-size:12px;color:#9c9;margin-top:8px;word-break:break-all}
#listenState{font-size:13px;color:#9c9;margin-left:6px}
img{width:100%;background:#000;border-radius:8px;margin-top:8px}
small{color:#888}
.camwrap{display:none}.camwrap.on{display:block}
.camsw{font-size:13px;padding:8px 11px;margin:2px}
</style></head><body>
<h1>XLeRobot 养老陪伴 <small>· 自动语音</small></h1>
<div class="grid">
  <div class="card">
    <button class="stop" onclick="send('stop')">停下</button>
    <button class="come" onclick="send('come')">过来</button>
    <button class="report" onclick="send('report')">报告</button>
    <button onclick="send('forward')">前进</button>
    <button onclick="send('backward')">后退</button>
    <button onclick="send('turn_left')">左转</button>
    <button onclick="send('turn_right')">右转</button>
    <button onclick="send('look')">看我</button>
    <div id="chat"></div>
    <div class="row">
      <input id="text" placeholder="也可以打字：停下 / 过来 / 报告 / 左转 / 看我" onkeydown="if(event.key==='Enter')sendChat()">
      <button onclick="sendChat()">发送</button>
    </div>
    <div class="row">
      <button id="listenBtn" onclick="toggleListen()">暂停监听</button>
      <span id="listenState">🎧 自动监听中，直接说话即可</span>
    </div>
    <div id="status">robot: ...</div>
  </div>
  <div class="card">
    <strong>实时相机</strong>
    <div>
      <button class="camsw" onclick="showCam('head')">头部</button>
      <button class="camsw" onclick="showCam('left')">左腕</button>
      <button class="camsw" onclick="showCam('right')">右腕</button>
      <button class="camsw" onclick="showCam('all')">三路</button>
    </div>
    <div id="wrap_head" class="camwrap"><img id="cam_head" src=""><small>head /dev/video0</small></div>
    <div id="wrap_left" class="camwrap"><img id="cam_left" src=""><small>left /dev/video6</small></div>
    <div id="wrap_right" class="camwrap"><img id="cam_right" src=""><small>right /dev/video2</small></div>
  </div>
</div>
<script>
const camBase = location.protocol + '//' + location.hostname + ':8080';
document.getElementById('cam_head').src = camBase + '/stream/head?ts=' + Date.now();
document.getElementById('cam_left').src = camBase + '/stream/left?ts=' + Date.now();
document.getElementById('cam_right').src = camBase + '/stream/right?ts=' + Date.now();
function showCam(name){
  for(const n of ['head','left','right']){
    document.getElementById('wrap_'+n).classList.toggle('on', name==='all' || name===n);
  }
}
showCam('all');
const chatEl=document.getElementById('chat');
function addMsg(role,text){
  const d=document.createElement('div');
  d.className='msg '+role; d.textContent=text; chatEl.appendChild(d);
  chatEl.scrollTop=chatEl.scrollHeight;
}
let lastId=0;
async function pollMsgs(){
  try{
    const r=await fetch('/api/messages?after='+lastId); const j=await r.json();
    for(const m of j.messages){ lastId=m.id; addMsg(m.role==='bot'?'bot':(m.role==='user'?'user':'sys'), m.text); }
    const st=document.getElementById('listenState');
    if(j.paused) st.textContent='🔊 正在说话...';
    else if(j.listening) st.textContent='🎧 自动监听中，直接说话即可';
    else st.textContent='⏸ 监听已暂停';
  }catch(e){}
}
async function send(intent){
  try{ await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({intent})}); }
  catch(e){ addMsg('sys','错误: '+e); }
  pollMsgs();
}
async function sendChat(){
  const el=document.getElementById('text'); const t=el.value.trim(); if(!t) return;
  el.value='';
  try{ await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})}); }
  catch(e){ addMsg('sys','错误: '+e); }
  pollMsgs();
}
async function toggleListen(){
  const cur=document.getElementById('listenBtn').textContent.indexOf('暂停')>=0;
  await fetch('/api/listen',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!cur})});
  document.getElementById('listenBtn').textContent = cur ? '继续监听' : '暂停监听';
  pollMsgs();
}
async function pollStatus(){
  try{
    const r=await fetch('/api/status'); const j=await r.json();
    document.getElementById('status').textContent='robot: '+JSON.stringify(j);
  }catch(e){ document.getElementById('status').textContent='robot: offline'; }
}
setInterval(pollMsgs,700);
setInterval(pollStatus,2000);
pollMsgs(); pollStatus();
</script></body></html>
"""


def main():
    global SHUTDOWN, CAPTURE_DEV, PLAYBACK_DEV
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8082)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--capture-dev", default=CAPTURE_DEV)
    ap.add_argument("--playback-dev", default=PLAYBACK_DEV)
    ap.add_argument("--no-listen", action="store_true")
    args = ap.parse_args()
    CAPTURE_DEV = args.capture_dev
    PLAYBACK_DEV = args.playback_dev

    vw.load_models()
    init_chat_db()
    if message_count() == 0:
        add_message("bot", "您好，我是小乐，您的居家陪伴机器人。今天感觉怎么样？")
    print(f"auto voice web on http://{args.host}:{args.port}", flush=True)
    print("LLM configured:", bool(os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_MODEL")), flush=True)
    if not args.no_listen:
        threading.Thread(target=listen_loop, daemon=True).start()
    try:
        app.run(host=args.host, port=args.port, threaded=True)
    finally:
        SHUTDOWN = True
        if ARECORD is not None:
            try:
                ARECORD.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    main()
