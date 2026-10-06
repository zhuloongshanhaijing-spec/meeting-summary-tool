/* 碎片装配工作台 —— 零框架、零构建、零 CDN。
   数据源：GET /api/status 的 assembly 字段（v2 装配计划 + 本机构建/确认状态）。
   职责：渲染分组与衔接分界 → 试听衔接（前尾8s+后头8s）→ 接受/标记缺口 → 调序 → 确认。
   铁律：本页只读流水线产物；唯一写入是确认侧车（POST /api/assembly/confirm）。 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var state = {
    plan: null,            /* 原始计划（generated_ts 判定重生成） */
    groups: [],            /* [{ids:[...]}] 用户可调 */
    marks: {},             /* "left>right" -> "ok" | "gap" */
    edited: false,
    building: false,
    confirmed: false
  };
  var audition = null;     /* [audioA, audioB] 当前试听 */

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function toast(msg, kind) {
    var box = $("a-toasts");
    if (!box) return;
    var el = document.createElement("p");
    el.className = kind === "error" ? "modal-error" : "";
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(function () { el.remove(); }, 4000);
  }
  function fmtDur(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    var m = Math.floor(sec / 60), s = sec % 60;
    return m + "分" + (s < 10 ? "0" : "") + s + "秒";
  }
  function frag(id) {
    var list = (state.plan && state.plan.fragments) || [];
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
    return null;
  }
  function confLabel(c) {
    return c === "high" ? "置信 高" : (c === "medium" ? "置信 中" : "置信 低");
  }
  function relationLabel(j) {
    if (!j) return "人工调整后的新衔接";
    if (j.relation === "same_continuous") return "候选连续";
    if (j.relation === "same_gap") return "同段会议 · 中间疑似缺段";
    if (j.relation === "same_reorder") return "同段会议 · 原始顺序不明";
    return "不同会议";
  }
  function audioUrl(id, source) {
    return "/api/audio/" + encodeURIComponent(id) + "/" + encodeURIComponent(source);
  }
  function junctionKey(l, r) { return l + ">" + r; }

  /* ---- 试听：前段尾 8s → 后段头 8s -------------------------------------- */
  function stopAudition() {
    if (!audition) return;
    audition.forEach(function (a) { try { a.pause(); } catch (e) {} });
    Array.prototype.forEach.call(document.querySelectorAll(".asm-listen"), function (b) {
      b.classList.remove("active"); b.textContent = "试听衔接";
    });
    audition = null;
  }
  function playJunction(btn, leftId, rightId) {
    stopAudition();
    var L = frag(leftId), R = frag(rightId);
    if (!L || !R) { toast("片段数据不完整，无法试听", "error"); return; }
    var a1 = new Audio(audioUrl(L.id, L.source));
    var a2 = new Audio(audioUrl(R.id, R.source));
    audition = [a1, a2];
    btn.classList.add("active"); btn.textContent = "试听中…（点此停止）";
    var leftTailStart = Math.max(0, (L.duration_seconds || 0) - 8);
    var switched = false;
    function switchToRight() {
      if (switched) return;
      switched = true;
      a1.pause();
      a2.currentTime = 0;
      a2.play().catch(function () { toast("后段音频播放失败", "error"); stopAudition(); });
    }
    a1.addEventListener("timeupdate", function () {
      if (!switched && (L.duration_seconds || 0) > 0 &&
          a1.currentTime >= (L.duration_seconds || 0) - 0.3) switchToRight();
    });
    a1.addEventListener("ended", switchToRight);
    a2.addEventListener("timeupdate", function () {
      if (a2.currentTime >= 8) stopAudition();
    });
    a2.addEventListener("ended", function () { if (switched) stopAudition(); });
    a1.addEventListener("error", function () { toast("前段音频加载失败（缺 runs 源文件？）", "error"); stopAudition(); });
    a2.addEventListener("error", function () { if (switched) { toast("后段音频加载失败", "error"); stopAudition(); } });
    if ((L.duration_seconds || 0) > 8) {
      a1.addEventListener("loadedmetadata", function () {
        try { a1.currentTime = leftTailStart; } catch (e) {}
      });
    }
    a1.play().catch(function () { toast("音频播放失败（浏览器拦截？再点一次）", "error"); stopAudition(); });
  }

  /* ---- 渲染 ------------------------------------------------------------ */
  function render() {
    var plan = state.plan;
    var stateText = $("a-state-text"), stateBadge = $("a-state");
    if (!plan || plan.status === "absent") {
      stateText.textContent = "无计划";
      $("a-groups").innerHTML = '<p class="dim">几段打乱的会议录音，在这里还原成各自的完整记录——回编译台上传散乱片段并完成编译，然后点「生成装配计划」。</p>';
      $("a-notice").textContent = "";
      $("a-meta").textContent = "";
      return;
    }
    stateText.textContent = state.confirmed ? "已确认" : (state.building ? "分析中…" : "待审核");
    stateBadge.className = "badge " + (state.confirmed ? "badge-done" : (state.building ? "badge-run" : "badge-idle"));
    $("a-notice").textContent = plan.notice || "";
    var bits = ["LLM 辅助: " + (plan.llm_used ? "已用（" + esc(plan.llm_model || "") + "）"
      : "未用" + (plan.llm_skipped_reason ? "（" + esc(plan.llm_skipped_reason) + "）" : "")),
      "分组依据: " + (plan.group_order_basis === "note" ? "笔记顺序"
        : plan.group_order_basis === "mixed" ? "笔记+默认" : "字母序（无可靠线索）"),
      "片段: " + ((plan.fragments || []).length)];
    if ((plan.references || []).length) bits.push("参考笔记: " + esc(plan.references.join("、")));
    $("a-meta").textContent = bits.join(" · ");

    var html = "";
    state.groups.forEach(function (g, gi) {
      var meta = groupMeta(g.gid);
      html += '<div class="asm-group"><div class="asm-group-head">' +
        '<span class="asm-group-title">第 ' + (gi + 1) + ' 组</span>' +
        '<span class="asm-conf asm-conf-' + esc(meta.confidence) + '">' + confLabel(meta.confidence) + '</span>' +
        (meta.note ? '<span class="asm-group-note">' + esc(meta.note) + '</span>' : "") +
        (meta.cycle ? '<span class="asm-cycle">顺序有环冲突，已保守排序</span>' : "") +
        "</div>";
      g.ids.forEach(function (id, fi) {
        var f = frag(id);
        html += fragRow(id, f, gi, fi, g.ids.length);
        if (fi < g.ids.length - 1) html += junctionRow(g.ids[fi], g.ids[fi + 1]);
      });
      html += "</div>";
    });
    if ((plan.skipped || []).length) {
      html += '<p class="asm-skipped">未能纳入装配的片段: ' +
        esc(plan.skipped.map(function (s) { return s.id + "（" + s.reason + "）"; }).join("、")) + "</p>";
    }
    if ((plan.degraded_pairs || []).length) {
      html += '<p class="asm-degraded">有 ' + plan.degraded_pairs.length +
        " 处衔接因模型超时/输出异常降级为词面兜底判断（junction 已如实标注 source=fallback），请以试听为准。</p>";
    }
    $("a-groups").innerHTML = html;
    wire();
  }

  function groupMeta(gid) {
    var list = (state.plan && state.plan.groups) || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].group_id === gid) {
        return { confidence: list[i].confidence || "low",
                 note: list[i].note_order_source,
                 cycle: !!list[i].cycle_conflict };
      }
    }
    return { confidence: "low", note: null, cycle: false };
  }

  function fragRow(id, f, gi, fi, n) {
    if (!f) return '<div class="asm-frag"><span class="asm-frag-name">' + esc(id) +
      '</span><span class="dim">（数据缺失）</span></div>';
    var hint = f.note_hint_index != null ? ' · 笔记线索: ' + esc(f.note_hint_source || "笔记") + " #" + (f.note_hint_index + 1) : "";
    return '<div class="asm-frag">' +
      '<span class="asm-frag-name">' + esc(f.source) + '</span>' +
      '<span class="asm-frag-dur num">' + fmtDur(f.duration_seconds) + " · " + (f.sentence_count || "?") + " 句</span>" +
      (hint ? '<span class="asm-frag-hint">' + hint + "</span>" : "") +
      '<span class="asm-moves">' +
      '<button class="asm-move" type="button" data-move-up="' + esc(id) + '"' + (gi === 0 && fi === 0 ? " disabled" : "") + ' aria-label="上移 ' + esc(f.source) + '">↑</button>' +
      '<button class="asm-move" type="button" data-move-down="' + esc(id) + '"' + (gi === state.groups.length - 1 && fi === n - 1 ? " disabled" : "") + ' aria-label="下移 ' + esc(f.source) + '">↓</button>' +
      (n > 1 ? '<button class="asm-move asm-split" type="button" data-split="' + esc(id) + '" aria-label="把 ' + esc(f.source) + ' 拆分为独立一组">拆分</button>' : "") +
      "</span></div>";
  }

  function junctionRow(leftId, rightId) {
    var j = findJunction(leftId, rightId);
    var mark = state.marks[junctionKey(leftId, rightId)];
    var cls = "asm-junction" + (mark === "gap" ? " gap" : (mark === "ok" ? " accepted" : "")) +
      (j && j.gap && !mark ? " gap" : "");
    var summary = "";
    if (j && (j.left_summary || j.right_summary)) {
      summary = '<p class="asm-summaries">前段结尾: <span class="q">' + esc(j.left_summary || "（无摘要）") +
        '</span> ｜ 后段开头: <span class="q">' + esc(j.right_summary || "（无摘要）") + "</span></p>";
    }
    var srcNote = "";
    if (!j) srcNote = '<span class="asm-junction-manual">人工调整后的新衔接（无本地分析数据，请试听核实）</span>';
    else if (j.source === "fallback") srcNote = '<span class="asm-junction-manual">词面兜底判断（模型不可用），请以试听为准</span>';
    return '<div class="' + cls + '"><div class="asm-junction-head">' +
      '<span class="asm-junction-label">— 分界 —</span>' +
      '<span>' + relationLabel(j) + "</span>" +
      (j && j.gap && mark !== "gap" ? '<span class="asm-junction-gapflag">⚠ 疑似缺段</span>' : "") +
      srcNote +
      "</div>" + summary +
      '<div class="asm-junction-actions">' +
      '<button class="asm-jmark asm-listen" type="button" data-listen="' + esc(leftId) + "|" + esc(rightId) + '">试听衔接</button>' +
      '<button class="asm-jmark' + (mark === "ok" ? " on-ok" : "") + '" type="button" data-mark="ok" data-jk="' + esc(junctionKey(leftId, rightId)) + '">接受</button>' +
      '<button class="asm-jmark' + (mark === "gap" ? " on-gap" : "") + '" type="button" data-mark="gap" data-jk="' + esc(junctionKey(leftId, rightId)) + '">标记缺口</button>' +
      "</div></div>";
  }

  function findJunction(l, r) {
    var list = (state.plan && state.plan.junctions) || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].left === l && list[i].right === r) return list[i];
    }
    return null;
  }

  /* ---- 交互 ------------------------------------------------------------ */
  function locate(id) {
    for (var gi = 0; gi < state.groups.length; gi++) {
      var fi = state.groups[gi].ids.indexOf(id);
      if (fi >= 0) return { g: gi, f: fi };
    }
    return null;
  }
  function moveFragment(id, dir) {
    var pos = locate(id);
    if (!pos) return;
    var g = state.groups[pos.g];
    var target = pos.f + dir;
    if (target >= 0 && target < g.ids.length) {  /* 组内交换 */
      g.ids[pos.f] = g.ids[target];
      g.ids[target] = id;
    } else {                                     /* 跨组移动 */
      var tg = pos.g + dir;
      if (tg < 0 || tg >= state.groups.length) return;
      g.ids.splice(pos.f, 1);
      var removedEmpty = !g.ids.length;
      if (removedEmpty) state.groups.splice(pos.g, 1);
      if (dir < 0) {
        state.groups[pos.g - 1].ids.push(id);    /* 上移出组 → 接到上一组末尾 */
      } else {
        var nextIndex = removedEmpty ? pos.g : pos.g + 1;
        state.groups[nextIndex].ids.unshift(id); /* 下移出组 → 插到下一组开头 */
      }
    }
    state.edited = true;
    render();
  }

  /* 把一个片段从所在组拆出，成为紧随其后的独立组（修正模型的错误合并）。*/
  function splitFragment(id) {
    var pos = locate(id);
    if (!pos) return;
    var g = state.groups[pos.g];
    if (!g || g.ids.length < 2) return;
    g.ids.splice(pos.f, 1);
    var fresh = { gid: "u" + Date.now(), ids: [id] };
    if (g.ids.length === 0) state.groups.splice(pos.g, 1, fresh);
    else state.groups.splice(pos.g + 1, 0, fresh);
    state.edited = true;
    render();
  }

  function wire() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-move-up]"), function (b) {
      b.addEventListener("click", function () { stopAudition(); moveFragment(b.getAttribute("data-move-up"), -1); });
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-move-down]"), function (b) {
      b.addEventListener("click", function () { stopAudition(); moveFragment(b.getAttribute("data-move-down"), 1); });
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-split]"), function (b) {
      b.addEventListener("click", function () { stopAudition(); splitFragment(b.getAttribute("data-split")); });
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-listen]"), function (b) {
      b.addEventListener("click", function () {
        if (audition) { stopAudition(); return; }
        var pair = b.getAttribute("data-listen").split("|");
        playJunction(b, pair[0], pair[1]);
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-mark]"), function (b) {
      b.addEventListener("click", function () {
        var key = b.getAttribute("data-jk"), kind = b.getAttribute("data-mark");
        state.marks[key] = (state.marks[key] === kind) ? undefined : kind;
        if (state.marks[key] === undefined) delete state.marks[key];
        render();
      });
    });
  }

  function adoptPlan(plan) {
    state.plan = plan;
    state.groups = (plan.groups || []).map(function (g) {
      return { gid: g.group_id, ids: (g.fragment_ids || []).slice() };
    });
    state.marks = {};
    state.edited = false;
  }

  function refresh(first) {
    fetch("/api/status").then(function (r) { return r.json(); }).then(function (d) {
      var plan = d.assembly || { status: "absent" };
      state.building = !!plan.building;
      state.confirmed = !!plan.confirmed;
      $("a-banner").hidden = !plan.error && !state.building;
      if (plan.error) {
        $("a-banner").hidden = false;
        $("a-banner-title").textContent = "装配分析出错";
        $("a-banner-text").textContent = plan.error;
      } else if (state.building) {
        $("a-banner").hidden = false;
        $("a-banner-title").textContent = "正在分析";
        $("a-banner-text").textContent = "本地装配分析进行中（含模型判断，可能需要几分钟）…完成后本页自动更新。";
      }
      if (plan.status === "absent") {
        state.plan = null;
        render();
        return;
      }
      if (first || !state.plan || state.plan.generated_ts !== plan.generated_ts) {
        if (state.edited && !first) {
          toast("计划已重新生成；你的手动调整未套用，如需应用请刷新页面", "error");
        } else {
          adoptPlan(plan);
        }
      } else {
        state.plan = plan;  /* 刷新 error/building 等附随字段 */
      }
      render();
    }).catch(function () {
      $("a-state-text").textContent = "离线";
    });
  }

  $("a-rebuild").addEventListener("click", function () {
    fetch("/api/assembly/build", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (d) { toast(d.message || d.error || "已提交", d.error ? "error" : ""); refresh(false); })
      .catch(function () { toast("无法启动本地装配分析", "error"); });
  });
  $("a-confirm").addEventListener("click", function () {
    var ids = [];
    state.groups.forEach(function (g) { ids = ids.concat(g.ids); });
    fetch("/api/assembly/confirm", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fragment_ids: ids, junction_marks: state.marks })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.ok) { $("a-confirm-msg").textContent = "已确认。阅读顺序已记录（逐句原文未改动）。"; }
      else { $("a-confirm-msg").textContent = d.error || "确认失败"; }
      refresh(false);
    }).catch(function () { $("a-confirm-msg").textContent = "网络错误，确认未保存"; });
  });

  refresh(true);
  setInterval(function () { refresh(false); }, 3000);
})();
