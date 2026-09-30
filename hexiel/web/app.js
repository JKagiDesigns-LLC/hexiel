/* hexiel web UI front-end: WS chat, approvals, todo/memory/skills panels. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => (s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;");

let ws = null;
let busy = false;
let live = null;            // {bubble, tools, thinking} during a turn
let wsQueue = [];

// ---------------------------------------------------------------- rendering

function newLive() {
  const wrap = document.createElement("div");
  wrap.className = "msg assistant";
  wrap.innerHTML = `<div class="bubble"><div class="thinking">thinking…</div><div class="body"></div><div class="tools"></div></div>`;
  $("#chat").appendChild(wrap);
  const live = {
    wrap,
    bubble: wrap.querySelector(".body"),
    thinking: wrap.querySelector(".thinking"),
    tools: wrap.querySelector(".tools"),
    text: "",
    byId: new Map(),
  };
  live.bubble.dataset.lang = "";
  $("#chat").scrollTop = $("#chat").scrollHeight;
  return live;
}

function addTool(toolMsg) {
  if (!live) live = newLive();
  const el = document.createElement("div");
  el.className = "tool running";
  el.innerHTML = `<div class="head">${esc(toolMsg.name)} — ${esc(toolMsg.short)}<span class="chev">▸</span></div><div class="preview">…</div>`;
  el.addEventListener("click", () => {
    el.classList.toggle("open");
    el.querySelector(".chev").textContent = el.classList.contains("open") ? "▾" : "▸";
  });
  live.tools.appendChild(el);
  live.byId.set(toolMsg.id, el);
  $("#chat").scrollTop = $("#chat").scrollHeight;
  return el;
}

function toolDone(data) {
  if (!live) return;
  const el = live.byId.get(data.id);
  if (!el) return;
  el.classList.remove("running");
  el.classList.add(data.ok ? "done-ok" : "done-err");
  const head = el.querySelector(".head");
  const name = head.textContent.split(" — ")[0];
  head.innerHTML = `<span style="opacity:.8">${data.ok ? "✓" : "✗"}</span> ${esc(name)} — ${esc(head.textContent.split(" — ").slice(1).join(" — "))}` +
                   `<span style="opacity:.6"> ${data.elapsed}s</span><span class="chev">▸</span>`;
  el.querySelector(".preview").textContent = data.preview || "(no output)";
}

function renderHistory(items) {
  $("#chat").innerHTML = "";
  for (const item of items) {
    if (item.t === "user") {
      const div = document.createElement("div");
      div.className = "msg user";
      div.innerHTML = `<div class="bubble">${esc(item.text)}</div>`;
      $("#chat").appendChild(div);
    } else if (item.t === "assistant") {
      const div = document.createElement("div");
      div.className = "msg assistant";
      div.innerHTML = `<div class="bubble"><div class="body"></div></div>`;
      div.querySelector(".body").innerHTML = renderMarkdown(item.text);
      $("#chat").appendChild(div);
    } else if (item.t === "tool") {
      // history tool cards render standalone: no thinking label, no live group
      if (!live) live = newLive();
      live.thinking.style.display = "none";
      addTool(item);
      if (item.ok !== undefined) toolDone({ id: item.id, ok: item.ok, preview: item.preview, elapsed: 0 });
      const wrap = live.wrap;
      live = null;
      wrap.querySelectorAll(".thinking").forEach(el => el.remove());
      $("#chat").appendChild(wrap);
    } else if (item.t === "note") {
      const div = document.createElement("div");
      div.className = "note";
      div.textContent = item.text;
      $("#chat").appendChild(div);
    }
  }
  live = null;
  $("#chat").scrollTop = $("#chat").scrollHeight;
}

function finishTurn(data) {
  if (live) {
    const text = live.bubble.innerHTML;
    if (live.text.trim()) { live.bubble.innerHTML = renderMarkdown(live.text.trim()); }
    live.thinking.remove();
    // collapse finished tools' previews
    live.wrap.querySelectorAll(".tool.done-ok .preview, .tool.done-err .preview")
        .forEach(p => p.parentElement.classList.remove("open"));
    live = null;
  }
  busy = false;
  updateSendButton();
  if (data && data.error) {
    const div = document.createElement("div");
    div.className = "note incident";
    div.textContent = "error: " + data.error;
    $("#chat").appendChild(div);
  }
  if (data && data.messages) renderHistory(data.messages);
  refreshState();
}

function updateSendButton() {
  const btn = $("#send");
  btn.disabled = false;
  if (busy) { btn.textContent = "Stop"; btn.classList.add("stop"); }
  else { btn.textContent = "Send"; btn.classList.remove("stop"); }
  $("#ctx-label").textContent = lastStatus?.model_id || "";
}

// ---------------------------------------------------------------- websocket

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { wsQueue.splice(0).forEach(ws.send); };
  ws.onmessage = (ev) => {
    const data = JSON.parse(ev.data);
    switch (data.t) {
      case "history":
        renderHistory(data.items);
        applyStatus(data.status);
        break;
      case "status": applyStatus(data.status); break;
      case "text":
        if (!live) live = newLive();
        live.text += data.delta;
        live.bubble.innerHTML = renderMarkdown(live.text) ;
        $("#chat").scrollTop = $("#chat").scrollHeight;
        break;
      case "thinking":
        if (!live) live = newLive();
        live.thinking.textContent = "thinking" + "…";
        break;
      case "tool": addTool(data); break;
      case "tool_done": toolDone(data); break;
      case "note": {
        const div = document.createElement("div");
        div.className = "note";
        div.textContent = data.text;
        $("#chat").appendChild(div);
        break;
      }
      case "incident": {
        const div = document.createElement("div");
        div.className = "note incident";
        div.textContent = "⚠ hexiel recorded an incident (run /heal): " + data.text;
        $("#chat").appendChild(div);
        break;
      }
      case "turn_end": finishTurn(data); break;
      case "approval": showApproval(data); break;
      case "approval_done": hideApproval(data); break;
    }
  };
  ws.onclose = () => { setTimeout(connect, 1500); };
}

function sendWs(obj) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
  else wsQueue.push(JSON.stringify(obj));
}

// ---------------------------------------------------------------- approvals

function showApproval(data) {
  $("#approve-tool").textContent = data.tool;
  $("#approve-args").textContent = data.args;
  $("#modal-back").classList.add("show");
  $("#modal-back").dataset.id = data.id;
  $("#approve-always").style.display = "inline-block";
}

function verdict(v) {
  sendWs({ t: "approve", id: Number($("#modal-back").dataset.id), verdict: v });
}
$("#approve-yes").addEventListener("click", () => verdict("yes"));
$("#approve-always").addEventListener("click", () => verdict("always"));
$("#approve-no").addEventListener("click", () => verdict("no"));
document.addEventListener("keydown", (ev) => {
  if ($("#modal-back").classList.contains("show")) {
    if (ev.key === "y") verdict("yes");
    if (ev.key === "n") verdict("no");
    if (ev.key === "Escape") verdict("no");
  }
});

function hideApproval(data) {
  if (String($("#modal-back").dataset.id) === String(data.id)) {
    $("#modal-back").classList.remove("show");
  }
}

// ---------------------------------------------------------------- state/panels

let lastStatus = null;
async function refreshState() {
  try {
    const state = await (await fetch("/api/state")).json();
    document.lastApiState = state;
    applyStatus(state.status);
    const select = $("#model-select");
    if (select.options.length !== state.models.length) {
      select.innerHTML = state.models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
    }
    if (state.current_profile) select.value = state.current_profile;
    renderTodo(state.todo || []);
    renderSidebar(state);
  } catch { /* server restarting */ }
}

