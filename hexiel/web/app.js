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

// ---------------------------------------------------------------- images and PDFs
const mediaUrl = (m) => `api/media/${encodeURIComponent(m.id)}`;
function mediaCard(m) {
  const card = document.createElement("div");
  card.className = "media " + m.kind;
  if (m.kind === "image") {
    const img = document.createElement("img");
    img.src = mediaUrl(m); img.alt = m.name; img.loading = "lazy";
    img.addEventListener("click", () => openViewer(m));
    card.append(img);
    const cap = document.createElement("div");
    cap.className = "cap"; cap.textContent = m.name + (m.w ? ` · ${m.w}×${m.h}` : "");
    card.append(cap);
  } else {
    card.innerHTML = `<div class="pdfcard"><span class="ico">📄</span><span><b></b><br><small></small></span></div>`;
    card.querySelector("b").textContent = m.name;
    card.querySelector("small").textContent = (m.pages ? `${m.pages} page${m.pages === 1 ? "" : "s"} · ` : "") + "click to read";
    card.addEventListener("click", () => openViewer(m));
  }
  return card;
}
function mediaStrip(items) {
  const strip = document.createElement("div");
  strip.className = "media-strip";
  for (const m of items || []) strip.append(mediaCard(m));
  return strip;
}
function openViewer(m) {
  const back = $("#viewer-back"), body = $("#viewer-body");
  body.innerHTML = "";
  if (m.kind === "image") {
    const img = document.createElement("img"); img.src = mediaUrl(m); img.alt = m.name; body.append(img);
  } else {
    const frame = document.createElement("iframe"); frame.src = mediaUrl(m); frame.title = m.name; body.append(frame);
  }
  $("#viewer-title").textContent = m.name;
  $("#viewer-open").href = mediaUrl(m);
  back.classList.add("show");
}
$("#viewer-close").addEventListener("click", () => { $("#viewer-back").classList.remove("show"); $("#viewer-body").innerHTML = ""; });
$("#viewer-back").addEventListener("click", (ev) => { if (ev.target.id === "viewer-back") $("#viewer-close").click(); });
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && $("#viewer-back").classList.contains("show")) $("#viewer-close").click(); });

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
  if (data.media && data.media.length) el.append(mediaStrip(data.media));
}

