#!/usr/bin/env python3
"""One-turn LLM brain for the companion robot.

The companion used to decide between "chat" and "drive the robot" with a
hand-written keyword table (``voice_web.map_intent``).  That misses most
natural phrasing ("把车往沙发那边挪一点") and silently drops the numbers in
"往前走三十厘米".  This module replaces it with a single DeepSeek
function-calling turn: the model either calls a robot tool or answers in
prose, so the decision and the wording come from the same context.

It also keeps durable facts about the user (name, conditions, medication,
family) in ``data/chat/chat.db`` so the robot stops re-asking the same
question every session.  Extraction runs in a background thread and never
adds latency to the spoken reply.

Environment (same as voice_web):
  LLM_BASE_URL=https://api.deepseek.com/v1
  LLM_API_KEY=...
  LLM_MODEL=deepseek-chat
"""
import json
import os
import sqlite3
import threading
from datetime import datetime

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAT_DIR = os.path.join(ROOT, "data", "chat")
os.makedirs(CHAT_DIR, exist_ok=True)
CHAT_DB = os.path.join(CHAT_DIR, "chat.db")

MAX_HISTORY = 10
MAX_FACTS = 12
LLM_TIMEOUT = 12

# ---------------------------------------------------------------------------
# Tools the model may call.  The names are stable: voice_web.handle_action()
# maps them onto robot_api endpoints.
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "stop_robot",
            "description": "立即让机器人停下。用户说停、别动、等一下、不要走了时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_robot",
            "description": (
                "让机器人底盘前进/后退/转向。用户说了距离或角度就按用户说的填，"
                "没说就用默认值。前进和后退不要同时填。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "forward_m": {
                        "type": "number",
                        "description": "前进为正、后退为负，单位米。默认 0.3，范围 -2 到 2。",
                    },
                    "turn_deg": {
                        "type": "number",
                        "description": "左转为正、右转为负，单位度。默认 0，范围 -180 到 180。",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "come_here",
            "description": "让机器人朝用户开过来、靠近一点。用户说过来、过来一下、靠近点时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "look_at_me",
            "description": "让机器人转头面向用户。用户说看我、看着我、转过来时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_status",
            "description": "播报机器人自身状态（是否在线、底盘速度）。用户问状态、报告、情况怎么样时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

ACTION_REPLY = {
    "stop_robot": "好的，停下了。",
    "come_here": "我过来了，您别动。",
    "look_at_me": "我看着您呢。",
    "report_status": "我看看自己的状态。",
}


# ---------------------------------------------------------------------------
# Durable facts
# ---------------------------------------------------------------------------
_FACT_LOCK = threading.Lock()


def _db():
    conn = sqlite3.connect(CHAT_DB, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS facts("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    return conn


def facts() -> dict:
    try:
        with _db() as db:
            rows = db.execute(
                "SELECT key, value FROM facts ORDER BY updated_at DESC LIMIT ?",
                (MAX_FACTS,),
            ).fetchall()
        return {k: v for k, v in rows}
    except Exception as e:
        print("agent_brain: facts read failed:", type(e).__name__, e, flush=True)
        return {}


def save_facts(new: dict) -> None:
    if not new:
        return
    now = datetime.now().isoformat(timespec="seconds")
    with _FACT_LOCK, _db() as db:
        for k, v in new.items():
            k = str(k).strip()[:40]
            v = str(v).strip()[:200]
            if k and v:
                db.execute(
                    "INSERT INTO facts(key,value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (k, v, now),
                )
        # Keep the table small: drop the oldest entries beyond 3x MAX_FACTS.
        db.execute(
            "DELETE FROM facts WHERE key NOT IN "
            "(SELECT key FROM facts ORDER BY updated_at DESC LIMIT ?)",
            (MAX_FACTS * 3,),
        )
        db.commit()


def _facts_block() -> str:
    f = facts()
    if not f:
        return "（还没有记录，第一次见面。）"
    return "\n".join(f"- {k}：{v}" for k, v in f.items())


def system_prompt() -> str:
    return (
        "你是“小乐”，一台面向老年人的居家陪伴机器人。\n"
        "规则：\n"
        "1. 先直接回答对方这句话，再补一句关心，不要绕圈子。\n"
        "2. 不知道的事（天气、新闻、政策、药量、价格）就说“这个我不太清楚”，"
        "绝不编造。\n"
        "3. 下面是你记得的事实，用得上就用，别重复问已经知道的事。\n"
        "4. 用户要机器人动，就调用工具；只是聊天不要调用工具。"
        "用户在说身体不舒服、心情、家常、往事时，绝对不要调用移动工具。"
        "只有用户明确要求前进、后退、转向、停下、过来或看你时才调用工具，"
        "即使你不确定机器人是否在线也要调用，不要只用文字敷衍。\n"
        "5. 口语化，一般 2-3 句，不超过 120 个汉字。\n"
        "6. 不做医学诊断、不开药。遇到胸痛、呼吸困难、跌倒、意识不清，"
        "立刻建议坐下或躺下、叫家人、拨打 120。\n"
        "7. 不要假装自己是真人。\n"
        f"\n你记得的事实：\n{_facts_block()}\n"
    )


# ---------------------------------------------------------------------------
# LLM plumbing
# ---------------------------------------------------------------------------
def _post(messages, tools=None, temperature=0.5, max_tokens=300,
          response_format=None):
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")
    if not base or not model:
        raise RuntimeError("LLM_BASE_URL / LLM_MODEL not configured")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if response_format:
        body["response_format"] = response_format
    r = requests.post(f"{base}/chat/completions", headers=headers,
                      json=body, timeout=LLM_TIMEOUT)
    r.raise_for_status()
    return r.json()


def build_messages(text: str, history=None, context=None) -> list:
    msgs = [{"role": "system", "content": system_prompt()}]
    if context:
        msgs.append({
            "role": "system",
            "content": f"当前视觉输入（机器人头部相机，自动检测）：{context}。"
                       "回答与面前的人、距离、是否看得见相关的问题时以此为准；"
                       "看不到就说看不到，不要编。",
        })
    for m in (history or [])[-MAX_HISTORY:]:
        role = m.get("role")
        if role in ("user", "assistant") and m.get("content"):
            msgs.append({"role": role, "content": m["content"]})
    msgs.append({"role": "user", "content": text})
    return msgs


# Used to sanity-check a tool call: a move/come/look decision on a sentence
# with none of these words is a model misfire and is re-asked as plain chat.
MOTION_WORDS = ("前", "后", "左", "右", "转", "走", "动", "停", "退",
                "过来", "靠近", "面向", "看我", "让开", "别动")


def has_motion_words(text: str) -> bool:
    return any(k in (text or "") for k in MOTION_WORDS)


def chat_only(text: str, history=None, context=None):
    """Plain completion without tools, used when a tool call misfires."""
    try:
        j = _post(build_messages(text, history, context))
        msg = j["choices"][0]["message"]
        return {"action": "chat", "args": {},
                "reply": (msg.get("content") or "").strip(),
                "engine": "llm"}
    except Exception as e:
        print("agent_brain: chat_only failed:", type(e).__name__, e, flush=True)
        return None


def decide(text: str, history=None, context=None):
    """One round trip: returns {action, args, reply, engine} or None.

    ``action`` is either ``"chat"`` or one of the TOOLS names.  Returning
    None means the LLM is unreachable and the caller should fall back to the
    keyword table.
    """
    try:
        j = _post(build_messages(text, history, context), tools=TOOLS)
    except Exception as e:
        print("agent_brain: LLM unavailable:", type(e).__name__, e, flush=True)
        return None

    try:
        msg = j["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return None

    calls = msg.get("tool_calls") or []
    if calls:
        fn = calls[0].get("function") or {}
        name = fn.get("name", "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        reply = (msg.get("content") or "").strip()
        return {
            "action": name,
            "args": args if isinstance(args, dict) else {},
            "reply": reply or ACTION_REPLY.get(name, "好的。"),
            "engine": "llm-tool",
        }

    reply = (msg.get("content") or "").strip()
    return {"action": "chat", "args": {}, "reply": reply, "engine": "llm"}


# ---------------------------------------------------------------------------
# Fact extraction (background, never on the spoken path)
# ---------------------------------------------------------------------------
_EXTRACT_PROMPT = (
    "从下面这句话里抽取关于说话人的长期事实，只抽会长期有效的：姓名、称呼、"
    "年龄、慢性病、用药、家人、住址、生活习惯、喜好。"
    "只输出 JSON 对象，键是 2-6 个字的中文标签，值是简短内容，例如"
    '{"称呼":"王阿姨","慢性病":"高血压"}。没有可记的就输出 {}。'
)


def extract_facts(text: str) -> dict:
    try:
        j = _post(
            [
                {"role": "system", "content": _EXTRACT_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0.0,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        content = j["choices"][0]["message"]["content"]
        data = json.loads(content)
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if v not in (None, "", [])}
    except Exception as e:
        print("agent_brain: fact extraction skipped:", type(e).__name__, e,
              flush=True)
        return {}


def remember_async(text: str) -> None:
    """Fire-and-forget: never block the reply on fact extraction."""
    if len(text or "") < 4:
        return

    def run():
        new = extract_facts(text)
        if new:
            save_facts(new)
            print("agent_brain: learned", new, flush=True)

    threading.Thread(target=run, daemon=True).start()
