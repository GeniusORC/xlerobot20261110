#!/usr/bin/env python3
"""Voice + robot web demo with an elderly-companion chat layer.

Run:
  source scripts/env.sh voice
  python scripts/voice_web.py --port 8082

Robot control: scripts/robot_api.py on 8090
Cameras:       scripts/camera_web.py on 8080

The chat layer uses an OpenAI-compatible LLM if configured:
  LLM_BASE_URL=https://api.deepseek.com/v1
  LLM_API_KEY=...
  LLM_MODEL=deepseek-chat

Without these variables it falls back to a local rule-based companion.
"""
import argparse
import os
import random
import subprocess
import tempfile
import time
import traceback
import uuid
from datetime import datetime

import requests
from flask import Flask, jsonify, request, send_from_directory

from voice_loop import tts, transcribe, vad_trim

ROBOT_API = os.environ.get("ROBOT_API", "http://127.0.0.1:8090")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIO_DIR = os.path.join(ROOT, "data", "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

VAD = None
ASR = None
CHAT_HISTORY = []
MAX_HISTORY = 12

EMERGENCY_WORDS = (
    "胸痛", "胸口疼", "呼吸困难", "喘不过", "说不出话", "嘴歪", "半身",
    "跌倒", "摔倒", "救命", "晕倒", "意识不清", "大出血",
)
CRISIS_WORDS = ("不想活", "自杀", "活着没意思", "想死")


def system_prompt() -> str:
    return (
        "你是“小乐”，一台面向老年人的居家陪伴机器人。"
        "说话要温暖、简短、口语化，语速慢，先共情再回答。"
        "不要假装自己是真人；不要做医学诊断，不要开药，不要夸大能力。"
        "涉及胸痛、呼吸困难、跌倒、意识不清等紧急情况时，"
        "立即建议坐下/躺下、呼叫家人或拨打120。"
        "可以聊家常、提醒吃药、询问睡眠和心情、解释机器人能做什么。"
        "每次回答尽量控制在80个汉字以内。"
    )


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
    if any(k in t for k in ("停下", "停止", "停住", "别动", "不要动")):
        return "stop"
    if any(k in t for k in ("看我", "看着我", "面向我", "转过来", "看看我")):
        return "look"
    if any(k in t for k in ("左转", "向左转", "往左转")):
        return "turn_left"
    if any(k in t for k in ("右转", "向右转", "往右转")):
        return "turn_right"
    if any(k in t for k in ("前进", "向前", "往前", "往前走", "走一点")):
        return "forward"
    if any(k in t for k in ("后退", "向后", "往后", "退一点", "退后")):
        return "backward"
    if any(k in t for k in ("过来", "到这里", "来一下", "过来一下", "靠近")):
        return "come"
    if any(k in t for k in ("报告", "汇报", "状态", "情况", "报一下")):
        return "report"
    return "unknown"


def is_emergency(text: str) -> bool:
    return any(k in (text or "") for k in EMERGENCY_WORDS)


def is_crisis(text: str) -> bool:
    return any(k in (text or "") for k in CRISIS_WORDS)


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


def fallback_chat(text: str) -> str:
    t = (text or "").strip()
    now = datetime.now()
    if not t:
        return "我在听，您慢慢说。"
    if is_crisis(t):
        return (
            "我很担心您现在的感受。请先不要一个人待着，马上联系家人，"
            "或拨打心理援助热线。您愿意先跟我说说发生了什么吗？"
        )
    if is_emergency(t):
        return (
            "请先坐下或躺下，不要勉强走动。如果胸痛、呼吸困难、说话不清，"
            "请立即叫家人帮忙，或拨打120。需要我帮您呼叫家人吗？"
        )
    if any(k in t for k in ("你好", "在吗", "早上好", "晚上好", "你是谁")):
        return "您好，我是小乐，您的居家陪伴机器人。今天感觉怎么样？"
    if any(k in t for k in ("孤单", "寂寞", "无聊", "没人", "一个人")):
        return "我陪您说说话。您想聊聊家里人，还是听听以前喜欢的事？"
    if any(k in t for k in ("睡", "失眠", "睡不着", "早醒")):
        return "睡不好确实难受。睡前少看屏幕，泡泡脚、听听轻音乐。如果长期失眠，记得告诉医生。"
    if any(k in t for k in ("吃药", "药", "服药")):
        return "吃药要按医生的嘱咐来，别自己加量或停药。需要我帮您记一下下一次吃药时间吗？"
    if any(k in t for k in ("疼", "痛", "不舒服", "头晕", "心慌")):
        return "先坐下休息，别着急走动。如果不舒服持续或加重，请马上告诉家人，必要时去医院。"
    if any(k in t for k in ("孩子", "儿子", "女儿", "家人", "老伴")):
        return "家人一定很惦记您。要不要我帮您记下想对他们说的话，等他们回来再说？"
    if any(k in t for k in ("天气",)):
        return f"现在是{now.hour}点{now.minute:02d}分。天气我暂时查不到，出门前记得看看窗外、添件衣服。"
    if any(k in t for k in ("几点", "时间", "现在")):
        return f"现在是{now.hour}点{now.minute:02d}分。"
    if any(k in t for k in ("会什么", "能做什么", "功能", "帮我")):
        return "我可以陪您聊天、提醒吃药，也能听“停下、过来、报告”来移动和汇报状态。您想让我做什么？"
    if any(k in t for k in ("谢谢", "辛苦了")):
        return "不客气，能陪着您我就很高兴。"
    if any(k in t for k in ("再见", "拜拜")):
        return "好，您慢一点，记得照顾好自己。需要我就叫我。"
    # Generic empathic fallback.
    prompts = [
        "我在听。您愿意再多说一点吗？",
        "这件事听起来挺让您费心的。后来呢？",
        "嗯，我明白您的意思。您现在最想解决哪一件事？",
        "您说得对。要不要我陪您一起想想办法？",
    ]
    return random.choice(prompts)


def llm_chat(text: str) -> tuple[str | None, str]:
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")
    if not base or not model:
        return None, "fallback"
    messages = [{"role": "system", "content": system_prompt()}]
    messages.extend(CHAT_HISTORY[-MAX_HISTORY:])
    messages.append({"role": "user", "content": text})
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        r = requests.post(
            f"{base}/chat/completions",
            headers=headers,
            json={"model": model, "messages": messages, "temperature": 0.6, "max_tokens": 220},
            timeout=20,
        )
        r.raise_for_status()
        j = r.json()
        reply = j["choices"][0]["message"]["content"].strip()
        return reply[:300], "llm"
    except Exception as e:
        print("LLM failed, using fallback:", type(e).__name__, e, flush=True)
        return None, "fallback"


def chat_reply(text: str) -> tuple[str, str]:
    if is_crisis(text) or is_emergency(text):
        reply, engine = fallback_chat(text), "safety"
    else:
        reply, engine = llm_chat(text)
        if reply is None:
            reply, engine = fallback_chat(text), "fallback"
    CHAT_HISTORY.append({"role": "user", "content": text})
    CHAT_HISTORY.append({"role": "assistant", "content": reply})
    del CHAT_HISTORY[:-MAX_HISTORY]
    return reply, engine


def handle_intent(intent: str, text: str = "") -> dict:
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
    if intent == "forward":
        result = robot_command("forward")
        reply = "好的，向前一点。"
        return {"text": text, "intent": intent, "reply": reply, "robot": result,
                "audio": speech_file(reply)}
    if intent == "backward":
        result = robot_command("backward")
        reply = "好的，向后退一点。"
        return {"text": text, "intent": intent, "reply": reply, "robot": result,
                "audio": speech_file(reply)}
    if intent == "turn_left":
        result = robot_command("turn_left")
        reply = "好的，向左转。"
        return {"text": text, "intent": intent, "reply": reply, "robot": result,
                "audio": speech_file(reply)}
    if intent == "turn_right":
        result = robot_command("turn_right")
        reply = "好的，向右转。"
        return {"text": text, "intent": intent, "reply": reply, "robot": result,
                "audio": speech_file(reply)}
    if intent == "look":
        result = robot_command("look")
        reply = "好的，我看着您。"
        return {"text": text, "intent": intent, "reply": reply, "robot": result,
                "audio": speech_file(reply)}
    return {"text": text, "intent": intent, "reply": "未知意图"}


def route_text(text: str) -> dict:
    intent = map_intent(text)
    if intent != "unknown":
        return {**handle_intent(intent, text), "mode": "command"}
    reply, engine = chat_reply(text)
    return {"text": text, "intent": "chat", "reply": reply, "engine": engine,
            "audio": speech_file(reply), "mode": "chat"}


@app.get("/")
def index():
    return HTML


@app.post("/api/chat")
def api_chat():
    data = request.get_json(force=True, silent=True) or {}
    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify({"error": "empty text"}), 400
    return jsonify(route_text(text))


@app.post("/api/command")
def api_command():
    data = request.get_json(force=True, silent=True) or {}
    intent = str(data.get("intent", "")).strip().lower()
    if intent not in ("stop", "come", "report", "forward", "backward", "turn_left", "turn_right", "look"):
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
        out = route_text(text)
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
<html><head><meta charset="utf-8"><title>XLeRobot 养老陪伴</title>
<style>
body{background:#111;color:#eee;font-family:system-ui,sans-serif;margin:0;padding:18px}
h1{font-size:22px;margin:0 0 14px}
.grid{display:grid;grid-template-columns:1.05fr 1fr;gap:16px}
.card{background:#1d1d1f;border-radius:12px;padding:16px}
button{font-size:17px;padding:13px 18px;margin:5px;border:0;border-radius:10px;cursor:pointer}
.stop{background:#d33;color:#fff}.come{background:#2a8;color:#fff}.report{background:#36c;color:#fff}
.mic{background:#eee;color:#111}
button:disabled{opacity:.5;cursor:default}
#chat{height:380px;overflow-y:auto;display:flex;flex-direction:column;gap:8px;padding:4px}
.msg{max-width:85%;padding:9px 12px;border-radius:12px;line-height:1.45;white-space:pre-wrap}
.user{align-self:flex-end;background:#2b5f9e;color:#fff}
.bot{align-self:flex-start;background:#2a2a2e;color:#eee}
.sys{align-self:center;color:#888;font-size:12px}
.row{display:flex;gap:8px;margin-top:10px}
input{font-size:16px;padding:11px;border-radius:9px;border:0;flex:1;min-width:0}
#status{font-size:12px;color:#9c9;margin-top:8px;word-break:break-all}
img{width:100%;background:#000;border-radius:8px;margin-top:8px}
small{color:#888}
.camwrap{display:none}
.camwrap.on{display:block}
.camsw{font-size:13px;padding:8px 11px;margin:2px}
</style></head><body>
<h1>XLeRobot 养老陪伴</h1>
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
      <input id="text" placeholder="和小乐聊聊，或说：停下 / 过来 / 报告 / 前进 / 后退 / 左转 / 右转 / 看我" onkeydown="if(event.key==='Enter')sendChat()">
      <button onclick="sendChat()">发送</button>
    </div>
    <div class="row">
      <button id="mic" class="mic" onclick="toggleMic()">🎤 开始录音</button>
      <span id="micState"><small>浏览器麦克风</small></span>
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
function play(a){ if(a){ const x=new Audio('/audio/'+a); x.play().catch(()=>{}); } }
async function send(intent){
  addMsg('sys','[按钮] '+intent);
  try{
    const r=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({intent})});
    const j=await r.json(); if(j.reply) addMsg('bot',j.reply); play(j.audio);
  }catch(e){ addMsg('sys','错误: '+e); }
}
async function sendChat(){
  const el=document.getElementById('text'); const t=el.value.trim(); if(!t) return;
  el.value=''; addMsg('user',t);
  try{
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})});
    const j=await r.json();
    if(j.text && j.mode==='command') addMsg('sys','[意图] '+j.intent);
    if(j.reply) addMsg('bot',j.reply); play(j.audio);
  }catch(e){ addMsg('sys','错误: '+e); }
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
        const j=await r.json();
        if(j.text) addMsg('user',j.text);
        if(j.reply) addMsg('bot',j.reply); play(j.audio);
      }catch(e){ addMsg('sys','识别错误: '+e); }
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
addMsg('bot','您好，我是小乐，您的居家陪伴机器人。今天感觉怎么样？');
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
    print("LLM configured:", bool(os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_MODEL")), flush=True)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
