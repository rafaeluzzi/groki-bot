"""Zero-dependency, self-refreshing page for ``GET /debug/panel``.

Single file with inline HTML/CSS/JS; fetches ``/debug/status`` every 3 s and redraws.
Block colours: green = healthy, red = down, yellow = reconnecting (process up, session not connected). The page text is Chinese.
"""

from __future__ import annotations

PANEL_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>StackChan 状态面板</title>
<style>
  body { font-family: -apple-system, "PingFang SC", sans-serif; margin: 16px;
         background: #111; color: #eee; }
  h1 { font-size: 18px; margin: 0 0 12px; }
  .blocks { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
  .block { flex: 1; min-width: 180px; border-radius: 10px; padding: 12px 14px;
           color: #fff; }
  .block .name { font-size: 13px; opacity: .85; }
  .block .value { font-size: 22px; font-weight: 700; margin-top: 4px; }
  .block .sub { font-size: 12px; opacity: .8; margin-top: 4px; }
  .ok   { background: #1d7a3e; }
  .err  { background: #a02c2c; }
  .warn { background: #a07a1d; color: #111; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 16px;
          font-size: 13px; }
  th, td { border: 1px solid #333; padding: 6px 8px; text-align: left; }
  th { background: #1c1c1c; }
  td.ok-cell { color: #5fd38a; }
  td.err-cell { color: #ff7b7b; }
  .stale { color: #ff7b7b; font-weight: 700; }
  #updated { font-size: 12px; color: #888; margin-bottom: 12px; }
</style>
</head>
<body>
<h1>StackChan 状态面板</h1>
<div id="updated">加载中……</div>
<div class="blocks">
  <div class="block" id="blk-device"><div class="name">设备（ESP32）</div>
    <div class="value">—</div><div class="sub"></div></div>
  <div class="block" id="blk-gemini"><div class="name">Gemini 会话</div>
    <div class="value">—</div><div class="sub"></div></div>
  <div class="block" id="blk-wake"><div class="name">唤醒闸门</div>
    <div class="value">—</div><div class="sub"></div></div>
  <div class="block" id="blk-audio"><div class="name">音频</div>
    <div class="value">—</div><div class="sub"></div></div>
</div>
<table id="tbl-counters">
  <tr><th>计数</th><th>值</th><th>计数</th><th>值</th></tr>
</table>
<h1>最近工具调用</h1>
<table id="tbl-tools"><tr><th>时间</th><th>工具</th><th>结果</th></tr></table>
<h1>最近 transcript</h1>
<table id="tbl-transcripts"><tr><th>时间</th><th>内容</th></tr></table>
<script>
function fmtTs(ts) {
  if (ts === null || ts === undefined) return "—";
  const d = new Date(ts * 1000);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return hh + ":" + mm + ":" + ss;
}
function fmtAgo(ts, now) {
  if (ts === null || ts === undefined) return "—";
  const s = Math.max(0, Math.round(now - ts));
  if (s < 60) return s + " 秒前";
  if (s < 3600) return Math.floor(s / 60) + " 分钟前";
  return Math.floor(s / 3600) + " 小时前";
}
function fmtNum(v) {
  return v === null || v === undefined ? "—" : String(v);
}
function fmtTokenUsage(u) {
  if (!u) return "—";
  const parts = [
    "total=" + fmtNum(u.total_token_count),
    "prompt=" + fmtNum(u.prompt_token_count),
    "response=" + fmtNum(u.response_token_count),
    "tool=" + fmtNum(u.tool_use_prompt_token_count),
  ];
  if (u.updated_at) parts.push("更新时间=" + fmtTs(u.updated_at));
  return parts.join("；");
}
function setBlock(id, cls, value, sub) {
  const el = document.getElementById(id);
  el.className = "block " + cls;
  el.querySelector(".value").textContent = value;
  el.querySelector(".sub").textContent = sub || "";
}
function esc(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}
function render(st) {
  const now = st.generated_at;
  document.getElementById("updated").textContent =
    "最近刷新：" + fmtTs(now) + "（每 3 秒自动刷新）";

  const dev = st.device;
  setBlock("blk-device",
    dev.connected ? "ok" : "err",
    dev.connected ? "已连接" : "断开",
    dev.connected
      ? (dev.device_id || "") + " 自 " + fmtTs(dev.connected_since)
      : "最近断开：" + fmtTs(dev.last_disconnect_at));

  const g = st.gemini;
  let gCls, gVal;
  if (g.connected) { gCls = "ok"; gVal = "已连接"; }
  else if (g.running) { gCls = "warn"; gVal = "重连中"; }
  else { gCls = "err"; gVal = "断开"; }
  setBlock("blk-gemini", gCls, gVal,
    "会话 #" + g.session_count +
    (g.last_error ? "；最近错误 " + fmtTs(g.last_error.at) : ""));

  const w = st.wake_gate;
  setBlock("blk-wake",
    w.available ? "ok" : "err",
    w.available ? w.state : "不可用",
    w.last_wake_at ? "最近唤醒：" + fmtTs(w.last_wake_at) : "尚未唤醒");

  const a = st.audio;
  const audioStale = a.last_device_audio_at !== null &&
    (now - a.last_device_audio_at) > 10;
  setBlock("blk-audio",
    a.last_device_audio_at === null ? "err" : (audioStale ? "warn" : "ok"),
    a.tts_active ? "TTS 播放中" : "空闲",
    "设备上行音频：" + fmtAgo(a.last_device_audio_at, now));

  const counters = [
    ["设备断连累计", st.device.disconnect_count],
    ["1008 重连累计", g.reconnect_1008_count],
    ["活跃掉线累计", g.active_drops],
    ["最近活跃掉线", fmtTs(g.last_active_drop_at)],
    ["唤醒累计", w.wake_count],
    ["关窗累计", w.close_count],
    ["保活发送累计", g.keepalive_count],
    ["最近保活", fmtTs(g.last_keepalive_at)],
    ["保活状态", g.keepalive_running ? "运行中" : (g.keepalive_disabled_reason || "未运行")],
    ["最近保活跳过", g.last_keepalive_skip_reason || "—"],
    ["Token 用量", fmtTokenUsage(g.token_usage)],
    ["最近错误", g.last_error ? g.last_error.message : "—"],
  ];
  let html = "<tr><th>计数</th><th>值</th><th>计数</th><th>值</th></tr>";
  for (let i = 0; i < counters.length; i += 2) {
    const a1 = counters[i], a2 = counters[i + 1];
    html += "<tr><td>" + esc(String(a1[0])) + "</td><td>" +
      esc(String(a1[1])) + "</td>";
    html += a2
      ? "<td>" + esc(String(a2[0])) + "</td><td>" + esc(String(a2[1])) + "</td></tr>"
      : "<td></td><td></td></tr>";
  }
  document.getElementById("tbl-counters").innerHTML = html;

  let tools = "<tr><th>时间</th><th>工具</th><th>结果</th></tr>";
  for (const t of st.recent.tool_calls) {
    tools += "<tr><td>" + fmtTs(t.at) + "</td><td>" + esc(t.name) + "</td>" +
      (t.ok ? '<td class="ok-cell">ok</td>'
            : '<td class="err-cell">' + esc(t.error || "error") + "</td>") +
      "</tr>";
  }
  document.getElementById("tbl-tools").innerHTML = tools;

  let trs = "<tr><th>时间</th><th>chars</th></tr>";
  for (const t of st.recent.transcripts) {
    trs += "<tr><td>" + fmtTs(t.at) + "</td><td>" + esc(String(t.chars ?? "")) + "</td></tr>";
  }
  document.getElementById("tbl-transcripts").innerHTML = trs;
}
async function tick() {
  try {
    const resp = await fetch("/debug/status", { cache: "no-store" });
    render(await resp.json());
  } catch (e) {
    document.getElementById("updated").innerHTML =
      '<span class="stale">网关无响应：' + esc(String(e)) + "</span>";
  }
}
tick();
setInterval(tick, 3000);
</script>
</body>
</html>
"""
