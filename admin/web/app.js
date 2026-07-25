/* 会话中心前端逻辑：vanilla JS + 原生 EventSource，零构建、零外部依赖（内网可开）。 */
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 10000;

  var state = {
    token: localStorage.getItem("pvg_admin_token") || "",
    username: localStorage.getItem("pvg_admin_username") || "",
    role: localStorage.getItem("pvg_admin_role") || "",
    sessions: [],
    currentSid: null,
    currentMode: "auto",
    events: [],      // 当前会话已收到的事件（按 offset 升序）
    nextOffset: 0,
    es: null,        // 当前 EventSource
    pollTimer: null,
  };

  // ---------- 基础工具 ----------

  function $(id) { return document.getElementById(id); }

  function api(path, options) {
    options = options || {};
    options.headers = Object.assign(
      { "Authorization": "Bearer " + state.token },
      options.body ? { "Content-Type": "application/json" } : {},
      options.headers || {}
    );
    return fetch(path, options).then(function (resp) {
      if (resp.status === 401) { logout(); throw new Error("登录已失效"); }
      if (!resp.ok) {
        return resp.text().then(function (t) { throw new Error("HTTP " + resp.status + ": " + t); });
      }
      return resp.json();
    });
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ---------- 登录 ----------

  function showMain() {
    $("login-view").classList.add("hidden");
    $("main-view").classList.remove("hidden");
    $("who").textContent = state.username + "（" + state.role + "）";
    startPolling();
  }

  function logout() {
    localStorage.removeItem("pvg_admin_token");
    localStorage.removeItem("pvg_admin_username");
    localStorage.removeItem("pvg_admin_role");
    location.reload();
  }

  $("login-form").addEventListener("submit", function (e) {
    e.preventDefault();
    $("login-error").textContent = "";
    fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: $("login-username").value.trim(),
        password: $("login-password").value,
      }),
    }).then(function (resp) {
      if (!resp.ok) throw new Error("用户名或密码错误");
      return resp.json();
    }).then(function (body) {
      state.token = body.token;
      state.username = body.username;
      state.role = body.role;
      localStorage.setItem("pvg_admin_token", body.token);
      localStorage.setItem("pvg_admin_username", body.username);
      localStorage.setItem("pvg_admin_role", body.role);
      showMain();
    }).catch(function (err) {
      $("login-error").textContent = err.message;
    });
  });

  $("btn-logout").addEventListener("click", logout);

  // ---------- 左栏：会话列表（10s 轮询） ----------

  function startPolling() {
    refreshSessions();
    state.pollTimer = setInterval(refreshSessions, POLL_INTERVAL_MS);
  }

  function refreshSessions() {
    api("/api/sessions").then(function (items) {
      state.sessions = items;
      renderSessionList();
      // 当前会话的 mode 可能被其他坐席改动，同步刷新接管条
      var cur = items.filter(function (s) { return s.id === state.currentSid; })[0];
      if (cur && cur.mode !== state.currentMode) {
        state.currentMode = cur.mode;
        renderTakeoverBar();
      }
    }).catch(function () { /* 轮询失败静默，下轮重试 */ });
  }

  function renderSessionList() {
    var ul = $("session-list");
    ul.innerHTML = "";
    state.sessions.forEach(function (s) {
      var li = document.createElement("li");
      if (s.id === state.currentSid) li.className = "active";
      var title = s.title || s.id;
      var last = s.last_message ? s.last_message.preview : "(无消息)";
      var ts = s.last_activity_utc || s.creation_utc || "";
      li.innerHTML =
        '<div class="title">' +
        (s.unanswered ? '<span class="dot" title="疑似已读不回"></span>' : "") +
        esc(title) +
        ' <span class="mode-badge' + (s.mode === "manual" ? " manual" : "") + '">' + esc(s.mode || "auto") + "</span></div>" +
        '<div class="meta">' + esc(last) + "</div>" +
        '<div class="meta">' + esc(ts) + "</div>";
      li.addEventListener("click", function () { selectSession(s); });
      ul.appendChild(li);
    });
  }

  // ---------- 中栏：对话视图（历史分页 + SSE 实时） ----------

  function selectSession(s) {
    state.currentSid = s.id;
    state.currentMode = s.mode || "auto";
    state.events = [];
    state.nextOffset = 0;
    $("chat").innerHTML = "";
    $("chat-empty").classList.add("hidden");
    $("trace").innerHTML = "";
    $("trace-empty").classList.remove("hidden");
    renderSessionList();
    renderTakeoverBar();
    if (state.es) { state.es.close(); state.es = null; }

    // 先拉历史事件（trace 回放同源数据），再从 next_offset 接 SSE
    api("/api/sessions/" + encodeURIComponent(s.id) + "/events?min_offset=0&limit=500")
      .then(function (page) {
        (page.items || []).forEach(appendEvent);
        state.nextOffset = page.next_offset || 0;
        openStream();
      })
      .catch(function (err) { appendSystemLine("加载历史失败：" + err.message); openStream(); });
  }

  function openStream() {
    var url = "/api/sessions/" + encodeURIComponent(state.currentSid) +
      "/stream?min_offset=" + state.nextOffset + "&token=" + encodeURIComponent(state.token);
    var es = new EventSource(url);
    state.es = es;
    es.addEventListener("parlant-event", function (e) {
      var ev = JSON.parse(e.data);
      appendEvent(ev);
      if (typeof ev.offset === "number") state.nextOffset = Math.max(state.nextOffset, ev.offset + 1);
    });
    es.addEventListener("error", function (e) {
      if (e.data) appendSystemLine("流错误：" + e.data);
      // EventSource 自动重连；重连后 min_offset 不变会重复事件，appendEvent 内按 offset 去重
    });
  }

  var seenOffsets = {};

  function appendEvent(ev) {
    if (typeof ev.offset === "number") {
      if (seenOffsets[state.currentSid + ":" + ev.offset]) return;
      seenOffsets[state.currentSid + ":" + ev.offset] = true;
    }
    state.events.push(ev);
    state.events.sort(function (a, b) { return (a.offset || 0) - (b.offset || 0); });
    renderChat();
  }

  function appendSystemLine(text) {
    var div = document.createElement("div");
    div.className = "meta";
    div.style.textAlign = "center";
    div.style.color = "#9aa3b8";
    div.textContent = text;
    $("chat").appendChild(div);
  }

  function isPreamble(ev) {
    // preamble 判定为启发式：data.preamble 或 data.metadata.preamble 为真时标注"占位"
    var d = ev.data || {};
    return d.preamble === true || (d.metadata && d.metadata.preamble === true);
  }

  function renderChat() {
    var chat = $("chat");
    chat.innerHTML = "";
    var statusBuffer = [];

    function flushStatus() {
      if (!statusBuffer.length) return;
      var strip = document.createElement("div");
      strip.className = "stage-strip";
      statusBuffer.forEach(function (ev) {
        var d = (ev.data && ev.data.data) || {};
        var status = (ev.data && ev.data.status) || "?";
        var chip = document.createElement("span");
        chip.className = "chip" + (status === "error" ? " error" : "");
        chip.textContent = status + (d.stage ? ":" + d.stage : "");
        strip.appendChild(chip);
      });
      chat.appendChild(strip);
      statusBuffer = [];
    }

    state.events.forEach(function (ev, idx) {
      if (ev.kind === "status") { statusBuffer.push(ev); return; }
      flushStatus();
      if (ev.kind === "message") {
        var div = document.createElement("div");
        div.className = "bubble " + (ev.source || "customer");
        var who = ev.source === "customer" ? "旅客" :
          ev.source === "human_agent" ? "坐席" : "AI";
        var name = (ev.data && ev.data.participant && ev.data.participant.display_name) || who;
        div.innerHTML =
          '<div class="who-line">' + esc(name) +
          (isPreamble(ev) ? ' <span class="tag">占位 preamble</span>' : "") + "</div>" +
          esc((ev.data && ev.data.message) || "");
        if (ev.source === "ai_agent") {
          div.title = "点击查看该轮 trace";
          div.addEventListener("click", function () { renderTrace(idx); });
        }
        chat.appendChild(div);
      } else if (ev.kind === "tool") {
        var det = document.createElement("details");
        det.className = "tool-block";
        det.innerHTML = "<summary>工具调用</summary><pre>" +
          esc(JSON.stringify(ev.data, null, 2)) + "</pre>";
        chat.appendChild(det);
      }
    });
    flushStatus();
    chat.scrollTop = chat.scrollHeight;
  }

  // ---------- 右栏：trace 面板 ----------

  function renderTrace(aiIdx) {
    $("trace-empty").classList.add("hidden");
    var evs = state.events;
    // 轮次窗口：上一条 customer message（含）→ 本条 ai message 后首个 ready:completed status（含）
    var start = 0;
    for (var i = aiIdx - 1; i >= 0; i--) {
      if (evs[i].kind === "message" && evs[i].source === "customer") { start = i; break; }
    }
    var end = evs.length - 1;
    for (var j = aiIdx + 1; j < evs.length; j++) {
      var d = evs[j].data || {};
      if (evs[j].kind === "status" && d.status === "ready" && d.data && d.data.stage === "completed") { end = j; break; }
      if (evs[j].kind === "message" && evs[j].source === "customer") { end = j - 1; break; }
    }
    var window = evs.slice(start, end + 1);

    var html = "<h3>阶段耗时</h3>";
    var lastTs = null;
    window.forEach(function (ev) {
      if (ev.kind !== "status") return;
      var d = ev.data || {};
      var inner = d.data || {};
      var ts = ev.creation_utc ? Date.parse(ev.creation_utc) : null;
      var delta = lastTs != null && ts != null ? ((ts - lastTs) / 1000).toFixed(2) + "s" : "—";
      if (ts != null) lastTs = ts;
      html += '<div class="stage-row"><span>' + esc(d.status || "?") +
        (inner.stage ? " · " + esc(inner.stage) : "") + "</span><span>" + delta + "</span></div>";
    });

    var tools = window.filter(function (ev) { return ev.kind === "tool"; });
    if (tools.length) {
      html += "<h3>工具调用（入参/出参）</h3>";
      tools.forEach(function (ev) {
        html += "<pre>" + esc(JSON.stringify(ev.data, null, 2)) + "</pre>";
      });
    }

    // 事件 data 中若带有规则/话术来源信息则展示（防御式，字段以后续引擎实际输出为准）
    var ai = evs[aiIdx];
    var extra = Object.assign({}, ai.data || {});
    delete extra.message;
    delete extra.participant;
    if (Object.keys(extra).length) {
      html += "<h3>回复来源信息</h3>";
      Object.keys(extra).forEach(function (k) {
        html += '<div class="kv"><b>' + esc(k) + "</b>：" + esc(JSON.stringify(extra[k])) + "</div>";
      });
    }
    $("trace").innerHTML = html;
  }

  // ---------- 接管条与人工发送 ----------

  function canOperate() { return state.role === "supervisor" || state.role === "admin"; }

  function renderTakeoverBar() {
    var bar = $("takeover-bar");
    bar.classList.remove("hidden");
    $("mode-label").textContent = state.currentMode === "manual"
      ? "当前为人工接管模式（AI 已暂停）" : "当前为 AI 自动模式";
    $("btn-takeover").classList.toggle("hidden", !canOperate() || state.currentMode === "manual");
    $("btn-resume").classList.toggle("hidden", !canOperate() || state.currentMode !== "manual");
    $("human-input").classList.toggle("hidden", !canOperate() || state.currentMode !== "manual");
  }

  function setMode(mode) {
    api("/api/sessions/" + encodeURIComponent(state.currentSid) + "/takeover", {
      method: "PATCH",
      body: JSON.stringify({ mode: mode }),
    }).then(function () {
      state.currentMode = mode;
      renderTakeoverBar();
      refreshSessions();
    }).catch(function (err) { alert("操作失败：" + err.message); });
  }

  $("btn-takeover").addEventListener("click", function () { setMode("manual"); });
  $("btn-resume").addEventListener("click", function () { setMode("auto"); });

  function sendHumanMessage() {
    var text = $("human-text").value.trim();
    if (!text || !state.currentSid) return;
    api("/api/sessions/" + encodeURIComponent(state.currentSid) + "/messages", {
      method: "POST",
      body: JSON.stringify({ message: text }),
    }).then(function () {
      $("human-text").value = "";
      // 发出的 human_agent 消息随后经 SSE 流回到对话视图
    }).catch(function (err) { alert("发送失败：" + err.message); });
  }

  $("btn-send").addEventListener("click", sendHumanMessage);
  $("human-text").addEventListener("keydown", function (e) {
    if (e.key === "Enter") sendHumanMessage();
  });

  // ---------- 启动 ----------

  if (state.token) {
    // 已有 token：直接进主视图（失效时首个 401 会踢回登录页）
    showMain();
  }
})();
