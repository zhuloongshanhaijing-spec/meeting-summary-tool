/* 会议知识编译台 — 前端控制器（零依赖，离线可用）
 *
 * 职责：文件投放（拖拽+选择双通道）→ /api/upload → /api/start；
 * 每 1s 轮询 /api/status 驱动徽标 / 总计 / 12 站阶段轨道 / 队列 / 时间线；
 * 结果包区内容交给 MSTResults（results.js，与本文件并行加载）。
 */
(function () {
  "use strict";

  /* 12 阶段（run_meeting.STAGE_ORDER 契约；stage_index 顺序） */
  var STAGE_LABELS = [
    "文件清单", "音频预处理（降噪）", "语言探测", "语音识别",
    "组装逐句记录", "生成证据", "无关话语过滤", "主题提取与索引",
    "claim 保真审计", "笔记佐证", "构建报告包", "验证与质量门禁"
  ];
  /* stage 原始键 → 中文（时间线行与当前阶段详情用；键是流水线稳定契约） */
  var STAGE_KEYS = {
    inventory: "文件清单", audio_prepare: "音频预处理（降噪）", lang_probe: "语言探测",
    asr: "语音识别", literal: "组装逐句记录", evidence: "生成证据",
    relevance: "无关话语过滤", reconcile: "主题提取与索引", audit: "claim 保真审计",
    notes: "笔记佐证", package: "构建报告包", validate: "验证与质量门禁",
    "asr.segment": "语音识别 · 分段", "asr.whisper": "语音识别 · Whisper", "asr.qwen": "语音识别 · Qwen"
  };
  var AUDIO_EXTS = { m4a: 1, mp3: 1, wav: 1, aac: 1, flac: 1, aiff: 1, caf: 1 };
  var NOTE_EXTS = { md: 1, markdown: 1, txt: 1 };

  var $ = function (id) { return document.getElementById(id); };
  var els = {
    offline: $("offline"), pid: $("pid"), badge: $("badge"), badgeText: $("badge-text"),
    totals: $("totals"), track: $("track"), detail: $("track-detail"),
    eventCard: $("event-card"), queue: $("queue"), timeline: $("timeline"),
    eventName: $("event-name"), outputDir: $("output-dir"),
    pickDir: $("btn-pick-dir"),
    btnStart: $("btn-start"), btnStop: $("btn-stop"), toasts: $("toasts")
  };

  var sel = { audio: [], notes: [] };   /* 已选待上传文件 */
  var uploading = false;                /* 上传/启动流程占用主按钮 */
  var lastStatus = null;                /* 最近一次 /api/status */
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
      if (!exts[extOf(f.name)]) { ignored++; return; }
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
      return '<li class="frow">' +
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
    syncControls();
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
    var total = (cur && cur.stage_total) || 12;
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
      (cur.stage_total || 12) + "：" + name + (running ? "，进行中" : ""));
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
      '</span> · 阶段 <span class="num">' + (cur.stage_index || "?") + "/" + (cur.stage_total || 12) + "</span></div>";
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
        '<span class="qcounts">录音 ' + (q.audio || 0) + " · 笔记 " + (q.notes || 0) +
        " · 共 " + (q.files || 0) + " 文件</span>" +
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
      toast("请先投放录音或笔记文件", "error");
      return;
    }
    uploading = true;
    syncControls();
    var fd = new FormData();
    var ev = els.eventName.value.trim();
    if (ev) fd.append("event", ev); /* 契约：event 字段必须在文件之前 */
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

  /* ---- 轮询（1s，5s 超时；断连横幅）------------------------------------ */

  var inFlight = false;

  function poll() {
    if (inFlight) return;
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

  /* ---- 启动 ------------------------------------------------------------- */

  setupZone("drop-audio", "pick-audio", "audio");
  setupZone("drop-notes", "pick-notes", "notes");
  $("list-audio").addEventListener("click", onRemoveClick);
  $("list-notes").addEventListener("click", onRemoveClick);
  els.btnStart.addEventListener("click", startCompile);
  els.btnStop.addEventListener("click", stopCompile);
  if (els.pickDir) els.pickDir.addEventListener("click", pickOutputDir);

  buildTrack(12);
  renderTotals({});
  renderQueue([]);
  renderTimeline([]);

  if (window.MSTResults) {
    try { window.MSTResults.init(document.getElementById("results-root")); } catch (e) { /* 同上 */ }
  }

  poll();
  setInterval(poll, 1000);
})();