function applyStatus(status) {
  if (!status) return;
  lastStatus = status;
  busy = status.busy;
  const c = status.context;
  const label = `${status.model_id} · ctx ${(Math.round(c.pct))}% of ${Math.round(c.window / 1000)}k` +
                (c.compact_at_pct ? ` (compact @${c.compact_at_pct}%)` : "");
  $("#ctx-label").textContent = label;
  $("#todo-badge").textContent = status.todo || "";
  updateSendButton();
}

function renderTodo(items) {
  if (!items.length) { $("#todo-panel").innerHTML = "<em class='dim'>no todo list — hexiel creates one automatically for multi-step tasks</em>"; return; }
  $("#todo-panel").innerHTML = items.map(it => {
    const cls = it.state === "x" ? "done" : it.state === "~" ? "doing" : "queued";
    const mark = it.state === "x" ? "☑" : it.state === "~" ? "◐" : "☐";
    return `<div class="task ${cls}">${mark} ${esc(it.text)}</div>`;
  }).join("");
}

function renderSidebar(state) {
  $("#mem-summary").textContent = state.memory_index || "no memories yet";
  $("#skills-list").innerHTML = (state.skills || []).map(s =>
    `<div class="side-item" title="${esc(s.description)}">⚡ ${esc(s.name)}</div>`).join("")
    || "<em class='dim'>none</em>";
  $("#sessions-list").innerHTML = (state.sessions || []).slice(1).map(s =>
    `<div class="side-item" title="resume id ${esc(s.id)}">${esc(s.preview || s.id)}</div>`).join("");
  const chain = state.toolchain || {};
  $("#toolchain-list").innerHTML =
    `<div class="side-item">${chain.container ? "📦 " + esc(chain.container) : "—"}` +
    (chain.test_runners && chain.test_runners.length ? " · ♪ " + esc(chain.test_runners.join(", ")) : "") + "</div>";
}

$("#model-select").addEventListener("change", async (ev) => {
  await fetch("/api/model", { method: "POST", body: JSON.stringify({ name: ev.target.value }),
                              headers: { "content-type": "application/json" } });
  refreshState();
});
$("#clear-btn").addEventListener("click", async () => {
  if (!confirm("Clear this conversation?")) return;
  await fetch("/api/clear", { method: "POST" });
  $("#chat").innerHTML = "";
  refreshState();
});
$("#compact-btn").addEventListener("click", async () => {
  await fetch("/api/compact", { method: "POST" });
  const div = document.createElement("div");
  div.className = "note"; div.textContent = "compacting…";
  $("#chat").appendChild(div);
});

// composer
const input = $("#input");
input.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); submit(); }
});
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 200) + "px";
});
function submit() {
  if (busy) { sendWs({ t: "stop" }); return; }
  const text = input.value.trim();
  if (!text) return;
  input.value = ""; input.style.height = "auto";
  const div = document.createElement("div");
  div.className = "msg user";
  div.innerHTML = `<div class="bubble">${esc(text)}</div>`;
  $("#chat").appendChild(div);
  busy = true; updateSendButton();
  sendWs({ t: "send", text });
}
$("#send").addEventListener("click", submit);

connect();
refreshState();
setInterval(refreshState, 20000);