function renderHistory(items) {
  $("#chat").innerHTML = "";
  for (const item of items) {
    if (item.t === "user") {
      const div = document.createElement("div");
      div.className = "msg user";
      div.innerHTML = `<div class="bubble">${esc(item.text)}</div>`;
      if (item.media && item.media.length) div.querySelector(".bubble").append(mediaStrip(item.media));
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
      if (item.ok !== undefined) toolDone({ id: item.id, ok: item.ok, preview: item.preview, elapsed: 0, media: item.media });
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
  ws = new WebSocket(`${proto}://${location.host}${location.pathname.replace(/[^/]*$/, "")}ws`);
  // (ws.send passed unbound throws "Illegal invocation", which used to drop anything typed before the socket opened)
  ws.onopen = () => { wsQueue.splice(0).forEach((m) => ws.send(m)); };
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
      case "heal_review": showHealReview(data); break;
      case "remote_login": $("#remote-back").classList.add("show"); $("#remote-email").focus(); break;
      case "tool": addTool(data); break;
      case "tool_done": toolDone(data); break;
      case "note": {
        const div = document.createElement("div");
        div.className = "note";
        div.textContent = data.text;
        if (live) {                       // keep the note in order with the reply it interrupts
          live.body = null;               // the next text starts a new segment below it
          live.flow.appendChild(div);
        } else {
          $("#chat").appendChild(div);
        }
        $("#chat").scrollTop = $("#chat").scrollHeight;
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
    const state = await (await fetch("api/state")).json();
    document.lastApiState = state;
    applyStatus(state.status);
    $("#model-name").textContent = state.status?.model_id || state.current_profile || "model";
    renderTodo(state.todo || []);
    renderSidebar(state);
  } catch { /* server restarting */ }
}

function applyStatus(status) {
  if (!status) return;
  lastStatus = status;
  busy = status.busy;
  const c = status.context;
  const label = `${status.model_id} · ctx ${(Math.round(c.pct))}% used of ${Math.round(c.window / 1000)}k` +
                (c.compact_at_pct ? ` (compact @${c.compact_at_pct}%)` : "") +
                (status.auto_mode === "all" ? " · AUTO-APPROVE ALL" :
                 status.auto_mode === "except-commits" ? " · auto-approve (asks before commits)" : "");
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
  const resp = await (await fetch("api/config", { method: "POST",
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
  const data = await (await fetch("api/config")).json();
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

// ---------------------------------------------------------------- self-heal review
let healFiles = [], healSel = 0;

function renderHealDiff() {
  const f = healFiles[healSel];
  $("#heal-diff").innerHTML = !f ? "" : esc(f.diff || "(binary or empty change)").split("\n").map(line =>
    line.startsWith("+") && !line.startsWith("+++") ? `<span class="add">${line}</span>` :
    line.startsWith("-") && !line.startsWith("---") ? `<span class="del">${line}</span>` :
    line.startsWith("@@") ? `<span class="hunk">${line}</span>` : line).join("\n");
  $$("#heal-files label").forEach((el, i) => el.classList.toggle("sel", i === healSel));
}

function showHealReview(data) {
  healFiles = data.files.map(f => ({ ...f, selected: true }));
  healSel = 0;
  $("#heal-summary").textContent = data.summary;
  const letter = { modified: "M", added: "A", deleted: "D" };
  $("#heal-files").innerHTML = healFiles.map((f, i) =>
    `<label data-i="${i}"><input type="checkbox" checked data-i="${i}"><span class="st ${letter[f.status]}">${letter[f.status] || "?"}</span>` +
    `<span>${esc(f.path)}</span><span class="n">+${f.added} −${f.removed}</span></label>`).join("");
  $$("#heal-files label").forEach(el => el.addEventListener("click", () => { healSel = +el.dataset.i; renderHealDiff(); }));
  $$("#heal-files input").forEach(el => el.addEventListener("change", () => { healFiles[+el.dataset.i].selected = el.checked; }));
  renderHealDiff();
  $("#heal-back").classList.add("show");
}

async function decideHeal(choice) {
  const files = healFiles.filter(f => f.selected).map(f => f.path);
  const r = await (await fetch("api/heal/decide", { method: "POST", headers: { "content-type": "application/json" },
                                body: JSON.stringify({ choice, files }) })).json();
  if (r.error && !r.kept) { alert(r.error); return; }
  $("#heal-back").classList.remove("show");
  const div = document.createElement("div");
  div.className = "note"; div.textContent = (r.error ? "⚠ " + r.error : "✓ " + r.message) + (r.restarting ? " — restarting…" : "");
  $("#chat").appendChild(div);
  if (r.restarting) {
    const wait = async () => { try { await fetch("api/state"); location.reload(); } catch { setTimeout(wait, 1000); } };
    setTimeout(wait, 2500);
  }
}
["send", "keep", "discard", "later"].forEach(c => $(`#heal-${c}`).addEventListener("click", () => decideHeal(c)));

// ---------------------------------------------------------------- live model picker
let pickerItems = [], pickerSel = 0;

function fuzzy(q, text) {
  let i = 0; text = text.toLowerCase();
  for (const ch of q.toLowerCase().replace(/\s/g, "")) { i = text.indexOf(ch, i); if (i < 0) return false; i++; }
  return true;
}

function renderPicker(data) {
  const q = $("#picker-search").value.trim();
  pickerItems = [];
  let html = "";
  for (const g of data.groups || []) {
    const ms = g.models.filter(m => !q || fuzzy(q, `${m.model} ${g.label} ${m.profile}`));
    if (!ms.length) continue;
    html += `<div class="group">${esc(g.label)}</div>`;
    for (const m of ms) {
      const idx = pickerItems.push(m) - 1;
      html += `<div class="item${idx === pickerSel ? " sel" : ""}" data-i="${idx}"><span>${esc(m.model)}</span>` +
              (m.detail ? `<span class="meta">${esc(m.detail)}</span>` : "") +
              (m.profile ? `<span class="meta">profile ${esc(m.profile)}</span>` : "") +
              (m.current ? `<span class="cur">● current</span>` : "") + `</div>`;
    }
  }
  $("#picker-list").innerHTML = html || "<div class='dim' style='padding:10px'>no matching models</div>";
  $$("#picker-list .item").forEach(el => el.addEventListener("click", () => choosePicked(+el.dataset.i)));
  const errs = Object.entries(data.errors || {}).map(([k, v]) => `<span class="err">✗ ${esc(k)}: ${esc(v)}</span>`);
  const n = (data.groups || []).reduce((a, g) => a + g.models.length, 0);
  $("#picker-status").innerHTML = [`${n} model(s) available now`, ...errs].join(" · ");
}

let pickerData = { groups: [] };
async function openPicker() {
  $("#picker-search").value = ""; pickerSel = 0;
  $("#picker-list").innerHTML = "<div class='dim' style='padding:10px'>asking every provider what's available…</div>";
  $("#picker-status").textContent = "";
  $("#picker-back").classList.add("show");
  $("#picker-search").focus();
  pickerData = await (await fetch("api/models/live")).json();   // fresh every time — never cached
  renderPicker(pickerData);
}

async function choosePicked(i) {
  const m = pickerItems[i];
  if (!m) return;
  $("#picker-back").classList.remove("show");
  const scope = await askScope(`Switch to ${m.model}`);
  if (!scope) return;
  const r = await (await fetch("api/models/select", { method: "POST", headers: { "content-type": "application/json" },
                                body: JSON.stringify({ id: m.id, scope }) })).json();
  const div = document.createElement("div");
  div.className = "note"; div.textContent = r.error ? "⚠ " + r.error : "✓ " + r.message;
  $("#chat").appendChild(div);
  refreshState();
}

$("#model-btn").addEventListener("click", openPicker);
$("#picker-close").addEventListener("click", () => $("#picker-back").classList.remove("show"));
$("#picker-search").addEventListener("input", () => { pickerSel = 0; renderPicker(pickerData); });
$("#picker-search").addEventListener("keydown", (ev) => {
  if (ev.key === "ArrowDown") { pickerSel = Math.min(pickerSel + 1, pickerItems.length - 1); renderPicker(pickerData); ev.preventDefault(); }
  else if (ev.key === "ArrowUp") { pickerSel = Math.max(pickerSel - 1, 0); renderPicker(pickerData); ev.preventDefault(); }
  else if (ev.key === "Enter") { choosePicked(pickerSel); ev.preventDefault(); }
  else if (ev.key === "Escape") { $("#picker-back").classList.remove("show"); }
});
document.addEventListener("keydown", (ev) => {
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "p") { ev.preventDefault(); openPicker(); }
});
$("#clear-btn").addEventListener("click", async () => {
  if (!confirm("Clear this conversation?")) return;
  await fetch("api/clear", { method: "POST" });
  $("#chat").innerHTML = "";
  refreshState();
});
$("#compact-btn").addEventListener("click", async () => {
  await fetch("api/compact", { method: "POST" });
  const div = document.createElement("div");
  div.className = "note"; div.textContent = "compacting…";
  $("#chat").appendChild(div);
});

// composer
const input = $("#input");

// ---------------------------------------------------------------- slash menu
let slashCommands = [], slashItems = [], slashSel = 0;
fetch("api/commands").then(r => r.json()).then(d => {
  slashCommands = d.commands || [];
  if (location.hash === "#slash") { input.value = "/u"; renderSlash(); }      // demo / screenshots
}).catch(() => {});
const CLIENT_COMMANDS = { model: () => openPicker(), config: () => $("#settings-btn").click(),
                          update: () => { const b = $("#update-btn"); b.style.display ? checkUpdate() : b.click(); } };

function renderSlash() {
  const text = input.value;
  const menu = $("#slash-menu");
  if (!text.startsWith("/") || text.includes(" ") || text.includes("\n")) { menu.hidden = true; slashItems = []; return; }
  const q = text.slice(1).toLowerCase();
  slashItems = slashCommands.filter(c => c.name.startsWith(q))
    .concat(q.length >= 2 ? slashCommands.filter(c => !c.name.startsWith(q) && c.name.includes(q)) : []);
  if (!slashItems.length) { menu.hidden = true; return; }
  slashSel = Math.min(slashSel, slashItems.length - 1);
  menu.innerHTML = slashItems.map((c, i) => `<div class="row${i === slashSel ? " sel" : ""}" data-i="${i}">` +
    `<span class="name">/${esc(c.name)}</span><span class="args">${esc(c.args || "")}</span>` +
    `<span class="desc">${esc(c.desc || "")}</span></div>`).join("");
  $$("#slash-menu .row").forEach(el => el.addEventListener("mousedown", (ev) => { ev.preventDefault(); pickSlash(+el.dataset.i, true); }));
  menu.hidden = false;
}

function pickSlash(i, run) {
  const c = slashItems[i];
  if (!c) return;
  $("#slash-menu").hidden = true;
  if (CLIENT_COMMANDS[c.name]) { input.value = ""; CLIENT_COMMANDS[c.name](); return; }
  if (c.args || !run) { input.value = `/${c.name} `; input.focus(); return; }
  input.value = `/${c.name}`;
  submit();
}

input.addEventListener("keydown", (ev) => {
  if (!$("#slash-menu").hidden && slashItems.length) {
    if (ev.key === "ArrowDown") { slashSel = (slashSel + 1) % slashItems.length; renderSlash(); ev.preventDefault(); return; }
    if (ev.key === "ArrowUp") { slashSel = (slashSel - 1 + slashItems.length) % slashItems.length; renderSlash(); ev.preventDefault(); return; }
    if (ev.key === "Tab") { pickSlash(slashSel, false); ev.preventDefault(); return; }
    if (ev.key === "Enter" && !ev.shiftKey) { pickSlash(slashSel, true); ev.preventDefault(); return; }
    if (ev.key === "Escape") { $("#slash-menu").hidden = true; return; }
  }
  if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); submit(); }
});
const FILE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf"];
let attached = [];                                  // images and PDFs waiting to be sent with the next message
async function addFile(file) {
  try {
    const res = await (await fetch("api/paste-image", { method: "POST",
      headers: { "content-type": file.type, "x-filename": file.name || "" }, body: file })).json();
    if (!res.path) throw new Error(res.error || "upload failed");
    const ref = res.path.includes(" ") ? `"${res.path}" ` : `${res.path} `;
    input.setRangeText(ref, input.selectionStart, input.selectionEnd, "end");
    attached.push(res);
    renderAttached();
    input.dispatchEvent(new Event("input"));
  } catch (e) { alert(`could not attach ${file.name || "file"}: ${e.message}`); }
}
function renderAttached() {
  const box = $("#attached");
  box.innerHTML = "";
  attached.forEach((m, i) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    if (m.kind === "image") { const img = document.createElement("img"); img.src = mediaUrl(m); chip.append(img); }
    else chip.append("📄 ");
    chip.append(m.name);
    chip.addEventListener("click", () => openViewer(m));
    const x = document.createElement("button");
    x.type = "button"; x.textContent = "✕"; x.title = "Remove";
    x.addEventListener("click", (ev) => {
      ev.stopPropagation();
      input.value = input.value.replace(m.path.includes(" ") ? `"${m.path}" ` : `${m.path} `, "");
      attached.splice(i, 1); renderAttached();
    });
    chip.append(x);
    box.append(chip);
  });
  box.hidden = attached.length === 0;
}
input.addEventListener("paste", async (ev) => {
  const files = [...(ev.clipboardData?.files || [])].filter(f => FILE_TYPES.includes(f.type));
  if (!files.length) return;                      // plain text pastes normally
  ev.preventDefault();
  for (const f of files) await addFile(f);
});
for (const type of ["dragenter", "dragover"]) document.addEventListener(type, (ev) => {
  if ([...(ev.dataTransfer?.types || [])].includes("Files")) { ev.preventDefault(); document.body.classList.add("dropping"); }
});
document.addEventListener("dragleave", (ev) => { if (!ev.relatedTarget) document.body.classList.remove("dropping"); });
document.addEventListener("drop", async (ev) => {
  document.body.classList.remove("dropping");
  const files = [...(ev.dataTransfer?.files || [])].filter(f => FILE_TYPES.includes(f.type));
  if (!files.length) return;
  ev.preventDefault();
  for (const f of files) await addFile(f);
  input.focus();
});
input.addEventListener("input", () => { slashSel = 0; renderSlash(); });
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
  const sent = attached.filter(m => text.includes(m.path));
  if (sent.length) div.querySelector(".bubble").append(mediaStrip(sent));
  attached = []; renderAttached();
  $("#chat").appendChild(div);
  busy = true; updateSendButton();
  sendWs({ t: "send", text });
}
$("#send").addEventListener("click", submit);

connect();
refreshState();
setInterval(refreshState, 20000);
// ---------------------------------------------------------------- self-update

async function checkUpdate() {
  try {
    const info = await (await fetch("api/update")).json();
    const btn = $("#update-btn");
    if (info.available) {
      btn.textContent = `⬆ Update to ${info.latest}`;
      btn.title = `You have ${info.current}. Install ${info.latest} and restart (your conversation is kept).`;
      btn.style.display = "";
    } else { btn.style.display = "none"; }
  } catch { /* offline */ }
}
$("#update-btn").addEventListener("click", async () => {
  const btn = $("#update-btn");
  if (!confirm(`${btn.title}\n\nUpdate now?`)) return;
  btn.disabled = true; btn.textContent = "updating…";
  const r = await (await fetch("api/update", { method: "POST" })).json();
  const div = document.createElement("div");
  div.className = "note"; div.textContent = (r.ok ? "✓ " : "⚠ ") + r.log + (r.restarting ? "\nrestarting…" : "");
  $("#chat").appendChild(div);
  if (r.restarting) {   // wait for the new server, then reload onto the new UI
    const wait = async () => { try { await fetch("api/state"); location.reload(); } catch { setTimeout(wait, 1000); } };
    setTimeout(wait, 2500);
  } else { btn.disabled = false; checkUpdate(); }
});
checkUpdate();

// deep link: /#settings opens the settings panel (bookmarks, screenshots)
if (location.hash === "#settings") $("#settings-btn").click();
if (location.hash === "#models") openPicker();


// ---------------------------------------------------------------- /remote-control sign-in
function closeRemoteLogin() { $("#remote-back").classList.remove("show"); $("#remote-password").value = ""; }
$("#remote-close").addEventListener("click", closeRemoteLogin);
async function remoteLogin() {
  const msg = $("#remote-msg");
  msg.textContent = "signing in…";
  try {
    const res = await fetch("/api/remote/login", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ email: $("#remote-email").value, password: $("#remote-password").value }),
    });
    const r = await res.json();
    if (r.error) { msg.textContent = "⚠ " + r.error; return; }
    closeRemoteLogin(); msg.textContent = "";
    const div = document.createElement("div");
    div.className = "note"; div.textContent = "✓ " + r.message;
    $("#chat").appendChild(div);
  } catch (e) { msg.textContent = "⚠ " + e; }
}
$("#remote-go").addEventListener("click", remoteLogin);
$("#remote-password").addEventListener("keydown", (ev) => { if (ev.key === "Enter") remoteLogin(); });

// ---------------------------------------------------------------- mobile drawer
// On small screens the sidebar becomes an overlay drawer behind the ☰ button.
const closeDrawer = () => document.body.classList.remove("side-open");
$("#menu-btn").addEventListener("click", () => document.body.classList.toggle("side-open"));
$("#side-back").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeDrawer(); });
// opening any topbar panel also folds the drawer back in
["#model-btn", "#compact-btn", "#clear-btn", "#settings-btn"].forEach(
  (sel) => $(sel).addEventListener("click", closeDrawer));
