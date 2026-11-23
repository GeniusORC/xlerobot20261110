#!/usr/bin/env python3
"""The control panel served at /control on 8082.

Kept as a plain string so it can be edited without touching the voice loop.
Everything it does goes through the /api/robot proxy in voice_auto_web.py,
which forwards to robot_api (8090); the browser therefore only ever talks to
one origin.
"""

HTML = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>XLeRobot 控制台</title>
<style>
:root{--bg:#111;--card:#1d1d1f;--line:#2c2c30;--fg:#eee;--dim:#9a9aa2;
      --ok:#3c9;--warn:#e8a13a;--bad:#d33;--accent:#2b5f9e}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,sans-serif;
     margin:0;padding:16px;max-width:1180px;margin-inline:auto}
h1{font-size:21px;margin:0 0 4px;display:flex;align-items:center;gap:10px}
h1 a{font-size:13px;font-weight:400;color:#7ab;text-decoration:none}
h2{font-size:15px;margin:0 0 10px;color:#cfcfd6;font-weight:600}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.wide{grid-column:1/-1}
button{font-size:15px;padding:11px 14px;margin:3px;border:0;border-radius:9px;
       cursor:pointer;background:#2f2f34;color:var(--fg)}
button:hover{background:#3a3a41}
button:disabled{opacity:.45;cursor:default}
button.stop{background:var(--bad);color:#fff;font-size:18px;padding:16px 22px;font-weight:600}
button.reset{background:#7a4a12;color:#fff}
button.go{background:var(--accent);color:#fff}
button.on{background:var(--ok);color:#04231a;font-weight:600}
.pad{display:grid;grid-template-columns:repeat(3,64px);grid-template-rows:repeat(3,58px);
     gap:6px;justify-content:center;margin:8px auto}
.pad button{width:100%;height:100%;margin:0;font-size:20px;padding:0}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px}
input,select{font-size:15px;padding:9px 10px;border-radius:8px;border:1px solid var(--line);
             background:#131315;color:var(--fg);min-width:0}
input[type=number]{width:96px}
label{font-size:13px;color:var(--dim)}
#status{font-size:12.5px;color:var(--dim);margin-top:10px;line-height:1.7;word-break:break-word}
.pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;
      background:#2a2a2e;color:var(--dim);margin-right:6px}
.pill.ok{background:#123c2c;color:#6ed6a8}
.pill.bad{background:#3d1618;color:#f09}
.pill.warn{background:#3d2f12;color:#f2c069}
img{width:100%;background:#000;border-radius:9px;display:block;margin-top:8px}
.camwrap{display:none}.camwrap.on{display:block}
#toast{position:fixed;left:50%;transform:translateX(-50%);bottom:18px;max-width:90vw;
       padding:11px 16px;border-radius:10px;background:#333;color:#fff;font-size:14px;
       opacity:0;transition:opacity .2s;pointer-events:none;z-index:9}
#toast.show{opacity:1}
#toast.err{background:#7a1f22}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:500;font-size:12px}
tr.mrow{cursor:pointer}
tr.mrow:hover td{background:#26262b}
tr.sel td{background:#233247}
.mono{font-family:ui-monospace,Menlo,monospace;font-size:13px}
.hint{font-size:12px;color:var(--dim);line-height:1.6;margin:8px 0 0}
.danger{border:1px solid #6b2a2c;background:#221517;border-radius:10px;padding:12px;margin-top:12px}
.danger h3{margin:0 0 8px;font-size:13.5px;color:#f09}
@media (max-width:760px){.grid{grid-template-columns:1fr}}
</style></head><body>

<h1>XLeRobot 控制台 <a href="/">← 回到陪伴页面</a></h1>
<div id="status">连接中...</div>
<div id="health" style="font-size:12px;color:var(--dim);margin-top:6px"></div>
<div id="llmStatus" style="font-size:12px;color:var(--dim);margin-top:4px"></div>
<div id="trace" style="font-size:12px;color:var(--dim);margin-top:4px"></div>

<div class="grid" style="margin-top:12px">

  <div class="card">
    <h2>安全</h2>
    <div class="row">
      <button class="stop" onclick="estop()">停止</button>
      <button class="reset" onclick="resetRobot()">重置 XLeRobot</button>
    </div>
    <p class="hint">停止 = 立即停底盘并关闭跟随。重置 = 停止 + 关闭跟随 + 头部回到平视位置。</p>
  </div>

  <div class="card">
    <h2>跟随</h2>
    <div class="row">
      <button id="followBtn" onclick="toggleFollow()">跟随：关</button>
      <span class="hint" id="followHint" style="margin:0"></span>
    </div>
    <p class="hint">跟随由 head_track.py 驱动；这里只切换机器人的跟随许可（latch）。</p>
  </div>

  <div class="card">
    <h2>移动</h2>
    <div class="row">
      <label>距离 <select id="stepM">
        <option value="0.1">0.1 m</option>
        <option value="0.2" selected>0.2 m</option>
        <option value="0.5">0.5 m</option>
      </select></label>
      <label>角度 <select id="stepDeg">
        <option value="15" selected>15°</option>
        <option value="45">45°</option>
        <option value="90">90°</option>
      </select></label>
    </div>
    <div class="pad">
      <span></span><button onclick="move(1,0)">↑</button><span></span>
      <button onclick="move(0,1)">↰</button>
      <button onclick="move(-1,0)">↓</button>
      <button onclick="move(0,-1)">↱</button>
      <span></span><span></span><span></span>
    </div>
    <p class="hint">测距移动：前进/后退按所选距离，转向按所选角度（正 = 左转）。</p>
  </div>

  <div class="card">
    <h2>头部</h2>
    <div class="pad" style="grid-template-rows:repeat(2,58px)">
      <span></span><button onclick="head('dtilt',8)">▲</button><span></span>
      <button onclick="head('dpan',-8)">◀</button>
      <button onclick="head('dtilt',-8)">▼</button>
      <button onclick="head('dpan',8)">▶</button>
    </div>
    <div class="row" style="justify-content:center">
      <button onclick="resetRobot()">回到平视</button>
    </div>
    <p class="hint" id="headPos">pan -, tilt -</p>
  </div>

  <div class="card">
    <h2>机械臂</h2>
    <div class="row">
      <button onclick="gripper('left', true)">左夹爪 开</button>
      <button onclick="gripper('left', false)">左夹爪 关</button>
    </div>
    <div class="row">
      <button onclick="gripper('right', true)">右夹爪 开</button>
      <button onclick="gripper('right', false)">右夹爪 关</button>
    </div>
    <div class="row" style="margin-top:10px">
      <button class="go" onclick="snapshotPose()">记录当前姿态</button>
      <label>姿态 <select id="poseSelect">
        <option value="">-- 选择姿态 --</option>
      </select></label>
      <button class="go" onclick="replayPose()">执行</button>
    </div>
    <p class="hint" id="armHint">夹爪扭矩保持开启；停止键会中断当前轨迹。</p>
  </div>

  <div class="card wide">
    <h2>相机</h2>
    <div class="row">
      <button class="go" onclick="showCam('head')">头部</button>
      <button class="go" onclick="showCam('left')">左腕</button>
      <button class="go" onclick="showCam('right')">右腕</button>
      <button class="go" onclick="showCam('all')">三路</button>
    </div>
    <div id="wrap_head" class="camwrap"><img id="cam_head" alt="head"></div>
    <div id="wrap_left" class="camwrap"><img id="cam_left" alt="left"></div>
    <div id="wrap_right" class="camwrap"><img id="cam_right" alt="right"></div>
  </div>

  <div class="card wide">
    <h2>电机 / ID</h2>
    <div class="row">
      <button class="go" onclick="scan()">扫描总线</button>
      <label>总线 <select id="busSel" onchange="scan()">
        <option value="1" selected>bus1 · 左臂 1-6 + 头 7-8</option>
        <option value="2">bus2 · 右臂 1-6 + 轮 9-10</option>
      </select></label>
      <span class="hint" id="scanMsg" style="margin:0"></span>
    </div>
    <div id="motorTable" style="margin-top:10px"></div>
    <div id="motorDetail"></div>
  </div>

  <div class="card wide">
    <h2>日志查看器</h2>
    <p class="hint"><a href="http://127.0.0.1:8084/history" target="_blank" rel="noopener">打开步态历史趋势</a></p>
    <div class="row">
      <label>服务 <select id="logSelect" onchange="loadLog()">
        <option value="robot_api">robot_api</option>
        <option value="camera_web">camera_web</option>
        <option value="voice_auto">voice_auto</option>
        <option value="head_track">head_track</option>
        <option value="health_station">health_station</option>
        <option value="face_display">face_display</option>
        <option value="watchdog">watchdog</option>
      </select></label>
      <button class="go" onclick="loadLog()">刷新</button>
      <label><input type="checkbox" id="logAuto" onchange="toggleAutoLog()"> 自动刷新 (3s)</label>
    </div>
    <pre id="logContent" style="background:#0a0a0c;padding:10px;border-radius:8px;font-size:12px;
         line-height:1.5;overflow:auto;max-height:300px;margin-top:10px;font-family:ui-monospace,monospace"></pre>
  </div>

</div>
<div id="toast"></div>

<script>
const camBase = location.protocol + '//' + location.hostname + ':8080';
for (const n of ['head','left','right'])
  document.getElementById('cam_'+n).src = camBase + '/stream/' + n + '?ts=' + Date.now();
function showCam(name){
  for (const n of ['head','left','right'])
    document.getElementById('wrap_'+n).classList.toggle('on', name==='all' || n===name);
}
showCam('head');

let toastTimer = null;
function toast(msg, err){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'show' + (err ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.className = '', 2600);
}

async function j(method, path, body){
  try{
    const r = await fetch('/api/robot' + path, {
      method,
      headers: {'Content-Type':'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    let d = null;
    try { d = await r.json(); } catch(e){ d = {error: 'HTTP ' + r.status}; }
    if (!r.ok) toast((d && (d.error || d.message)) || ('HTTP ' + r.status), true);
    return d;
  }catch(e){ toast('网络错误: ' + e, true); return null; }
}

const $ = id => document.getElementById(id);
const f1 = v => (v === null || v === undefined) ? '—' : Number(v).toFixed(1);

let state = {follow:false, connected:false};

async function estop(){ await j('POST','/estop'); await poll(); toast('已停止'); }
async function resetRobot(){ await j('POST','/reset'); toast('正在重置'); setTimeout(poll, 2500); }

async function move(dir, turn){
  const m = parseFloat($('stepM').value), d = parseFloat($('stepDeg').value);
  const body = dir ? {forward_m: dir*m} : {turn_deg: turn*d};
  const r = await j('POST','/move', body);
  if (r && r.queued) toast('已排队: ' + (dir ? (dir>0?'前进 ':'后退 ') + m + ' m'
                                                   : (turn>0?'左转 ':'右转 ') + d + '°'));
  setTimeout(poll, 600);
}
async function head(k, v){ await j('POST','/head', {[k]: v}); setTimeout(poll, 250); }

async function toggleFollow(){
  const on = !state.follow;
  await j('POST','/follow', {on});
  await poll();
  toast(on ? '跟随许可已打开' : '跟随许可已关闭');
}

async function poll(){
  const s = await j('GET','/status');
  if (!s) return;
  state = s;
  const pills = [];
  pills.push('<span class="pill ' + (s.connected?'ok':'bad') + '">' +
             (s.connected?'已连接':'未连接') + '</span>');
  if (s.busy) pills.push('<span class="pill warn">忙</span>');
  if (s.follow) pills.push('<span class="pill ok">跟随中</span>');
  if (s.follow_client) pills.push('<span class="pill">跟随客户端在线</span>');
  pills.push('<span class="pill">' + (s.message || '') + '</span>');
  $('status').innerHTML = pills.join('');
  if (s.llm) {
    const label = !s.llm.configured ? 'LLM 未配置' :
      s.llm.ok === true ? `LLM ${s.llm.model} ${s.llm.latency_ms ?? '?'} ms` :
      s.llm.ok === false ? `LLM 离线: ${s.llm.error}` : `LLM ${s.llm.model} 尚未检测`;
    $('llmStatus').textContent = label + ' · ' + (s.trace?.engine || '尚无最近指令');
    $('trace').textContent = s.trace?.text ? `最近输入: ${s.trace.text} · ${s.trace.action || ''} · ${s.trace.elapsed_ms} ms` : '';
  }
  $('followBtn').textContent = '跟随：' + (s.follow ? '开' : '关');
  $('followBtn').classList.toggle('on', !!s.follow);
  $('followHint').textContent = s.follow_client ? 'head_track.py 正在发送心跳'
                                                : '没有客户端在驱动跟随';
  const h = await j('GET','/head');
  if (h && h.pan !== undefined)
    $('headPos').textContent = 'pan ' + f1(h.pan) + ', tilt ' + f1(h.tilt) +
                               '  (平视 ≈ ' + (-63.6) + ')';
}

async function pollHealth(){
  let h;
  try { h = await (await fetch('/api/health')).json(); } catch (_) { return; }
  if (!h) return;
  const dots = [];
  const names = {robot_api:'机器人',camera_web:'摄像头',voice_web:'语音',health_web:'健康',face_web:'表情'};
  for (const [k, label] of Object.entries(names)){
    const status = h[k];
    const cls = (status && status.ok) ? 'ok' : 'bad';
    dots.push('<span style="color:var(--' + cls + ')">●</span> ' + label);
  }
  $('health').innerHTML = dots.join(' ');
}

/* ---------------- motors ---------------- */
let selMotor = null, detailTimer = null;

async function scan(){
  const bus = $('busSel').value;
  $('scanMsg').textContent = '扫描中...';
  $('motorTable').innerHTML = '';
  const r = await j('GET','/motors');
  if (!r || !r.buses) { $('scanMsg').textContent = '扫描失败'; return; }
  const b = r.buses['bus' + bus];
  if (!b || b.error){ $('scanMsg').textContent = '扫描失败: ' + ((b && b.error) || '?'); return; }
  $('scanMsg').textContent = b.motors.length + ' 个电机响应' +
    (b.unknown.length ? '，未配置 ID: ' + b.unknown.join(',') : '') +
    (b.missing.length ? '，缺失 ID: ' + b.missing.join(',') : '');
  const rows = b.motors.map(m =>
    '<tr class="mrow" onclick="pick(' + bus + ',' + m.id + ')">' +
    '<td class="mono">' + m.id + '</td>' +
    '<td>' + (m.name || '<span style="color:#e8a13a">未配置</span>') + '</td>' +
    '<td class="mono">' + m.model + '</td></tr>').join('');
  $('motorTable').innerHTML =
    '<table><tr><th>ID</th><th>名称</th><th>型号</th></tr>' + rows + '</table>';
}

function pick(bus, id){
  selMotor = {bus, id};
  document.querySelectorAll('tr.mrow').forEach(tr => tr.classList.remove('sel'));
  [...document.querySelectorAll('tr.mrow')].forEach(tr => {
    if (Number(tr.children[0].textContent) === id) tr.classList.add('sel');
  });
  loadDetail();
  clearInterval(detailTimer);
  detailTimer = setInterval(loadDetail, 1500);
}

async function loadDetail(){
  if (!selMotor) return;
  const {bus, id} = selMotor;
  const d = await j('GET','/motor/' + bus + '/' + id);
  if (!d || d.error) return;
  const torque = d.Torque_Enable === 1;
  $('motorDetail').innerHTML = `
    <div class="card" style="margin-top:12px;background:#17171a">
      <h2>电机 ${id} · ${d.name || '未配置'} <span class="pill">bus${bus}</span></h2>
      <div class="row" style="gap:18px">
        <span>位置 <b class="mono">${d.Present_Position ?? '—'}</b></span>
        <span>目标 <b class="mono">${d.Goal_Position ?? '—'}</b></span>
        <span>负载 <b class="mono">${d.load_pct ?? '—'}%</b></span>
        <span>电压 <b class="mono">${d.voltage_v ?? '—'} V</b></span>
        <span>温度 <b class="mono">${d.Present_Temperature ?? '—'}°C</b></span>
        <span>${d.Moving ? '运动中' : '静止'}</span>
      </div>
      <div class="row">
        <button onclick="jog(-100)">-100</button>
        <button onclick="jog(-20)">-20</button>
        <button onclick="jog(20)">+20</button>
        <button onclick="jog(100)">+100</button>
        <span class="hint" style="margin:0">原始计数，4096 ≈ 360°</span>
      </div>
      <div class="row">
        <button onclick="setTorque(${!torque})">${torque ? '关闭扭矩' : '打开扭矩'}</button>
        <label>目标位置 <input id="goalIn" type="number" min="0" max="4095" value="${d.Goal_Position ?? 2048}"></label>
        <button onclick="setGoal()">应用</button>
      </div>
      <div class="danger">
        <h3>改 ID（危险）</h3>
        <div class="row">
          <label>新 ID <input id="newId" type="number" min="1" max="253" value="${id}"></label>
          <button onclick="changeId()">写入新 ID</button>
        </div>
        <p class="hint">改 ID 会立即改变电机地址，且只改运行中的配置：必须同步修改
        config_xlerobot_2wheels.py 并重启 robot_api，否则下次启动会找不到这个电机。</p>
      </div>
    </div>`;
}

async function jog(delta){
  if (!selMotor) return;
  const r = await j('POST','/motor', {bus:selMotor.bus, id:selMotor.id, jog:delta});
  if (r && r.ok) toast(r.did.join(', '));
  loadDetail();
}
async function setGoal(){
  if (!selMotor) return;
  const g = Number($('goalIn').value);
  const r = await j('POST','/motor', {bus:selMotor.bus, id:selMotor.id, goal:g});
  if (r && r.ok) toast('目标 ' + g);
  loadDetail();
}
async function setTorque(on){
  if (!selMotor) return;
  const r = await j('POST','/motor', {bus:selMotor.bus, id:selMotor.id, torque:on});
  if (r && r.ok) toast(on ? '扭矩已打开' : '扭矩已关闭');
  loadDetail();
}
async function changeId(){
  if (!selMotor) return;
  const n = Number($('newId').value);
  if (n === selMotor.id) { toast('ID 没有变化'); return; }
  if (!confirm('确定把 bus' + selMotor.bus + ' 上 ID ' + selMotor.id +
               ' 改成 ' + n + '？\n\n这会改变电机地址，必须同步修改配置文件。'))
    return;
  const r = await j('POST','/motor', {bus:selMotor.bus, id:selMotor.id, set_id:n});
  if (r && r.ok){
    toast('ID 已改为 ' + n);
    if (r.warning) alert(r.warning);
    selMotor.id = n;
    scan();
  }
}

/* ---------------- arms ---------------- */

async function gripper(arm, open){
  if (!confirm(`${arm === 'left' ? '左' : '右'}夹爪将${open ? '打开' : '关闭'}。确认周围安全？`)) return;
  const r = await j('POST','/arm/gripper', {arm, open});
  if (r && r.ok) toast(`${arm === 'left' ? '左' : '右'}夹爪已${open ? '打开' : '关闭'}`);
}

async function snapshotPose(){
  const r = await j('GET','/arm/state');
  if (!r || r.error) { toast('读取姿态失败', true); return; }
  const name = prompt('姿态名称 (仅字母、数字、_、-):');
  if (!name) return;
  const saved = await j('POST', '/arm/poses/' + encodeURIComponent(name), {});
  if (!saved?.ok) { toast(saved?.error || '保存姿态失败', true); return; }
  loadPoses();
  toast('已保存姿态: ' + name);
}

async function replayPose(){
  const sel = $('poseSelect').value;
  if (!sel) { toast('请选择一个姿态', true); return; }
  if (!confirm(`将执行姿态 ${sel}。请确认机械臂周围无人且运行区域安全。`)) return;
  const r = await j('POST', '/arm/poses/' + encodeURIComponent(sel) + '/replay', {});
  if (!r?.ok) { toast(r?.error || '姿态执行失败', true); return; }
  toast('姿态已执行: ' + sel);
}

function loadPoses(){
  j('GET', '/arm/poses').then(r => {
    const sel = $('poseSelect');
    sel.innerHTML = '<option value="">-- 选择姿态 --</option>' +
      (r?.poses || []).map(k => `<option value="${k}">${k}</option>`).join('');
  });
}

/* ---------------- logs ---------------- */

let logTimer = null;

async function loadLog(){
  const name = $('logSelect').value;
  const r = await fetch('/api/logs/' + name + '?lines=50');
  if (!r.ok) { $('logContent').textContent = '加载失败'; return; }
  const data = await r.json();
  if (!data.exists) {
    $('logContent').textContent = '日志文件不存在';
    return;
  }
  const lines = data.lines || [];
  $('logContent').textContent = lines.join('');
  $('logContent').scrollTop = $('logContent').scrollHeight;
}

function toggleAutoLog(){
  if ($('logAuto').checked){
    loadLog();
    logTimer = setInterval(loadLog, 3000);
  } else {
    clearInterval(logTimer);
  }
}

poll();
setInterval(poll, 2000);
pollHealth();
setInterval(pollHealth, 5000);
loadPoses();
document.addEventListener('visibilitychange', () => {
  if (document.hidden) clearInterval(detailTimer);
  else if (selMotor) { clearInterval(detailTimer); detailTimer = setInterval(loadDetail, 1500); }
});
</script></body></html>
"""
