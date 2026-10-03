/* 会议知识编译台 — 前端控制器（零依赖，离线可用）
 *
 * 职责：文件投放（拖拽+选择双通道，录音通道兼收录屏视频）→ /api/upload → /api/start；
 * 每 1s 轮询 /api/status 驱动徽标 / 总计 / 阶段轨道（站数以 stage_total 为准）/ 队列 / 时间线；
 * 待确认区域的录屏事件：队列卡片按钮打开确认弹窗（detect → 拖框 → confirm，规格 §3.6）；
 * 结果包区内容交给 MSTResults（results.js，与本文件并行加载）。
 */
(function () {
  "use strict";

  /* 14 阶段（run_meeting.STAGE_ORDER 契约；stage_index 顺序。渲染站数一律取
     stage_total 或 STAGE_LABELS.length，本文件不出现站数字面量） */
  var STAGE_LABELS = [
    "文件清单", "视频分解（音轨/幻灯片）", "音频预处理（降噪）", "语言探测", "语音识别",
    "组装逐句记录", "生成证据", "无关话语过滤", "幻灯片对齐", "主题提取与索引",
    "claim 保真审计", "笔记佐证", "构建报告包", "验证与质量门禁"
  ];
  /* stage 原始键 → 中文（时间线行与当前阶段详情用；键是流水线稳定契约） */
  var STAGE_KEYS = {
    inventory: "文件清单", video_ingest: "视频分解（音轨/幻灯片）",
    audio_prepare: "音频预处理（降噪）", lang_probe: "语言探测",
    asr: "语音识别", literal: "组装逐句记录", evidence: "生成证据",
    relevance: "无关话语过滤", slide_align: "幻灯片对齐",
    reconcile: "主题提取与索引", audit: "claim 保真审计",
    notes: "笔记佐证", package: "构建报告包", validate: "验证与质量门禁",
    "asr.segment": "语音识别 · 分段", "asr.whisper": "语音识别 · Whisper", "asr.qwen": "语音识别 · Qwen",
    "video_ingest.probe": "视频分解 · 探测区域", "video_ingest.audio": "视频分解 · 音轨决策",
    "video_ingest.frames": "视频分解 · 抽帧分段", "video_ingest.segment": "视频分解 · 分段完成",
    "video_ingest.ocr": "视频分解 · OCR"
  };
  var AUDIO_EXTS = { m4a: 1, mp3: 1, wav: 1, aac: 1, flac: 1, aiff: 1, caf: 1 };
  /* 录屏视频（server VIDEO_EXTS 白名单，规格 §3.5）：走录音通道投放，行标 🎬 */
  var VIDEO_EXTS = { mp4: 1, mov: 1, mkv: 1, webm: 1, m4v: 1 };
  var NOTE_EXTS = { md: 1, markdown: 1, txt: 1 };

  var $ = function (id) { return document.getElementById(id); };
  var els = {
    offline: $("offline"), pid: $("pid"), badge: $("badge"), badgeText: $("badge-text"),
    totals: $("totals"), track: $("track"), detail: $("track-detail"),
    dependencies: $("dependencies"), candidates: $("candidates"),
    eventCard: $("event-card"), queue: $("queue"), timeline: $("timeline"),
    eventName: $("event-name"), outputDir: $("output-dir"), fragmentMode: $("fragment-mode"),
    pickDir: $("btn-pick-dir"),
    btnStart: $("btn-start"), btnStop: $("btn-stop"), toasts: $("toasts"),
    btnQuit: $("btn-quit"), quitHint: $("quit-hint"),
    regionModal: $("region-modal"), regionEvent: $("region-modal-event"),
    regionError: $("region-modal-error"), regionWarn: $("region-modal-warn"),
    regionWork: $("region-work"), regionStage: $("region-stage"),
    regionImg: $("region-preview"), regionBox: $("region-box"),
    regionHint: $("region-hint"), regionReadout: $("region-readout"),
    regionX: $("region-modal-x"), btnRegionAdopt: $("region-adopt"),
    btnRegionConfirm: $("region-confirm"), btnRegionRetry: $("region-retry")
  };

  var sel = { audio: [], notes: [] };   /* 已选待上传文件 */
  var uploading = false;                /* 上传/启动流程占用主按钮 */
  var lastStatus = null;                /* 最近一次 /api/status */
  var consoleDead = false;              /* 服务已从页面关闭：停止轮询 */
  var trackBuilt = 0;                   /* 已构建轨道的 stage_total */
  var queueSig = "", timelineSig = "";  /* 变更签名，避免无谓重绘 */
  var eventStartTs = null;              /* 当前事件的 event_start 时间 */

  /* ---- 小工具 ---------------------------------------------------------- */

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function pad2(n) { return (n < 10 ? "0" : "") + n; }
  function fmtClock(ts) {
    if (!ts) return "--:--:--";
    var d = new Date(ts * 1000);
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }
  function fmtDur(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h > 0 ? h + ":" + pad2(m) + ":" + pad2(s) : pad2(m) + ":" + pad2(s);
  }
  function fmtSize(bytes) {
    if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + " MB";
    if (bytes >= 1024) return Math.round(bytes / 1024) + " KB";
    return bytes + " B";
  }
  function extOf(name) {
    var i = String(name).lastIndexOf(".");
    return i < 0 ? "" : String(name).slice(i + 1).toLowerCase();
  }
  function nowSec() { return Date.now() / 1000; }

  function toast(msg, kind) {
    var t = document.createElement("div");
    t.className = "toast" + (kind === "error" ? " toast-error" : "");
    t.textContent = msg;
    els.toasts.appendChild(t);
    var life = kind === "error" ? 6000 : 4000;
    setTimeout(function () { t.classList.add("out"); }, life);
    setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, life + 350);
  }

  /* ---- 文件投放（拖拽 + 选择双通道）------------------------------------ */

  function addFiles(kind, fileList) {
    var exts = kind === "audio" ? AUDIO_EXTS : NOTE_EXTS;
    var ignored = 0;
    Array.prototype.forEach.call(fileList || [], function (f) {
      /* 录音通道兼收录屏（规格 §3.6）：server 端按扩展名分类落盘 */
      var ok = exts[extOf(f.name)] ||
        (kind === "audio" && VIDEO_EXTS[extOf(f.name)]);
      if (!ok) { ignored++; return; }
      var dup = sel[kind].some(function (x) { return x.name === f.name && x.size === f.size; });
      if (!dup) sel[kind].push(f);
    });
    renderFileList(kind);
    if (ignored > 0) toast("已忽略 " + ignored + " 个类型不匹配的文件");
  }

  function renderFileList(kind) {
    var ul = $(kind === "audio" ? "list-audio" : "list-notes");
    if (!sel[kind].length) { ul.innerHTML = ""; return; }
    ul.innerHTML = sel[kind].map(function (f, i) {
      var isVideo = kind === "audio" && VIDEO_EXTS[extOf(f.name)];
      return '<li class="frow">' +
        (isVideo ? '<span class="fico" aria-label="录屏文件">🎬</span>' : "") +
        '<span class="fname" title="' + esc(f.name) + '">' + esc(f.name) + "</span>" +
        '<span class="fsize num">' + fmtSize(f.size) + "</span>" +
        '<button type="button" class="frm" data-kind="' + kind + '" data-i="' + i +
        '" aria-label="移除 ' + esc(f.name) + '">×</button></li>';
    }).join("");
  }

  function onRemoveClick(e) {
    var btn = e.target && e.target.closest ? e.target.closest(".frm") : null;
    if (!btn) return;
    e.stopPropagation();
    sel[btn.getAttribute("data-kind")].splice(+btn.getAttribute("data-i"), 1);
    renderFileList(btn.getAttribute("data-kind"));
  }

  function setupZone(zoneId, inputId, kind) {
    var zone = $(zoneId), input = $(inputId);
    zone.addEventListener("click", function () { input.click(); });
    zone.addEventListener("keydown", function (e) {
      if (e.target !== zone) return;
      if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
        e.preventDefault();
        input.click();
      }
    });
    input.addEventListener("change", function () {
      addFiles(kind, input.files);
      input.value = ""; /* 允许再次选择同名文件 */
    });
    ["dragenter", "dragover"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) {
        e.preventDefault();
        e.stopPropagation();
        zone.classList.add("over");
      });
    });
    zone.addEventListener("dragleave", function (e) {
      if (!zone.contains(e.relatedTarget)) zone.classList.remove("over");
    });
    zone.addEventListener("drop", function (e) {
      e.preventDefault();
      e.stopPropagation();
      zone.classList.remove("over");
      if (e.dataTransfer) addFiles(kind, e.dataTransfer.files);
    });
  }

  /* ---- 状态渲染 --------------------------------------------------------- */

  function render(status) {
    lastStatus = status;
    renderBadge(status);
    renderTotals(status.totals || {});
    renderTrack(status);
    renderCurrent(status);
    renderQueue(status.queue || []);
    renderTimeline(status.recent || []);
    renderDependencies(status.dependencies || {});
    renderCandidates(status.candidate_plan || {});
    renderFragmentPlan(status.fragment_plan || {});
    syncControls();
  }

  function renderDependencies(deps) {
    if (!els.dependencies) return;
    var items = deps.items || [];
    els.dependencies.innerHTML = items.map(function (d) {
      var state = d.ready ? "已就绪" : (d.required ? "缺失（必需）" : "缺失（仅录屏可选）");
      return '<p class="qrow"><label><input class="dep-choice" type="checkbox" value="' + esc(d.id) + '"' + (!d.ready ? " checked" : " disabled") + '> <strong>' + esc(d.id) + '</strong></label> · ' + esc(d.tier) + ' · ' + esc(d.purpose) +
        ' · ' + esc(d.download) + ' · ' + state + ' · <a href="' + esc(d.official_url) + '" target="_blank" rel="noreferrer">官方链接</a></p>';
    }).join("") + '<button id="btn-install-deps" class="btn btn-ghost" type="button"' +
      (deps.installing ? " disabled" : "") + '>' + (deps.installing ? "本地安装中…" : "安装缺失依赖") + "</button>" +
      (deps.error ? '<p class="modal-error">' + esc(deps.error) + "</p>" : "");
    var btn = $("btn-install-deps");
    if (btn) btn.addEventListener("click", function () {
      var missing = Array.prototype.map.call(document.querySelectorAll(".dep-choice:checked"), function (x) { return x.value; });
      if (!missing.length) { toast("依赖已全部就绪"); return; }
      fetch("/api/dependencies/install", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ids: missing})})
        .then(function (r) { return r.json(); }).then(function (d) { toast(d.message || d.error || "已提交安装", d.error ? "error" : ""); poll(); })
        .catch(function () { toast("无法启动本地安装", "error"); });
    });
  }

  function renderCandidates(plan) {
    if (!els.candidates) return;
    var rows = plan.candidates || [];
    if (!rows.length) { els.candidates.textContent = "尚未发现候选会议。"; return; }
    els.candidates.innerHTML = rows.map(function (x, i) { return '<p class="qrow">' + (i + 1) + ". " + esc(x.event) + " · " + x.files + " 文件 · 独立处理 · " + (x.parsed ? esc(x.evidence.join(" / ")) : "等待独立解析") + "</p>"; }).join("") +
      (plan.uncertainties || []).map(function (x) { return '<p class="modal-warn">' + esc(x) + "</p>"; }).join("") +
      (rows.length > 1 && !plan.confirmed && plan.ready_to_confirm ? '<button id="btn-confirm-candidates" class="btn btn-ghost" type="button">确认候选分组与顺序</button>' : "");
    var btn = $("btn-confirm-candidates");
    if (btn) btn.addEventListener("click", function () {
      fetch("/api/candidates/confirm", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({order: rows.map(function (x) { return x.event; })})})
        .then(function (r) { return r.json(); }).then(function (d) { toast(d.ok ? "已确认；会议将分别处理" : (d.error || "确认失败"), d.ok ? "" : "error"); poll(); });
    });
  }

  function renderFragmentPlan(plan) {
    if (!els.candidates || !(plan.groups || []).length) return;
    var html = '<h3>散乱片段阅读顺序（本地候选）</h3>' + plan.groups.map(function (g) {
      return '<p class="qrow">' + g.fragments.map(function (f) { return esc(f.source) + '（' + Math.round(f.duration_seconds) + 's）'; }).join(' → ') + (g.gap_or_uncertain ? ' · 低置信：可能有缺段或不相关片段' : ' · 候选连续') + (g.note_order_hint != null ? ' · 笔记低置信顺序提示' : '') + '</p>';
    }).join('') + ((plan.references || []).length ? '<p class="qrow">本地参考笔记：' + esc(plan.references.join('、')) + '（仅作线索，不补写内容）</p>' : '') + '<p class="modal-warn">' + esc(plan.notice || '') + '</p>' + (!plan.confirmed ? '<button id="btn-confirm-fragments" class="btn btn-ghost" type="button">确认阅读顺序清单</button>' : '');
    els.candidates.innerHTML += html;
    var btn = $("btn-confirm-fragments");
    if (btn) btn.addEventListener("click", function () { fetch("/api/fragments/confirm", {method: "POST"}).then(function (r) { return r.json(); }).then(function (d) { toast(d.ok ? "已确认阅读顺序；报告仍保持独立" : (d.error || "确认失败"), d.ok ? "" : "error"); poll(); }); });
  }

  function renderBadge(status) {
    var cur = status.current, cls = "idle", text = "空闲";
    if (status.running) { cls = "run"; text = "编译中"; }
    else if (cur && cur.status === "failed") { cls = "fail"; text = "失败"; }
    else if (cur && cur.status === "done") { cls = "done"; text = "已完成"; }
    els.badge.className = "badge badge-" + cls;
    els.badgeText.textContent = text;
    els.badge.setAttribute("aria-label", "运行状态：" + text);
    if (status.running && status.pid) {
      els.pid.textContent = "pid " + status.pid;
      els.pid.hidden = false;
    } else {
      els.pid.hidden = true;
    }
  }

  function renderTotals(t) {
    els.totals.innerHTML =
      '<div class="stat"><div class="stat-v">' + (t.files_done || 0) +
      '<span class="stat-sub">/ ' + (t.files_pending || 0) + "</span></div>" +
      '<div class="stat-l">已完成文件 / 待处理</div></div>' +
      '<div class="stat"><div class="stat-v">' + (t.events_done || 0) + "</div>" +
      '<div class="stat-l">已完成事件</div></div>' +
      '<div class="stat"><div class="stat-v">' + (t.audio_hours || 0) + "</div>" +
      '<div class="stat-l">音频时长（小时）</div></div>' +
      '<div class="stat' + ((t.events_failed || 0) > 0 ? " stat-fail" : "") + '">' +
      '<div class="stat-v">' + (t.events_failed || 0) + "</div>" +
      '<div class="stat-l">失败事件</div></div>';
  }

  /* 阶段轨道：站点只在 stage_total 变化时重建，其余仅切 class，
     让圆点/连线的颜色过渡得以保留 */
  function buildTrack(total) {
    trackBuilt = total;
    var html = "";
    for (var i = 1; i <= total; i++) {
      html += '<li class="station">' +
        '<span class="dot" aria-hidden="true">' + i + "</span>" +
        '<span class="lbl">' + esc(STAGE_LABELS[i - 1] || "阶段 " + i) + "</span></li>";
    }
    els.track.innerHTML = html;
  }

  function renderTrack(status) {
    var cur = status.current;
    var total = (cur && cur.stage_total) || STAGE_LABELS.length;
    if (trackBuilt !== total) buildTrack(total);
    var idx = cur ? Math.max(1, Math.min(total, cur.stage_index || 1)) : 0;
    var stations = els.track.children;
    for (var i = 0; i < stations.length; i++) {
      var n = i + 1, st = "station";
      if (cur && n < idx) st += " done";
      else if (cur && n === idx) {
        if (cur.status === "failed") st += " fail";
        else if (cur.status === "running") st += " now";
        else if (cur.status === "done") st += " done";
        else st += " halt"; /* stopped：琥珀警示 */
      }
      stations[i].className = st;
    }
    updateDetail();
  }

  function stageName(cur) {
    return (cur && STAGE_KEYS[cur.stage]) ||
      STAGE_LABELS[(cur && cur.stage_index || 1) - 1] || (cur && cur.stage) || "";
  }

  function updateDetail() {
    var cur = lastStatus && lastStatus.current;
    if (!cur) {
      els.detail.innerHTML = '<span class="dim">尚未开始编译</span>';
      els.track.setAttribute("aria-label", "编译阶段轨道，尚未开始");
      return;
    }
    var name = stageName(cur);
    var parts = ['<span class="d-name">' + esc(name) + "</span>"];
    var ss = lastStatus.substage;
    if (ss && typeof ss.done === "number") {
      parts.push('<span class="d-count">' + ss.done +
        (ss.total != null ? " / " + ss.total : "") + "</span>");
    }
    if (cur.status === "running" && cur.stage_started) {
      parts.push('<span class="d-time">已进行 ' + fmtDur(nowSec() - cur.stage_started) + "</span>");
    }
    els.detail.innerHTML = parts.join('<span class="d-sep">·</span>');
    var running = cur.status === "running";
    els.track.setAttribute("aria-label", "编译阶段 " + (cur.stage_index || 1) + "/" +
      (cur.stage_total || STAGE_LABELS.length) + "：" + name + (running ? "，进行中" : ""));
  }

  function findEventStart(event, recent) {
    for (var i = recent.length - 1; i >= 0; i--) {
      var r = recent[i];
      if (r && r.kind === "event_start" && r.event === event && r.ts) return r.ts;
    }
    return null;
  }

  function renderCurrent(status) {
    var cur = status.current;
    if (!cur || !cur.event) { els.eventCard.hidden = true; eventStartTs = null; return; }
    eventStartTs = findEventStart(cur.event, status.recent || []);
    var statusText = { running: "编译中", done: "已完成", failed: "失败", stopped: "已停止" }[cur.status] || cur.status || "—";
    els.eventCard.hidden = false;
    els.eventCard.innerHTML =
      '<div class="ec-head"><span class="ec-name">' + esc(cur.event) + "</span>" +
      '<span class="ec-status ec-' + esc(cur.status || "") + '">' + statusText + "</span></div>" +
      '<div class="ec-msg">' + esc(cur.message || "") + "</div>" +
      '<div class="ec-meta">已运行 <span class="num" id="event-runtime">' +
      fmtDur(nowSec() - (eventStartTs || cur.stage_started || nowSec())) +
      '</span> · 阶段 <span class="num">' + (cur.stage_index || "?") + "/" +
      (cur.stage_total || STAGE_LABELS.length) + "</span></div>";
  }

  function renderQueue(queue) {
    var sig = JSON.stringify(queue);
    if (sig === queueSig) return;
    queueSig = sig;
    if (!queue.length) {
      els.queue.innerHTML = '<li class="empty">队列为空。投放材料后点开始编译。</li>';
      return;
    }
    els.queue.innerHTML = queue.map(function (q) {
      return '<li class="qrow">' +
        '<span class="qname">' + esc(q.name) + "</span>" +
        '<span class="qcounts">录音 ' + (q.audio || 0) +
        (q.video ? " · 录屏 " + q.video : "") +
        " · 笔记 " + (q.notes || 0) + " · 共 " + (q.files || 0) + " 文件</span>" +
        (q.awaiting_region
          ? '<span class="qtag qtag-warn">待确认区域</span>' +
            '<button type="button" class="btn-region" data-event="' + esc(q.name) +
            '">确认幻灯片区域</button>'
          : "") +
        (q.active ? '<span class="qtag"><i class="qdot"></i>处理中</span>' : "") +
        "</li>";
    }).join("");
  }

  function renderTimeline(recent) {
    var rows = recent.slice(-8).reverse(); /* 最近 8 条，新在上 */
    var sig = JSON.stringify(rows);
    if (sig === timelineSig) return;
    timelineSig = sig;
    if (!rows.length) {
      els.timeline.innerHTML = '<li class="empty">暂无记录。</li>';
      return;
    }
    els.timeline.innerHTML = rows.map(function (r) {
      var fail = r.status === "failed" || r.kind === "event_failed";
      var label = STAGE_KEYS[r.stage] ||
        ({ event_start: "开始", event_done: "完成", event_failed: "失败", event_stopped: "停止" }[r.kind]) || "";
      return '<li class="trow' + (fail ? " trow-fail" : "") + '">' +
        '<span class="ttime">[' + fmtClock(r.ts) + "]</span>" +
        (r.event ? ' <span class="tevent">' + esc(r.event) + "</span>" : "") +
        (label ? ' <span class="tlabel">' + esc(label) + "</span>" : "") +
        (r.message ? ' <span class="tmsg">' + esc(r.message) + "</span>" : "") +
        "</li>";
    }).join("");
  }

  function syncControls() {
    var running = !!(lastStatus && lastStatus.running);
    els.btnStop.hidden = !running;
    if (uploading) { els.btnStart.disabled = true; els.btnStart.textContent = "上传中…"; }
    else if (running) { els.btnStart.disabled = true; els.btnStart.textContent = "编译中…"; }
    else { els.btnStart.disabled = false; els.btnStart.textContent = "开始编译"; }
  }

  /* ---- 开始编译：校验 → upload → start ---------------------------------- */

  function startCompile() {
    if (uploading) return;
    if (!sel.audio.length && !sel.notes.length) {
      toast("请先投放录音、录屏或笔记文件", "error");
      return;
    }
    uploading = true;
    syncControls();
    var fd = new FormData();
    var ev = els.eventName.value.trim();
    if (ev) fd.append("event", ev); /* 契约：event 字段必须在文件之前 */
    if (els.fragmentMode && els.fragmentMode.checked) fd.append("fragment_mode", "1");
    var od = els.outputDir.value.trim(); /* 允许 ~ 开头路径，原样传后端 */
    if (od) fd.append("output_dir", od);
    sel.audio.forEach(function (f) { fd.append("audio", f, f.name); });
    sel.notes.forEach(function (f) { fd.append("notes", f, f.name); });

    fetch("/api/upload", { method: "POST", body: fd })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          return { code: res.status, data: data };
        });
      })
      .then(function (r) {
        if (r.code !== 201) {
          toast(r.data.error || "上传失败（HTTP " + r.code + "）", "error");
          return null;
        }
        toast("已入队：" + (r.data.event || ev || "事件"));
        sel.audio = []; sel.notes = [];
        renderFileList("audio"); renderFileList("notes");
        els.eventName.value = "";
        return fetch("/api/start", { method: "POST" }).then(function (res) {
          return res.json().catch(function () { return {}; }).then(function (data) {
            return { code: res.status, data: data };
          });
        });
      })
      .then(function (r) {
        if (!r) return;
        if (r.code === 202) {
          if (r.data.message) toast(r.data.message);
          poll();
        } else if (r.code === 409 && Array.isArray(r.data.awaiting_region)) {
          /* 录屏区域门禁（规格 §3.6）：服务端 message 已点名待确认事件 */
          toast(r.data.message ||
            "录屏区域未确认：" + r.data.awaiting_region.join("、"), "error");
          poll();
        } else {
          toast(r.data.error || r.data.message || "启动失败（HTTP " + r.code + "）", "error");
        }
      })
      .catch(function (err) {
        toast("网络错误：" + (err && err.message ? err.message : err), "error");
      })
      .then(function () {
        uploading = false;
        syncControls();
      });
  }

  function stopCompile() {
    if (!window.confirm("确定停止当前编译？未完成的阶段将被中断。")) return;
    fetch("/api/stop", { method: "POST" })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          if (res.status === 202) {
            if (data.message) toast(data.message);
          } else {
            toast(data.error || data.message || "停止失败（HTTP " + res.status + "）", "error");
          }
          poll();
        });
      })
      .catch(function (err) {
        toast("网络错误：" + (err && err.message ? err.message : err), "error");
      });
  }

  /* ---- 输出目录：原生文件夹选择 ---------------------------------------- */

  function pickOutputDir() {
    if (pickOutputDir.busy) return;
    pickOutputDir.busy = true;
    var old = els.pickDir.textContent;
    els.pickDir.disabled = true;
    els.pickDir.textContent = "选择中…";
    /* 用户在系统对话框里翻文件夹可能很久：给 300s（服务端原生 choose folder）*/
    var ctl = new AbortController();
    var timer = setTimeout(function () { ctl.abort(); }, 300000);
    fetch("/api/pick-folder", { method: "POST", signal: ctl.signal })
      .then(function (r) { return r.json().then(function (d) { return d; }); })
      .then(function (data) {
        if (data && data.path) {
          els.outputDir.value = data.path;
        } else if (data && data.error) {
          toast(data.error, "error");
        }
        /* cancelled：用户点了取消，保持原值，不提示 */
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") {
          toast("选择超时，请手动输入路径", "error");
        } else {
          toast("网络错误：" + (err && err.message ? err.message : err), "error");
        }
      })
      .then(function () {
        clearTimeout(timer);
        pickOutputDir.busy = false;
        els.pickDir.disabled = false;
        els.pickDir.textContent = old;
      });
  }

  /* ---- 录屏区域确认弹窗（规格 §3.6；detect 是 ≤120s 慢端点：仅由用户点击
     触发、在途不双发；轮询绝不自动打 detect）-------------------------------- */

  var MIN_BOX = 0.02;   /* 框选/缩放的最小宽高（相对值）；server 端还会再 clamp */

  var region = {
    open: false, event: null,
    detecting: false, confirming: false,
    confirmTarget: null,          /* "adopt" | "rect"：哪个按钮在提交中 */
    rect: null,                   /* 当前检测框 {x,y,w,h} 相对坐标 0-1；null=无框 */
    reliable: false,              /* detect 是否给出可靠自动结果（决定「采纳」可用） */
    hasPreview: false,
    returnFocus: null             /* 打开弹窗前的焦点元素；关闭时归还 */
  };
  var dragMode = null;            /* 进行中的拖动：move | resize | draw */

  function captureReturnFocus() {
    region.returnFocus = document.activeElement || null;
  }
  function restoreReturnFocus() {
    var el = region.returnFocus;
    region.returnFocus = null;
    /* 队列卡片可能已被轮询重绘换节点：仅当元素仍在文档中才归还焦点 */
    if (el && el.isConnected && el.focus) el.focus();
  }

  function clamp01(v) { return Math.min(1, Math.max(0, v)); }
  function copyRect(r) { return { x: r.x, y: r.y, w: r.w, h: r.h }; }
  function validRect(r) { return !!(r && r.w > 0 && r.h > 0); }
  function normRect(r) {
    /* server 的 rect 应已在 0-1；防御性收拢（与 server _clamp_rect 同向：
       先夹到 [0,1] 再收缩 w/h 保证不出框） */
    if (!r) return null;
    var x = clamp01(+r.x || 0), y = clamp01(+r.y || 0);
    var w = Math.min(clamp01(+r.w || 0), 1 - x);
    var h = Math.min(clamp01(+r.h || 0), 1 - y);
    return w > 0 && h > 0 ? { x: x, y: y, w: w, h: h } : null;
  }
  function regionAlive(name) { return region.open && region.event === name; }
  function previewUrlFor(name) {
    return "/api/events/" + encodeURIComponent(name) + "/region_preview.png";
  }

  function showRegionError(msg) {
    els.regionError.textContent = msg;
    els.regionError.hidden = false;
  }
  function clearRegionError() {
    els.regionError.textContent = "";
    els.regionError.hidden = true;
  }
  function setRegionHint(msg) { els.regionHint.textContent = msg || ""; }

  function setRegionWork(on) {
    els.regionWork.hidden = !on;
    if (on) els.regionStage.hidden = true; /* 预览由 loadPreview 就绪后再显示 */
  }

  function syncRegionButtons() {
    var busy = region.detecting || region.confirming;
    els.btnRegionAdopt.disabled = busy || !region.reliable;
    /* 「确认区域」提交的是预览图上的当前框：无图/无框都不可用 */
    els.btnRegionConfirm.disabled =
      busy || !region.hasPreview || !validRect(region.rect);
    els.btnRegionAdopt.textContent =
      region.confirmTarget === "adopt" ? "提交中…" : "采纳自动检测";
    els.btnRegionConfirm.textContent =
      region.confirmTarget === "rect" ? "提交中…" : "确认区域";
  }

  function updateReadout() {
    var r = region.rect;
    els.regionReadout.textContent = validRect(r)
      ? "x " + r.x.toFixed(2) + " · y " + r.y.toFixed(2) +
        " · w " + r.w.toFixed(2) + " · h " + r.h.toFixed(2)
      : "";
  }

  /* 检测框按相对坐标渲染（rect × 舞台百分比；舞台紧贴图片 → 即图片相对坐标） */
  function renderBox() {
    var b = els.regionBox, r = region.rect;
    if (!r || !region.hasPreview) { b.hidden = true; return; }
    b.hidden = false;
    b.style.left = (r.x * 100).toFixed(3) + "%";
    b.style.top = (r.y * 100).toFixed(3) + "%";
    b.style.width = (r.w * 100).toFixed(3) + "%";
    b.style.height = (r.h * 100).toFixed(3) + "%";
  }

  function loadPreview(url, name, onReady) {
    region.hasPreview = false;
    els.regionImg.hidden = true;
    els.regionImg.onload = function () {
      if (!regionAlive(name)) return;
      region.hasPreview = true;
      els.regionImg.hidden = false;
      els.regionStage.hidden = false;
      renderBox();
      syncRegionButtons();   /* 预览就绪 → 「确认区域」按当前框态启用 */
      if (onReady) onReady();
    };
    els.regionImg.onerror = function () { /* 无预览帧：保持错误提示与「重试检测」 */ };
    /* t= 防缓存：同一事件重复 detect 会覆写同名 PNG */
    els.regionImg.src = url + (url.indexOf("?") < 0 ? "?" : "&") + "t=" + Date.now();
  }

  function openRegionModal(name) {
    if (!name) return;
    if (region.confirming) {   /* 与 detecting 分支对称：在途提交给出可见反馈 */
      toast("区域确认正在提交，请稍候再试", "error");
      return;
    }
    if (region.detecting) {
      /* 慢端点在途：同事件 → 恢复弹窗可见；不同事件 → 不并发第二个 detect */
      if (region.event === name) {
        captureReturnFocus();
        region.open = true;
        els.regionModal.hidden = false;
        els.regionX.focus();
      } else {
        toast("另一事件的区域检测正在进行，请稍候再试", "error");
      }
      return;
    }
    captureReturnFocus();
    region.open = true;
    region.event = name;
    region.rect = null;
    region.reliable = false;
    region.hasPreview = false;
    region.confirmTarget = null;
    els.regionEvent.textContent = name;
    els.regionWarn.hidden = true;
    els.btnRegionRetry.hidden = true;
    clearRegionError();
    setRegionHint("");
    updateReadout();
    els.regionImg.onload = null;   /* 上一轮的 load/error 迟到回调不得进入新状态 */
    els.regionImg.onerror = null;
    els.regionImg.removeAttribute("src");
    els.regionImg.hidden = true;
    els.regionModal.hidden = false;
    setRegionWork(true);
    syncRegionButtons();
    els.regionX.focus();
    detectRegion(name);
  }

  function closeRegionModal() {
    if (!region.open) return;
    onDragEnd();   /* 幂等：拖动中途关闭（Esc/遮罩）不残留 dragMode 与 document 监听 */
    region.open = false;
    els.regionModal.hidden = true;
    restoreReturnFocus();
    /* 在途 detect/confirm 的响应因 regionAlive 失败而被忽略，不 abort（server 可能已在跑） */
  }

  function detectRegion(name) {
    if (region.detecting) return;   /* 慢端点不双发 */
    region.detecting = true;
    els.btnRegionRetry.hidden = true;
    clearRegionError();
    setRegionWork(true);
    syncRegionButtons();
    var ctl = new AbortController();
    /* server 120s 会自行返回 504；客户端 150s 兜底（不能复用轮询的 5s 超时） */
    var timer = setTimeout(function () { ctl.abort(); }, 150000);
    fetch("/api/region/detect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event: name }),
      signal: ctl.signal
    })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          return { code: res.status, data: data };
        });
      })
      .then(function (r) {
        if (!regionAlive(name)) return;
        if (r.code === 200) onDetectOk(name, r.data);
        else onDetectFail(name, r.code, r.data);
      })
      .catch(function (err) {
        if (!regionAlive(name)) return;
        onDetectFail(name, 0, {
          error: err && err.name === "AbortError"
            ? "检测请求超时：请重试"
            : "网络错误：" + (err && err.message ? err.message : err)
        });
      })
      .then(function () {
        clearTimeout(timer);
        if (region.event === name) {
          region.detecting = false;
          els.regionWork.hidden = true;
          syncRegionButtons();
        }
      });
  }

  function onDetectOk(name, data) {
    region.reliable = !!data.reliable;
    region.rect = normRect(data.rect);
    if (data.warning) {              /* 「多视频事件仅处理首个」 */
      els.regionWarn.textContent = data.warning;
      els.regionWarn.hidden = false;
    }
    if (region.reliable && region.rect) {
      var conf = typeof data.confidence === "number"
        ? "（置信度 " + Math.round(data.confidence * 100) + "%）" : "";
      setRegionHint("自动检测到幻灯片区域" + conf +
        "：未微调可直接「采纳自动检测」；拖动框体或四角把手微调后请点「确认区域」。");
    } else {
      /* 规格 §5「UI 提示拖框手选」：无可靠自动结果 → 用户在预览图上画框 */
      setRegionHint("未能自动检测到可靠的幻灯片区域：请在预览图上按住鼠标左键拖出幻灯片范围，再点「确认区域」。");
    }
    loadPreview(data.preview_url || previewUrlFor(name), name);
    updateReadout();
    syncRegionButtons();
  }

  function onDetectFail(name, code, data) {
    showRegionError((data && data.error) ||
      "区域检测失败（HTTP " + (code || "网络错误") + "）");
    setRegionHint("");
    els.btnRegionRetry.hidden = false;
    /* 预览帧若已在盘上（此前 detect 留下的）仍允许手动框选（规格 §5 手选路径） */
    loadPreview(previewUrlFor(name), name, function () {
      setRegionHint("预览帧可用：按住鼠标左键在图上拖出幻灯片区域后点「确认区域」，或点「重试检测」。");
    });
    syncRegionButtons();
  }

  function confirmRegion(useRect) {
    if (!region.open || region.detecting || region.confirming) return;
    var name = region.event;
    if (useRect && !validRect(region.rect)) return;
    /* rect 缺省 = 采纳缓存的自动结果（仅 reliable 时合法，server 会再校验） */
    var body = useRect ? { event: name, rect: copyRect(region.rect) } : { event: name };
    region.confirming = true;
    region.confirmTarget = useRect ? "rect" : "adopt";
    syncRegionButtons();
    fetch("/api/region/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          return { code: res.status, data: data };
        });
      })
      .then(function (r) {
        if (!regionAlive(name)) return;
        if (r.code === 200) {
          closeRegionModal();
          toast("已确认幻灯片区域：" + name);
          poll();   /* 立即刷新队列：卡片离开「待确认区域」 */
        } else {
          showRegionError(r.data.error || "确认失败（HTTP " + r.code + "）");
        }
      })
      .catch(function (err) {
        if (!regionAlive(name)) return;
        showRegionError("网络错误：" + (err && err.message ? err.message : err));
      })
      .then(function () {
        if (region.event === name) {
          region.confirming = false;
          region.confirmTarget = null;
          if (region.open) syncRegionButtons();
        }
      });
  }

  /* ---- 检测框拖动 / 四角缩放 / 空白处重新框选（document 级 move/up）------ */

  function stagePos(e) {
    var b = els.regionStage.getBoundingClientRect();
    return {
      x: b.width > 0 ? clamp01((e.clientX - b.left) / b.width) : 0,
      y: b.height > 0 ? clamp01((e.clientY - b.top) / b.height) : 0
    };
  }

  function onBoxMouseDown(e) {
    if (e.button !== 0 || !region.open || !region.rect || !region.hasPreview) return;
    if (region.detecting || region.confirming) return;
    e.preventDefault();
    e.stopPropagation();   /* 不触发舞台的「重新框选」 */
    var corner = e.target && e.target.getAttribute
      ? e.target.getAttribute("data-corner") : null;
    dragMode = { type: corner ? "resize" : "move", corner: corner,
                 start: stagePos(e), rect: copyRect(region.rect) };
    document.addEventListener("mousemove", onDragMove);
    document.addEventListener("mouseup", onDragEnd);
  }

  function onStageMouseDown(e) {
    if (e.button !== 0 || !region.open || !region.hasPreview || dragMode) return;
    if (region.detecting || region.confirming) return;
    e.preventDefault();
    var p = stagePos(e);
    dragMode = { type: "draw", anchor: p,
                 prev: region.rect ? copyRect(region.rect) : null };
    region.rect = { x: p.x, y: p.y, w: 0, h: 0 };
    renderBox();
    updateReadout();
    syncRegionButtons();   /* w=0 → 「确认区域」保持禁用，拖出尺寸后才可用 */
    document.addEventListener("mousemove", onDragMove);
    document.addEventListener("mouseup", onDragEnd);
  }

  function onDragMove(e) {
    if (!dragMode || !region.open) return;
    e.preventDefault();    /* 抑制拖动过程中的文本选择 */
    var p = stagePos(e);
    if (dragMode.type === "move") {
      var r0 = dragMode.rect;
      var x = clamp01(r0.x + (p.x - dragMode.start.x));
      var y = clamp01(r0.y + (p.y - dragMode.start.y));
      region.rect = { x: Math.min(x, 1 - r0.w), y: Math.min(y, 1 - r0.h),
                      w: r0.w, h: r0.h };
    } else if (dragMode.type === "resize") {
      region.rect = resizeRect(dragMode.rect, dragMode.corner, p);
    } else {               /* draw：锚点 → 当前点 */
      var a = dragMode.anchor;
      region.rect = { x: Math.min(a.x, p.x), y: Math.min(a.y, p.y),
                      w: Math.abs(p.x - a.x), h: Math.abs(p.y - a.y) };
    }
    renderBox();
    updateReadout();
  }

  function onDragEnd() {
    document.removeEventListener("mousemove", onDragMove);
    document.removeEventListener("mouseup", onDragEnd);
    if (dragMode && dragMode.type === "draw") {
      var r = region.rect;
      /* 过小视为误点：还原拖动前的框（自动检测结果不因误点丢失；无框保持无框） */
      if (!r || r.w < MIN_BOX || r.h < MIN_BOX) {
        region.rect = dragMode.prev;
        renderBox();
      }
    }
    dragMode = null;
    updateReadout();
    syncRegionButtons();
  }

  function resizeRect(o, corner, p) {
    var x1 = o.x, y1 = o.y, x2 = o.x + o.w, y2 = o.y + o.h;
    if (corner === "nw" || corner === "sw") x1 = Math.min(clamp01(p.x), x2 - MIN_BOX);
    if (corner === "ne" || corner === "se") x2 = Math.max(clamp01(p.x), x1 + MIN_BOX);
    if (corner === "nw" || corner === "ne") y1 = Math.min(clamp01(p.y), y2 - MIN_BOX);
    if (corner === "sw" || corner === "se") y2 = Math.max(clamp01(p.y), y1 + MIN_BOX);
    x1 = Math.max(0, x1); y1 = Math.max(0, y1);
    x2 = Math.min(1, x2); y2 = Math.min(1, y2);
    if (x2 - x1 <= 0 || y2 - y1 <= 0) return o;   /* 退化：保持原框 */
    return { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
  }

  function onQueueClick(e) {
    var btn = e.target && e.target.closest ? e.target.closest(".btn-region") : null;
    if (!btn) return;
    openRegionModal(btn.getAttribute("data-event"));
  }

  function setupRegionModal() {
    els.regionX.addEventListener("click", closeRegionModal);
    els.btnRegionAdopt.addEventListener("click", function () { confirmRegion(false); });
    els.btnRegionConfirm.addEventListener("click", function () { confirmRegion(true); });
    els.btnRegionRetry.addEventListener("click", function () {
      if (region.detecting || !region.event) return;
      clearRegionError();
      detectRegion(region.event);
    });
    /* 点遮罩关闭；用 mousedown 而非 click，避免「弹窗内按下、遮罩上松开」误关 */
    els.regionModal.addEventListener("mousedown", function (e) {
      if (e.target === els.regionModal) closeRegionModal();
    });
    document.addEventListener("keydown", function (e) {
      if ((e.key === "Escape" || e.key === "Esc") && region.open) closeRegionModal();
    });
    els.regionBox.addEventListener("mousedown", onBoxMouseDown);
    els.regionStage.addEventListener("mousedown", onStageMouseDown);
  }

  /* ---- 轮询（1s，5s 超时；断连横幅）------------------------------------ */

  var inFlight = false;

  function poll() {
    if (consoleDead || inFlight) return;
    inFlight = true;
    var ctl = new AbortController();
    var timer = setTimeout(function () { ctl.abort(); }, 5000);
    fetch("/api/status", { signal: ctl.signal, cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (status) {
        els.offline.hidden = true;
        render(status);
        if (window.MSTResults) {
          try { window.MSTResults.update(status); } catch (e) { /* results.js 异常不拖垮轮询 */ }
        }
      })
      .catch(function () {
        els.offline.hidden = false;
      })
      .then(function () {
        clearTimeout(timer);
        inFlight = false;
      });
  }

  /* 已耗时类数字每秒自刷新（不必等下一次轮询） */
  setInterval(function () {
    if (!lastStatus) return;
    var cur = lastStatus.current;
    if (cur && cur.status === "running") updateDetail();
    var el = document.getElementById("event-runtime");
    if (el && eventStartTs) el.textContent = fmtDur(nowSec() - eventStartTs);
  }, 1000);

  /* ---- 页面内关闭服务 ---------------------------------------------------- */

  function shutdownConsole(force) {
    fetch(force ? "/api/shutdown?force=1" : "/api/shutdown",
          { method: "POST", headers: { "X-MST-Shutdown": "yes" } })
      .then(function (r) {
        return r.json().then(function (j) { return { code: r.status, body: j }; });
      })
      .then(function (res) {
        if (res.code === 409) {          /* 编译进行中 → 二次确认后强制 */
          if (confirm("编译正在进行。\n确定停止编译并关闭服务吗？（该事件的输入会保留在 input/ 供重试）"))
            shutdownConsole(true);
          return;
        }
        if (res.code !== 200) { toast("关闭失败：" + (res.body.error || res.code)); return; }
        markConsoleDead();
      })
      .catch(function () {               /* 服务已死：同样进入已关闭态 */
        markConsoleDead();
      });
  }

  function markConsoleDead() {
    consoleDead = true;
    els.offline.hidden = true;           /* 不再显示"连接已断开" */
    if (els.btnQuit) {
      els.btnQuit.disabled = true;
      els.btnQuit.textContent = "✓ 已关闭";
      els.btnQuit.classList.add("quit-done");
    }
    if (els.quitHint) els.quitHint.hidden = false;
  }

  /* ---- 启动 ------------------------------------------------------------- */

  setupZone("drop-audio", "pick-audio", "audio");
  setupZone("drop-notes", "pick-notes", "notes");
  $("list-audio").addEventListener("click", onRemoveClick);
  $("list-notes").addEventListener("click", onRemoveClick);
  els.queue.addEventListener("click", onQueueClick);
  setupRegionModal();
  els.btnStart.addEventListener("click", startCompile);
  els.btnStop.addEventListener("click", stopCompile);
  if (els.pickDir) els.pickDir.addEventListener("click", pickOutputDir);
  if (els.btnQuit) els.btnQuit.addEventListener("click", function () {
    if (consoleDead) return;
    if (confirm("关闭本地控制台？\n关闭后本页失效；重新启动请运行：mst"))
      shutdownConsole(false);
  });

  buildTrack(STAGE_LABELS.length);
  renderTotals({});
  renderQueue([]);
  renderTimeline([]);

  if (window.MSTResults) {
    try { window.MSTResults.init(document.getElementById("results-root")); } catch (e) { /* 同上 */ }
  }

  poll();
  setInterval(poll, 1000);
})();
