/* results.js — 结果包浏览模块（无框架、无外部资源、离线可用）
 *
 * 集成契约：
 *   index.html 在 app.js 之前加载本文件；
 *   app.js 启动时调用 MSTResults.init(document.getElementById('results-root'))；
 *   每秒轮询后调用 MSTResults.update(status)（status 为 /api/status 的 JSON）。
 *
 * 只暴露 window.MSTResults = { init, update }，其余全部封闭在 IIFE 内。
 * 样式经 <style id="mst-results-style"> 注入（不依赖宿主 style.css）。
 */
(function () {
  'use strict';

  var API = '/api/outputs';
  var FETCH_TIMEOUT_MS = 5000;

  /* ================================================================
   * 一、迷你 Markdown 渲染器（纯函数 renderMarkdown(text) → HTML 字符串）
   *
   * 安全模型（硬要求）：入口处一次性完成 HTML 转义（& < > " '），
   * 之后所有格式化都作用在已转义文本上；任何分支都不会把原始
   * 字符重新引入。块引用等递归复用 renderBlocks（已转义行），
   * 绝不二次转义、也绝不绕过转义。
   * ================================================================ */

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /* 行内格式：`code`（先摘出保护）→ **加粗** → *斜体* → 还原 code */
  function renderInline(s) {
    var codes = [];
    s = s.replace(/`([^`]+)`/g, function (_, c) {
      codes.push(c);
      return '\u0000C' + (codes.length - 1) + '\u0000';
    });
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/\*([^*\s][^*]*?)\*/g, '<em>$1</em>');
    s = s.replace(/\u0000C(\d+)\u0000/g, function (_, i) {
      return '<code>' + codes[+i] + '</code>';
    });
    return s;
  }

  function splitTableRow(line) {
    var t = line.trim();
    if (t.charAt(0) === '|') { t = t.slice(1); }
    if (t.charAt(t.length - 1) === '|') { t = t.slice(0, -1); }
    return t.split('|').map(function (c) { return c.trim(); });
  }

  function isSeparatorRow(cells) {
    return cells.length > 0 && cells.every(function (c) {
      return /^:?-{3,}:?$/.test(c);
    });
  }

  /* 行首是否是另一种块的起点（段落收集时用来停笔） */
  function isBlockStart(line) {
    return /^(#{1,3})\s/.test(line)
      || /^\s*-{3,}\s*$/.test(line)
      || /^\s*&gt;/.test(line)
      || /^\s*[-*+]\s+/.test(line)
      || /^\s*\d+(?:[.)]\s|、)/.test(line)
      || /^\s*\|/.test(line);
  }

  /* renderBlocks：输入是“已转义”的行数组 */
  function renderBlocks(lines) {
    var out = [];
    var i = 0;
    var n = lines.length;
    while (i < n) {
      var line = lines[i];

      if (/^\s*$/.test(line)) { i++; continue; }            // 空行：分段

      var h = line.match(/^(#{1,3})\s+(.*)$/);              // # ## ### 标题
      if (h) {
        out.push('<h' + h[1].length + '>' + renderInline(h[2].trim())
                 + '</h' + h[1].length + '>');
        i++; continue;
      }

      if (/^\s*-{3,}\s*$/.test(line)) {                      // --- 水平线
        out.push('<hr>');
        i++; continue;
      }

      if (/^\s*&gt;/.test(line)) {                           // > 引用（'>' 已转义）
        var q = [];
        while (i < n && /^\s*&gt;/.test(lines[i])) {
          q.push(lines[i].replace(/^\s*&gt;\s?/, ''));
          i++;
        }
        out.push('<blockquote>' + renderBlocks(q) + '</blockquote>');
        continue;
      }

      if (/^\s*\|/.test(line) && i + 1 < n) {                // 表格
        var head = splitTableRow(line);
        if (isSeparatorRow(splitTableRow(lines[i + 1]))) {
          var rows = [];
          var j = i + 2;
          while (j < n && /^\s*\|/.test(lines[j])) {
            rows.push(splitTableRow(lines[j]));
            j++;
          }
          var html = ['<table><thead><tr>'];
          head.forEach(function (c) {
            html.push('<th>' + renderInline(c) + '</th>');
          });
          html.push('</tr></thead><tbody>');
          rows.forEach(function (r) {
            html.push('<tr>');
            for (var c = 0; c < head.length; c++) {
              html.push('<td>' + renderInline(r[c] || '') + '</td>');
            }
            html.push('</tr>');
          });
          html.push('</tbody></table>');
          out.push(html.join(''));
          i = j; continue;
        }
      }

      var li = line.match(/^\s*[-*+]\s+(.*)$/);              // 无序列表
      if (li) {
        var items = [];
        while (i < n) {
          var m = lines[i].match(/^\s*[-*+]\s+(.*)$/);
          if (!m) { break; }
          items.push('<li>' + renderInline(m[1]) + '</li>');
          i++;
        }
        out.push('<ul>' + items.join('') + '</ul>');
        continue;
      }

      var oi = line.match(/^\s*\d+(?:[.)]\s+|、\s*)(.*)$/); // 有序列表（1. / 1) / 1、）
      if (oi) {
        var oitems = [];
        while (i < n) {
          var om = lines[i].match(/^\s*\d+(?:[.)]\s+|、\s*)(.*)$/);
          if (!om) { break; }
          oitems.push('<li>' + renderInline(om[1]) + '</li>');
          i++;
        }
        out.push('<ol>' + oitems.join('') + '</ol>');
        continue;
      }

      var para = [line.trim()];                              // 普通段落
      i++;
      while (i < n && !/^\s*$/.test(lines[i]) && !isBlockStart(lines[i])) {
        para.push(lines[i].trim());
        i++;
      }
      out.push('<p>' + para.map(renderInline).join('<br>') + '</p>');
    }
    return out.join('\n');
  }

  function renderMarkdown(text) {
    return renderBlocks(escapeHtml(text == null ? '' : text)
      .split(/\r\n|\r|\n/));
  }

  /* -- 渲染器加载即自测（零依赖，console.assert） ---------------- */
  (function selfTest() {
    var t1 = renderMarkdown('# 标题\n\n正文 **加粗** 与 `code`');
    console.assert(t1.indexOf('<h1>') === 0 && t1.indexOf('<strong>加粗</strong>') !== -1
                   && t1.indexOf('<code>code</code>') !== -1,
                   'MSTResults renderMarkdown 自测: 标题/加粗/行内代码');
    var t2 = renderMarkdown('<script>alert(1)</script> & "q" \'');
    console.assert(t2.indexOf('<script') === -1 && t2.indexOf('&lt;script&gt;') !== -1
                   && t2.indexOf('&amp;quot;') === -1 && t2.indexOf('&quot;q&quot;') !== -1,
                   'MSTResults renderMarkdown 自测: HTML 全量转义且只转义一次');
    var t3 = renderMarkdown('| a | b |\n|---|---|\n| 1 | 2 |');
    console.assert(t3.indexOf('<table>') !== -1 && t3.indexOf('<th>a</th>') !== -1
                   && t3.indexOf('<td>2</td>') !== -1,
                   'MSTResults renderMarkdown 自测: 表格');
    var t4 = renderMarkdown('- 甲\n- 乙\n\n1. 第一\n\n> 引用\n\n---\n\n***x***');
    console.assert(t4.indexOf('<ul>') !== -1 && t4.indexOf('<ol>') !== -1
                   && t4.indexOf('<blockquote>') !== -1 && t4.indexOf('<hr>') !== -1,
                   'MSTResults renderMarkdown 自测: 列表/引用/水平线');
  }());

  /* ================================================================
   * 二、小工具
   * ================================================================ */

  function el(tag, cls) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    return node;
  }

  /* 全部 fetch 统一走这里：AbortController 5s 超时，覆盖到 body 读完 */
  function fetchUrl(url, consume) {
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, FETCH_TIMEOUT_MS);
    var p = fetch(url, { signal: ctrl.signal }).then(function (resp) {
      if (!resp.ok) { throw new Error('HTTP ' + resp.status); }
      return consume ? consume(resp) : resp;
    });
    p.then(function () { clearTimeout(timer); },
           function () { clearTimeout(timer); });
    return p;
  }

  function fmtDate(mtimeSec) {                     // mtime → YYYY-MM-DD HH:MM（本地时区）
    var d = new Date(mtimeSec * 1000);
    if (isNaN(d.getTime())) { return ''; }
    function p(x) { return (x < 10 ? '0' : '') + x; }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
           + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function humanSize(n) {
    if (typeof n !== 'number' || isNaN(n)) { return ''; }
    if (n < 1024) { return n + ' B'; }
    var units = ['KB', 'MB', 'GB', 'TB'];
    var v = n;
    for (var i = 0; i < units.length; i++) {
      v = v / 1024;
      if (v < 1024 || i === units.length - 1) {
        return (v >= 100 ? Math.round(v) : Math.round(v * 10) / 10) + ' ' + units[i];
      }
    }
  }

  function basename(p) { return String(p).split('/').pop(); }

  function rawUrl(event, file) {
    return API + '/' + encodeURIComponent(event) + '/raw?path='
           + encodeURIComponent(file);
  }

  /* ================================================================
   * 三、状态与渲染
   * ================================================================ */

  var state = {
    root: null,
    listEl: null,      // 卡片列表容器
    readerEl: null,    // 阅读区（懒创建）
    lastSig: null,     // outputs 签名：列表没变就绝不动 DOM（保护展开/阅读状态）
    outputs: [],
    expanded: null,    // 当前展开的事件名
    reader: null,      // 当前阅读区 {event, file}
    filesSeq: 0,       // 异步竞态令牌
    readerSeq: 0
  };

  function ensureStyle() {
    if (document.getElementById('mst-results-style')) { return; }
    var st = document.createElement('style');
    st.id = 'mst-results-style';
    st.textContent = [
      '.mst-results{color:#1C2333;font-size:14px;line-height:1.6;}',
      '.mst-results,.mst-results *{box-sizing:border-box;}',
      '.mst-results-list{display:flex;flex-direction:column;gap:10px;}',
      '.mst-empty{color:#5A6072;padding:18px 2px;font-size:13px;}',
      '.mst-msg{color:#5A6072;padding:6px 14px;font-size:13px;}',
      '.mst-error{color:#8A3B2E;padding:6px 14px;font-size:13px;}',
      '.mst-error a{color:inherit;text-decoration:underline;cursor:pointer;margin-left:8px;}',
      '.mst-card{border:1px solid #D8DAE3;border-radius:6px;background:#FAFAF7;overflow:hidden;}',
      '.mst-card-head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;',
      '  padding:10px 14px;cursor:pointer;user-select:none;}',
      '.mst-card-head:hover{background:#F3F3EE;}',
      '.mst-card-head:focus-visible{outline:1px solid #1C2333;outline-offset:-1px;}',
      '.mst-card-name{font-weight:600;flex:1;min-width:8em;',
      '  overflow-wrap:anywhere;}',
      '.mst-card-date,.mst-card-count{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,',
      '  "Liberation Mono",monospace;font-size:12px;color:#5A6072;white-space:nowrap;}',
      '.mst-card-body{border-top:1px solid #D8DAE3;padding:4px 0 8px;}',
      '.mst-file{display:flex;justify-content:space-between;align-items:baseline;',
      '  gap:12px;padding:5px 14px;}',
      '.mst-file-name{min-width:0;overflow-wrap:anywhere;text-align:left;}',
      'button.mst-file-link{background:none;border:none;padding:0;font:inherit;',
      '  color:#1C2333;cursor:pointer;text-decoration:underline;',
      '  text-decoration-color:#D8DAE3;text-underline-offset:3px;}',
      'button.mst-file-link:hover{text-decoration-color:#1C2333;}',
      'a.mst-file-link{color:#1C2333;text-decoration:underline;',
      '  text-decoration-color:#D8DAE3;text-underline-offset:3px;}',
      'a.mst-file-link:hover{text-decoration-color:#1C2333;}',
      '.mst-file-size{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,',
      '  "Liberation Mono",monospace;font-size:12px;color:#5A6072;white-space:nowrap;}',
      '.mst-zip{font-size:12px;color:#1C2333;text-decoration:none;white-space:nowrap;',
      '  border:1px solid #D8DAE3;border-radius:4px;padding:2px 10px;}',
      '.mst-zip:hover{background:#F3F3EE;}',
      '.mst-reader-top{display:flex;justify-content:space-between;align-items:center;',
      '  gap:12px;padding:6px 0 10px;border-bottom:1px solid #D8DAE3;margin-bottom:16px;}',
      '.mst-reader-title{font-size:12px;color:#5A6072;min-width:0;',
      '  overflow-wrap:anywhere;}',
      '.mst-reader-close{background:none;border:1px solid #D8DAE3;border-radius:4px;',
      '  color:#1C2333;font-size:15px;line-height:1;width:28px;height:26px;cursor:pointer;}',
      '.mst-reader-close:hover{background:#F3F3EE;}',
      '.mst-reader-content{max-width:74ch;font-size:14.5px;line-height:1.8;}',
      '.mst-md h1,.mst-md h2,.mst-md h3{line-height:1.4;margin:1.4em 0 .55em;}',
      '.mst-md h1{font-size:1.5em;padding-bottom:.3em;border-bottom:1px solid #D8DAE3;}',
      '.mst-md h2{font-size:1.25em;padding-bottom:.25em;border-bottom:1px solid #D8DAE3;}',
      '.mst-md h3{font-size:1.08em;}',
      '.mst-md p{margin:.7em 0;}',
      '.mst-md ul,.mst-md ol{margin:.7em 0;padding-left:1.5em;}',
      '.mst-md li{margin:.25em 0;}',
      '.mst-md blockquote{margin:.9em 0;padding:2px 0 2px 14px;',
      '  border-left:2px solid #D8DAE3;color:#5A6072;}',
      '.mst-md hr{border:0;border-top:1px solid #D8DAE3;margin:1.2em 0;}',
      '.mst-md code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,',
      '  "Liberation Mono",monospace;font-size:.88em;background:#F1F1EC;',
      '  border-radius:3px;padding:1px 5px;}',
      '.mst-md table{border-collapse:collapse;margin:1em 0;font-size:13px;',
      '  display:block;overflow-x:auto;max-width:100%;}',
      '.mst-md th,.mst-md td{border:1px solid #D8DAE3;padding:5px 10px;',
      '  text-align:left;vertical-align:top;}',
      '.mst-md th{background:#F1F1EC;font-weight:600;}'
    ].join('\n');
    document.head.appendChild(st);
  }

  function msgRow(text) {
    var row = el('div', 'mst-msg');
    row.textContent = text;
    return row;
  }

  function errRow(onRetry) {                      // 行内错误 + 可选“重试”
    var row = el('div', 'mst-error');
    row.textContent = '加载失败，请重试';
    if (onRetry) {
      var a = el('a');
      a.textContent = '重试';
      a.addEventListener('click', function (e) { e.preventDefault(); onRetry(); });
      row.appendChild(a);
    }
    return row;
  }

  /* -- 列表 -- */

  function renderList() {
    var list = state.listEl;
    list.textContent = '';
    if (!state.outputs.length) {
      var empty = el('div', 'mst-empty');
      empty.textContent = '还没有结果包。完成一次编译后，这里会出现可回查的报告。';
      list.appendChild(empty);
      return;
    }
    state.outputs.forEach(function (o) {
      list.appendChild(renderCard(o));
    });
  }

  function renderCard(o) {
    var open = state.expanded === o.name;
    var card = el('div', 'mst-card');
    var head = el('div', 'mst-card-head');
    head.setAttribute('role', 'button');
    head.setAttribute('tabindex', '0');
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
    var name = el('div', 'mst-card-name');
    name.textContent = o.name;
    name.title = o.name;
    var date = el('div', 'mst-card-date');
    date.textContent = fmtDate(o.mtime);
    var count = el('div', 'mst-card-count');
    count.textContent = (o.files == null ? '—' : String(o.files)) + ' 个文件';
    head.appendChild(name);
    head.appendChild(date);
    head.appendChild(count);
    if (open) {                                    // 展开卡片头部：「下载 zip」
      var zip = el('a', 'mst-zip');
      zip.href = API + '/' + encodeURIComponent(o.name) + '/zip';
      zip.setAttribute('download', o.name + '.zip');
      zip.textContent = '下载 zip';
      zip.addEventListener('click', function (e) { e.stopPropagation(); });
      head.appendChild(zip);
    }
    head.addEventListener('click', function () { toggleCard(o.name); });
    head.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        toggleCard(o.name);
      }
    });
    card.appendChild(head);
    if (open) {
      var body = el('div', 'mst-card-body');
      card.appendChild(body);
      loadFiles(o.name, body);
    }
    return card;
  }

  function toggleCard(name) {
    state.expanded = (state.expanded === name) ? null : name;
    state.filesSeq++;                             // 使在途请求作废
    renderList();
  }

  function loadFiles(eventName, bodyEl) {
    var seq = ++state.filesSeq;
    bodyEl.appendChild(msgRow('正在读取文件列表…'));
    fetchUrl(API + '/' + encodeURIComponent(eventName) + '/files',
             function (r) { return r.json(); })
      .then(function (data) {
        if (seq !== state.filesSeq || !bodyEl.isConnected) { return; }
        renderFiles(eventName, (data && data.files) || [], bodyEl);
      })
      .catch(function () {
        if (seq !== state.filesSeq || !bodyEl.isConnected) { return; }
        bodyEl.textContent = '';
        bodyEl.appendChild(errRow(function () { loadFiles(eventName, bodyEl); }));
      });
  }

  function renderFiles(eventName, files, bodyEl) {
    bodyEl.textContent = '';
    if (!files.length) {
      bodyEl.appendChild(msgRow('（没有文件）'));
      return;
    }
    files.forEach(function (f) {
      var row = el('div', 'mst-file');
      var nm = el('div', 'mst-file-name');
      if (/\.(md|markdown|txt)$/i.test(f.name)) {  // 可读文件 → 内嵌阅读区
        var btn = el('button', 'mst-file-link');
        btn.type = 'button';
        btn.textContent = f.name;
        btn.title = '阅读 ' + f.name;
        btn.addEventListener('click', function () { openReader(eventName, f.name); });
        nm.appendChild(btn);
      } else {                                     // 其他类型 → 下载行
        var a = el('a', 'mst-file-link');
        a.href = rawUrl(eventName, f.name);
        a.setAttribute('download', basename(f.name));
        a.textContent = f.name;
        nm.appendChild(a);
      }
      var sz = el('div', 'mst-file-size');
      sz.textContent = humanSize(f.size);
      row.appendChild(nm);
      row.appendChild(sz);
      bodyEl.appendChild(row);
    });
  }

  /* -- 阅读区 -- */

  function ensureReaderEl() {
    if (!state.readerEl) {
      state.readerEl = el('div', 'mst-reader');
      state.readerEl.style.display = 'none';
      state.root.appendChild(state.readerEl);
    }
  }

  function openReader(eventName, fileName) {
    state.reader = { event: eventName, file: fileName };
    renderReader();
  }

  function renderReader() {
    var r = state.reader;
    if (!r) { return; }
    ensureReaderEl();
    var seq = ++state.readerSeq;

    var top = el('div', 'mst-reader-top');
    var title = el('div', 'mst-reader-title');
    title.textContent = r.event + ' / ' + r.file;
    var close = el('button', 'mst-reader-close');
    close.type = 'button';
    close.setAttribute('aria-label', '关闭阅读区');
    close.textContent = '×';
    close.addEventListener('click', closeReader);
    top.appendChild(title);
    top.appendChild(close);

    var content = el('div', 'mst-reader-content');
    content.appendChild(msgRow('正在载入…'));

    state.readerEl.textContent = '';
    state.readerEl.appendChild(top);
    state.readerEl.appendChild(content);
    state.listEl.style.display = 'none';          // 阅读时收起列表
    state.readerEl.style.display = '';

    fetchUrl(rawUrl(r.event, r.file), function (resp) { return resp.text(); })
      .then(function (text) {
        if (seq !== state.readerSeq) { return; }
        content.textContent = '';
        var art = el('div', 'mst-md');
        art.innerHTML = renderMarkdown(text);     // 安全：渲染器入口已全量转义
        content.appendChild(art);
      })
      .catch(function () {
        if (seq !== state.readerSeq) { return; }
        content.textContent = '';
        content.appendChild(errRow(renderReader));
      });
  }

  function closeReader() {
    state.reader = null;
    state.readerSeq++;
    if (state.readerEl) { state.readerEl.style.display = 'none'; }
    if (state.listEl) { state.listEl.style.display = ''; }   // × 返回列表
  }

  /* ================================================================
   * 四、对外 API
   * ================================================================ */

  function init(rootEl) {
    if (!rootEl) { return; }
    ensureStyle();
    state.root = rootEl;
    state.root.classList.add('mst-results');
    state.root.textContent = '';
    state.listEl = el('div', 'mst-results-list');
    state.root.appendChild(state.listEl);
    state.readerEl = null;
    state.lastSig = null;
    state.outputs = [];
    state.expanded = null;
    closeReader();
  }

  function update(statusObj) {
    if (!state.root || !state.listEl) { return; }
    var outs = (statusObj && Array.isArray(statusObj.outputs))
      ? statusObj.outputs : [];
    var sig = outs.map(function (o) {
      return o.name + ':' + o.mtime + ':' + o.files;
    }).join('|');
    if (sig === state.lastSig) { return; }        // 没变化：不打扰用户当前状态
    state.lastSig = sig;
    state.outputs = outs;

    var alive = {};
    outs.forEach(function (o) { alive[o.name] = true; });
    if (state.reader && !alive[state.reader.event]) { closeReader(); }
    if (state.expanded && !alive[state.expanded]) { state.expanded = null; }
    renderList();
  }

  window.MSTResults = { init: init, update: update };
}());
