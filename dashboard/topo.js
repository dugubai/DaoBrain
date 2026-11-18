/* DaoBrain 架构拓扑·实时运行 —— 整体式架构图渲染器（纯 SVG，动态流转）。
 * DbTopo.build(data) -> SVG 字符串（可单测）; DbTopo.mount({api,box,meta,interval}) -> 取数+渲染+轮询。
 * 同源 fetch，不依赖任何外部库；供大盘 /、/arch 与中台共用。 */
(function (global) {
  "use strict";

  var COL = {           // group -> [描边色, 底色, 文字强调]
    local: ["#23765d", "#e9f4ef", "#23765d"],
    cloud: ["#315cce", "#e9effc", "#315cce"],
    red:   ["#ab3545", "#fbecef", "#ab3545"],
    core:  ["#916015", "#fff5e2", "#916015"],
    infra: ["#556274", "#eef1f5", "#556274"],
    aux:   ["#6b7280", "#f1f3f6", "#6b7280"],
    edge:  ["#7d9b86", "#eef4ef", "#5c7d67"]
  };
  var DEF = COL.aux;
  var NW = 152, NH = 56, CGAP = 78, RGAP = 24, PAD = 26, HEADER = 30;

  function esc(x) {
    return (x == null ? "" : String(x)).replace(/[&<>]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c];
    });
  }
  function col(g) { return COL[g] || DEF; }

  // ---- 布局：按 layer 分列，列内按 group 权重竖排 ----
  function layout(data) {
    var layers = (data.layers || []).map(function (L) { return L.layer; });
    if (!layers.length) { Object.keys({}); layers = []; var s = {}; (data.nodes || []).forEach(function (n) { s[n.layer] = 1; }); layers = Object.keys(s).map(Number).sort(function (a, b) { return a - b; }); }
    var byL = {};
    (data.nodes || []).forEach(function (n) { (byL[n.layer] = byL[n.layer] || []).push(n); });
    var pos = {}, maxRows = 0, cx = PAD;
    var cols = layers.filter(function (l) { return byL[l]; });
    cols.forEach(function (l) {
      var arr = byL[l].slice().sort(function (a, b) { return String(a.group) < String(b.group) ? -1 : 1; });
      maxRows = Math.max(maxRows, arr.length);
      var y = PAD + HEADER, x = cx;
      arr.forEach(function (n) { pos[n.service] = { x: x, y: y, n: n }; y += NH + RGAP; });
      cx += NW + CGAP;
    });
    var W = cx - CGAP + PAD, H = PAD + HEADER + maxRows * (NH + RGAP) + PAD;
    return { pos: pos, cols: cols, W: W, H: H };
  }

  // ---- 节点 ----
  function nodeSvg(p) {
    var n = p.n, c = col(n.group), x = p.x, y = p.y;
    var up = !!n.up, remote = n.host === "主机";
    var dot = !up ? "#ab3545" : (n.state === "restarting" ? "#c9a13b" : "#23765d");
    var dash = remote ? ' stroke-dasharray="4 3"' : "";
    var hostTxt = remote ? "主机" : "备机";
    var meta, bars = "";
    if (remote) {
      meta = "跨机仅探活 · " + esc(n.status || "");
    } else {
      var cpu = n.cpu == null ? 0 : Math.min(100, n.cpu), memp = n.mem_percent == null ? 0 : Math.min(100, n.mem_percent);
      var cpuHot = cpu >= 70, memHot = memp >= 80;
      meta = "↻" + (n.restarts == null ? 0 : n.restarts) + " · " + (n.net_mbps == null ? 0 : n.net_mbps) + "MB/s · pid" + (n.pids == null ? 0 : n.pids);
      bars =
        bar(x + 10, y + 30, "CPU", cpu, n.cpu == null ? "—" : n.cpu + "%", cpuHot) +
        bar(x + 80, y + 30, "MEM", memp, n.mem_mb == null ? "—" : Math.round(n.mem_mb) + "MB", memHot);
    }
    return '<g class="dbarc-node' + (up ? "" : " off") + '">' +
      '<rect x="' + x + '" y="' + y + '" width="' + NW + '" height="' + NH + '" rx="10" fill="' + c[1] + '" stroke="' + c[0] + '" stroke-width="1.4"' + dash + '/>' +
      '<circle cx="' + (x + 12) + '" cy="' + (y + 14) + '" r="4.5" fill="' + dot + '"/>' +
      '<text class="dbarc-lbl" x="' + (x + 22) + '" y="' + (y + 18) + '" fill="' + c[2] + '">' + esc(n.label) + '</text>' +
      '<text class="dbarc-sub" x="' + (x + 12) + '" y="' + (y + 28) + '" fill="#8a8172">' + hostTxt + ":" + (n.port || "") + " · " + esc(n.state || "") + "</text>" +
      (remote ? '<text class="dbarc-meta" x="' + (x + 12) + '" y="' + (y + 44) + '" fill="#8a8172">' + meta + "</text>" : bars) +
      (remote ? "" : '<text class="dbarc-meta" x="' + (x + 12) + '" y="' + (y + NH - 2) + '" fill="#a89f8e">' + meta + "</text>") +
      "</g>";
  }
  function bar(x, y, lab, pct, txt, hot) {
    var w = 54, h = 5;
    var fill = hot ? "#c0685a" : "#315cce";
    return '<text x="' + x + '" y="' + y + '" font-size="8.5" fill="#8a8172">' + lab + "</text>" +
      '<rect x="' + x + '" y="' + (y + 3) + '" width="' + w + '" height="' + h + '" rx="2.5" fill="#e6e9f0"/>' +
      '<rect x="' + x + '" y="' + (y + 3) + '" width="' + (w * Math.max(0, Math.min(100, pct)) / 100) + '" height="' + h + '" rx="2.5" fill="' + fill + '"/>' +
      '<text x="' + (x + w + 4) + '" y="' + (y + 8) + '" font-size="8.5" fill="#66758b">' + esc(txt) + "</text>";
  }

  // ---- 边（贝塞尔 + 流向动画 + 标签）----
  function edgeSvg(e, pos) {
    var a = pos[e.from], b = pos[e.to];
    if (!a || !b) return "";
    var x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2;
    var d, mx, my;
    if (a.x === b.x) {
      // 同列（如 gateway→hybrid_iface、gateway_cloud→orchestrator）走右侧沟道，避免穿框
      var gx = a.x + NW + 30;
      d = "M" + x1 + " " + y1 + " C" + gx + " " + y1 + "," + gx + " " + y2 + "," + (b.x + NW) + " " + y2;
      mx = gx + 2; my = (y1 + y2) / 2;
      // 终点接到目标右边缘外侧
      x2 = b.x + NW;
    } else if (b.x < a.x) {
      // 回流：目标在左列，走顶部沟道
      var top = Math.min(a.y, b.y) - 24;
      d = "M" + x1 + " " + y1 + " C" + x1 + " " + top + "," + x2 + " " + top + "," + x2 + " " + y2;
      mx = (x1 + x2) / 2; my = top + 3;
    } else {
      // 前向：源右缘 -> 目标左缘，横向 S 形贝塞尔
      var midx = (x1 + x2) / 2;
      d = "M" + x1 + " " + y1 + " C" + midx + " " + y1 + "," + midx + " " + y2 + "," + x2 + " " + y2;
      mx = midx; my = (y1 + y2) / 2;
    }
    var sig = e.signal || 0, on = sig > 0;
    var w = on ? Math.min(4, 1.3 + Math.log10(sig + 1) * 1.1) : 1.1;
    var dur = on ? Math.max(0.7, 3.2 - Math.log10(sig + 1) * 1.1) : 0;
    var color = on ? col(a.n.group)[0] : "#c3cbd6";
    var lab = esc(e.label) + (on ? " · " + sig + "/min" : " · 0/min");
    var lw = lab.length * 6.2 + 12;
    return '<g class="dbarc-edge">' +
      '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="' + w + '" stroke-opacity="' + (on ? 0.28 : 0.4) + '" marker-end="url(#dbarc-arrow' + (on ? "" : "-faint") + ')"/>' +
      (on ? '<path class="dbarc-flow" d="' + d + '" fill="none" stroke="' + color + '" stroke-width="' + w + '" stroke-dasharray="4 16" stroke-dashoffset="0" style="animation-duration:' + dur + 's"/>' : "") +
      '<rect class="dbarc-labbg" x="' + (mx - lw / 2) + '" y="' + (my - 9) + '" width="' + lw + '" height="14" rx="7" fill="#ffffff" stroke="' + color + '" stroke-opacity="0.35"/>' +
      '<text class="dbarc-lab" x="' + mx + '" y="' + (my + 1.5) + '" text-anchor="middle" fill="' + color + '">' + lab + "</text>" +
      "</g>";
  }

  function build(data) {
    var L = layout(data), pos = L.pos;
    var head = (data.layers || []).map(function (x) { return { layer: x.layer, title: x.title }; });
    var cols = {};
    Object.keys(pos).forEach(function (k) { var p = pos[k]; cols[p.n.layer] = Math.min(cols[p.n.layer] == null ? 1e9 : cols[p.n.layer], p.x); });
    var hdr = "";
    head.forEach(function (H) {
      if (cols[H.layer] == null) return;
      hdr += '<text class="dbarc-colh" x="' + (cols[H.layer] + NW / 2) + '" y="' + (PAD + HEADER - 12) + '" text-anchor="middle" fill="#8a8172">' + esc(H.title) + "</text>";
    });
    var edges = "", nodes = "";
    (data.edges || []).forEach(function (e) { edges += edgeSvg(e, pos); });
    Object.keys(pos).forEach(function (k) { nodes += nodeSvg(pos[k]); });
    return '<svg class="dbarc" viewBox="0 0 ' + L.W + " " + L.H + '" width="100%" height="' + L.H + '" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMin meet">' +
      '<defs>' +
      '<marker id="dbarc-arrow" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto"><path d="M0 0 L9 4.5 L0 9 z" fill="context-stroke"/></marker>' +
      '<marker id="dbarc-arrow-faint" markerWidth="7" markerHeight="7" refX="5" refY="3.5" orient="auto"><path d="M0 0 L7 3.5 L0 7 z" fill="#c3cbd6"/></marker>' +
      '<pattern id="dbarc-grid" width="26" height="26" patternUnits="userSpaceOnUse"><path d="M26 0 H0 V26" fill="none" stroke="#ece7db" stroke-width="1"/></pattern>' +
      "</defs>" +
      '<rect x="0" y="0" width="' + L.W + '" height="' + L.H + '" fill="url(#dbarc-grid)"/>' +
      hdr + edges + nodes + "</svg>";
  }

  function css() {
    if (document.getElementById("dbarc-style")) return;
    var st = document.createElement("style"); st.id = "dbarc-style";
    st.textContent =
      ".dbarc{font-family:inherit;max-width:100%}" +
      ".dbarc text{font-family:'Songti SC','SimSun',-apple-system,'PingFang SC',sans-serif}" +
      ".dbarc-lbl{font-size:12.5px;font-weight:700}" +
      ".dbarc-sub,.dbarc-meta{font-size:9px}" +
      ".dbarc-colh{font-size:11px;letter-spacing:1px}" +
      ".dbarc-lab{font-size:9.5px;font-weight:600}" +
      ".dbarc-labbg{stroke-width:1}" +
      ".dbarc-node.off{opacity:.55}" +
      ".dbarc-flow{animation-name:dbarc-dash;animation-timing-function:linear;animation-iteration-count:infinite}" +
      "@keyframes dbarc-dash{to{stroke-dashoffset:-40}}" +
      "@media(prefers-reduced-motion:reduce){.dbarc-flow{animation:none;opacity:.9}}";
    document.head.appendChild(st);
  }

  function metaText(d) {
    var eng = d.engine || {}, f = (d.flow && d.flow.per_min) || {}, c = (d.flow && d.flow.counts) || {};
    return "备机容器 " + (eng.containers_running || 0) + "/" + (eng.containers_total || 0) +
      " · " + (eng.cpus || "?") + "核/" + (eng.mem_total_gb || "?") + "GB · 节点 " + (d.nodes || []).length +
      " · 本地 " + (f.local || 0) + "/min · 公有云 " + (f.cloud || 0) + "/min · 绿 " + (f.green || 0) + "/min · 红 " + (f.red || 0) + "/min" +
      (d.cycle_seconds ? " · 采集" + d.cycle_seconds + "s" : "") +
      (d.collector_error ? " · 采集告警 " + d.collector_error : "");
  }

  function mount(o) {
    css();
    var box = document.getElementById(o.box), meta = o.meta && document.getElementById(o.meta);
    function load() {
      fetch(o.api, { credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (r) { if (!r.ok) throw new Error("http" + r.status); return r.json(); })
        .then(function (d) {
          if (box) box.innerHTML = build(d);
          if (meta) meta.textContent = metaText(d);
        })
        .catch(function () { if (box) box.innerHTML = '<div class="dbarch-empty">实时拓扑数据暂不可用（跨机隧道或大盘服务异常，请稍后重试）</div>'; });
    }
    load();
    if (o.interval !== 0) setInterval(load, o.interval || 6000);
    return load;
  }

  global.DbTopo = { build: build, mount: mount, layout: layout, metaText: metaText };
})(typeof window !== "undefined" ? window : globalThis);
