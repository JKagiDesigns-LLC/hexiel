/* hexiel web UI front-end: WS chat, approvals, todo/memory/skills panels. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);
const esc = (s) => (s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;");

let ws = null;
let busy = false;
let live = null;            // {flow, body, thinking} during a turn
let wsQueue = [];

// ---------------------------------------------------------------- rendering

function newLive() {
  const wrap = document.createElement("div");
  wrap.className = "msg assistant";
  // text segments and tool cards are appended to one flow in the order they
  // happen, so each step's reply sits below the tool calls that preceded it
  wrap.innerHTML = `<div class="bubble"><div class="flow"></div><div class="thinking">thinking…</div></div>`;
  $("#chat").appendChild(wrap);
  const live = {
    wrap,
    flow: wrap.querySelector(".flow"),
    thinking: wrap.querySelector(".thinking"),
    body: null,          // current text segment (null = start a new one)
    text: "",
    byId: new Map(),
  };
  $("#chat").scrollTop = $("#chat").scrollHeight;
  return live;
}

function appendText(delta) {
  if (!live) live = newLive();
  if (!live.body) {
    live.body = document.createElement("div");
    live.body.className = "body";
    live.flow.appendChild(live.body);
    live.text = "";
  }
  live.text += delta;
  live.body.innerHTML = renderMarkdown(live.text);
  $("#chat").scrollTop = $("#chat").scrollHeight;
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
  live.body = null;          // next text starts a new segment below this tool
  live.flow.appendChild(el);
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
    if (live.body && live.text.trim()) { live.body.innerHTML = renderMarkdown(live.text.trim()); }
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
      case "text": appendText(data.delta); break;
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

// ---------------------------------------------------------------- settings

// Ask "this session or permanent?"; resolves to "session" | "permanent" | null.
function askScope(title) {
  return new Promise(resolve => {
    $("#scope-title").textContent = title;
    $("#scope-back").classList.add("show");
    const done = (scope) => {
      $("#scope-back").classList.remove("show");
      ["#scope-session", "#scope-permanent", "#scope-cancel"].forEach(id => $(id).onclick = null);
      document.removeEventListener("keydown", onKey, true);
      resolve(scope);
    };
    const onKey = (ev) => {
      if (ev.key === "Escape") { ev.stopPropagation(); done(null); }
    };
    document.addEventListener("keydown", onKey, true);
    $("#scope-session").onclick = () => done("session");
    $("#scope-permanent").onclick = () => done("permanent");
    $("#scope-cancel").onclick = () => done(null);
  });
}

async function changeSetting(key, label, value) {
  const scope = await askScope(`${label} → ${value}`);
  if (!scope) { loadSettings(); return; }
  const resp = await (await fetch("/api/config", { method: "POST",
    body: JSON.stringify({ key, value, scope }), headers: { "content-type": "application/json" } })).json();
  $("#settings-msg").textContent = resp.error ? "⚠ " + resp.error : "✓ " + resp.message;
  if (resp.rows) renderSettings(resp.rows);
  refreshState();
}

function renderSettings(rows) {
  const fmt = v => (v === null || v === undefined) ? "" : String(v);
  $("#settings-table tbody").innerHTML = rows.map((r, i) => {
    const opts = r.options;
    let cell;
    if (opts === null || opts === undefined) {
      cell = `<input data-i="${i}" value="${esc(fmt(r.value))}" placeholder="(empty)">`;
    } else {
      const all = opts.map(fmt).includes(fmt(r.value)) ? opts : [r.value, ...opts];
      cell = `<select data-i="${i}">` + all.map(o =>
        `<option value="${esc(fmt(o))}"${fmt(o) === fmt(r.value) ? " selected" : ""}>${esc(fmt(o))}</option>`).join("") + "</select>";
    }
    const badge = r.session_only ? `<span class="badge session">this session</span>` : `<span class="badge saved">saved</span>`;
    return `<tr title="${esc(r.help)}"><td class="label">${esc(r.label)}</td><td>${cell}</td>` +
           `<td class="saved">${esc(fmt(r.saved))}</td><td>${badge}</td></tr>`;
  }).join("");
  $$("#settings-table select, #settings-table input").forEach(el => {
    const r = rows[+el.dataset.i];
    const fire = () => { if (el.value !== fmt(r.value)) changeSetting(r.key, r.label, el.value); };
    if (el.tagName === "SELECT") el.addEventListener("change", fire);
    else el.addEventListener("keydown", ev => { if (ev.key === "Enter") fire(); });
  });
}

async function loadSettings() {
  const data = await (await fetch("/api/config")).json();
  renderSettings(data.rows || []);
}

$("#settings-btn").addEventListener("click", () => {
  $("#settings-msg").textContent = "";
  $("#settings-back").classList.add("show");
  loadSettings();
});
$("#settings-close").addEventListener("click", () => $("#settings-back").classList.remove("show"));
document.addEventListener("keydown", ev => {
  if (ev.key === "Escape" && !$("#scope-back").classList.contains("show")) $("#settings-back").classList.remove("show");
});

$("#model-select").addEventListener("change", async (ev) => {
  const name = ev.target.value;
  const scope = await askScope(`Switch model to ${name}`);
  if (scope) {
    await fetch("/api/config", { method: "POST", body: JSON.stringify({ key: "profile", value: name, scope }),
                                 headers: { "content-type": "application/json" } });
  }
  refreshState();   // also snaps the dropdown back when cancelled
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
// deep link: /#settings opens the settings panel (bookmarks, screenshots)
if (location.hash === "#settings") $("#settings-btn").click();
