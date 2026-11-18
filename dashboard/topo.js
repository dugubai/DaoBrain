/* DaoBrain 架构拓扑·实时运行 —— 自上而下的分层架构图渲染器（纯 SVG，动态流转）。
 * 每个架构层 = 一条横向泳道带（①接入 ②④分发编排 ③⑦⑧分级检索 ⑤⑥⑪模型推理 ⑨⑩核验门禁 …），
 * 组件在带内横向排布，数据流按 docs/architecture.md 逐组件纵向流转；回流走右侧总线、基础设施走左侧总线。
 * 边按 req/min 拥堵档着色（仿导航路况）。DbTopo.build(data)->SVG; DbTopo.mount({api,box,meta,interval}).
 * 同源 fetch，不依赖外部库；供大盘 /、/arch 与中台共用。 */
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
  // 几何（自上而下泳道）
  var NW = 172, NH = 66, IGAP = 22, PAD = 24, HEADER = 40;
  var LABEL_W = 132, RIGHT_GUT = 128, BGAP = 62, LEGEND_H = 40;
  var GROUP_ORDER = { edge: 0, local: 1, green: 1, cloud: 2, red: 3, core: 4, infra: 5, aux: 6 };

  function esc(x) {
    return (x == null ? "" : String(x)).replace(/[&<>]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c];
    });
  }
  function col(g) { return COL[g] || DEF; }

  // ---- 路况拥堵配色：按链路 req/min 分档（绝对阈值，稳定不闪烁）----
  var CONG = [
    { hi: 15,   color: "#1f9d55", name: "畅通",   range: "<15" },
    { hi: 40,   color: "#e0a400", name: "缓行",   range: "15–40" },
    { hi: 70,   color: "#e8730c", name: "拥堵",   range: "40–70" },
    { hi: 1e12, color: "#d13b3b", name: "严重拥堵", range: "≥70" }
  ];
  function congestion(sig) {
    sig = sig || 0;
    if (sig <= 0) return { color: "#c3cbd6", name: "空闲", range: "0" };
    for (var i = 0; i < CONG.length; i++) { if (sig < CONG[i].hi) return CONG[i]; }
    return CONG[CONG.length - 1];
  }

  // ---- 布局：layer -> 横向泳道带；带内按 group 权重横向居中排布 ----
  function layout(data) {
    var titles = {};
    (data.layers || []).forEach(function (L) { titles[L.layer] = L.title; });
    var byL = {};
    (data.nodes || []).forEach(function (n) { (byL[n.layer] = byL[n.layer] || []).push(n); });
    var bands = Object.keys(byL).map(Number).sort(function (a, b) { return a - b; });
    var maxCount = bands.reduce(function (m, l) { return Math.max(m, byL[l].length); }, 1);
    var contentW = maxCount * NW + (maxCount - 1) * IGAP;
    var W = PAD + LABEL_W + contentW + RIGHT_GUT + PAD;
    var pos = {}, bandMeta = [];
    bands.forEach(function (l, i) {
      var arr = byL[l].slice().sort(function (a, b) {
        var ga = GROUP_ORDER[a.group] == null ? 9 : GROUP_ORDER[a.group];
        var gb = GROUP_ORDER[b.group] == null ? 9 : GROUP_ORDER[b.group];
        return ga !== gb ? ga - gb : String(a.service) < String(b.service) ? -1 : 1;
      });
      var by = PAD + HEADER + i * (NH + BGAP);
      var spanW = arr.length * NW + (arr.length - 1) * IGAP;
      var x0 = PAD + LABEL_W + (contentW - spanW) / 2;
      arr.forEach(function (n, j) {
        pos[n.service] = { x: x0 + j * (NW + IGAP), y: by, band: i, layer: l, n: n };
      });
      bandMeta.push({ layer: l, title: titles[l] || ("层 " + l), y: by, i: i, x0: PAD, x1: W - PAD });
    });
    var H = PAD + HEADER + Math.max(0, bands.length - 1) * (NH + BGAP) + NH + PAD;
    return { pos: pos, bandMeta: bandMeta, W: W, H: H, titles: titles };
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
        bar(x + 12, y + 42, "CPU", cpu, n.cpu == null ? "—" : n.cpu + "%", cpuHot) +
        bar(x + 96, y + 42, "MEM", memp, n.mem_mb == null ? "—" : Math.round(n.mem_mb) + "MB", memHot);
    }
    return '<g class="dbarc-node' + (up ? "" : " off") + '">' +
      '<rect x="' + x + '" y="' + y + '" width="' + NW + '" height="' + NH + '" rx="10" fill="' + c[1] + '" stroke="' + c[0] + '" stroke-width="1.4"' + dash + '/>' +
      '<circle cx="' + (x + 12) + '" cy="' + (y + 15) + '" r="4.5" fill="' + dot + '"/>' +
      '<text class="dbarc-lbl" x="' + (x + 22) + '" y="' + (y + 18) + '" fill="' + c[2] + '">' + esc(n.label) + '</text>' +
      '<text class="dbarc-sub" x="' + (x + 12) + '" y="' + (y + 30) + '" fill="#8a8172">' + hostTxt + ":" + (n.port || "") + " · " + esc(n.state || "") + "</text>" +
      (remote ? '<text class="dbarc-meta" x="' + (x + 12) + '" y="' + (y + 46) + '" fill="#8a8172">' + meta + "</text>" : bars) +
      (remote ? "" : '<text class="dbarc-meta" x="' + (x + 12) + '" y="' + (y + 62) + '" fill="#a89f8e">' + meta + "</text>") +
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

  // ---- 边（按泳道层差选择走线：前向下/左侧基建总线/右侧回流总线/同带下探）----
  function edgeSvg(e, pos, W) {
    var a = pos[e.from], b = pos[e.to];
    if (!a || !b) return "";
    var acx = a.x + NW / 2, acy = a.y + NH / 2, bcx = b.x + NW / 2, bcy = b.y + NH / 2;
    var da = a.band, db = b.band;
    var d, mx, my;
    if (db > da) {
      var y1 = a.y + NH, y2 = b.y, delta = db - da;
      if (delta <= 2) {
        my = (y1 + y2) / 2;
        d = "M" + acx + " " + y1 + " C" + acx + " " + my + "," + bcx + " " + my + "," + bcx + " " + y2;
        mx = (acx + bcx) / 2;
      } else {
        var gxL = PAD + LABEL_W / 2;
        d = "M" + acx + " " + y1 + " C" + gxL + " " + (y1 + 22) + "," + gxL + " " + (y2 - 22) + "," + bcx + " " + y2;
        mx = gxL; my = (y1 + y2) / 2;
      }
    } else if (db < da) {
      var gxR = W - PAD - RIGHT_GUT / 2, sx = a.x + NW, tx = b.x + NW;
      d = "M" + sx + " " + acy + " C" + gxR + " " + acy + "," + gxR + " " + bcy + "," + tx + " " + bcy;
      mx = gxR; my = (acy + bcy) / 2;
    } else {
      var lx, rx, dipY = a.y + NH + 16;
      if (b.x >= a.x) { lx = a.x + NW; rx = b.x; } else { lx = a.x; rx = b.x + NW; }
      d = "M" + lx + " " + acy + " C" + ((lx + rx) / 2) + " " + dipY + "," + ((lx + rx) / 2) + " " + dipY + "," + rx + " " + acy;
      mx = (lx + rx) / 2; my = dipY;
    }
    var sig = e.signal || 0, on = sig > 0;
    var cg = congestion(sig);
    var w = on ? Math.min(5, 1.5 + Math.log10(sig + 1) * 1.25) : 1.1;
    var dur = on ? Math.max(0.7, 3.2 - Math.log10(sig + 1) * 1.1) : 0;
    var color = on ? cg.color : "#c3cbd6";
    var lab = esc(e.label) + (on ? " · " + sig + "/min · " + cg.name : "");
    var lw = lab.length * 6.2 + 12;
    return '<g class="dbarc-edge">' +
      '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="' + w + '" stroke-opacity="' + (on ? 0.5 : 0.42) + '" marker-end="url(#dbarc-arrow' + (on ? "" : "-faint") + ')"/>' +
      (on ? '<path class="dbarc-flow" d="' + d + '" fill="none" stroke="' + color + '" stroke-width="' + w + '" stroke-dasharray="4 16" stroke-dashoffset="0" style="animation-duration:' + dur + 's"/>' : "") +
      '<rect class="dbarc-labbg" x="' + (mx - lw / 2) + '" y="' + (my - 9) + '" width="' + lw + '" height="14" rx="7" fill="#ffffff" stroke="' + color + '" stroke-opacity="0.35"/>' +
      '<text class="dbarc-lab" x="' + mx + '" y="' + (my + 1.5) + '" text-anchor="middle" fill="' + color + '">' + lab + "</text>" +
      "</g>";
  }

  // ---- 泳道带背景 + 左侧层标签 ----
  function bandsSvg(bandMeta) {
    var s = "";
    bandMeta.forEach(function (bd, i) {
      var top = bd.y - 16, h = NH + 32;
      var fill = (i % 2 === 0) ? "#f3f6fa" : "#faf7ef";
      s += '<rect x="' + bd.x0 + '" y="' + top + '" width="' + (bd.x1 - bd.x0) + '" height="' + h + '" rx="12" fill="' + fill + '" stroke="#e4e6dc" stroke-width="0.8"/>';
      s += '<text class="dbarc-tier" x="' + (bd.x0 + 12) + '" y="' + (bd.y + NH / 2 - 2) + '" fill="#4c5a6b">' + esc(bd.title) + "</text>";
      s += '<text class="dbarc-tier-no" x="' + (bd.x0 + 12) + '" y="' + (bd.y + NH / 2 + 12) + '" fill="#9aa6b4">层 ' + bd.layer + "</text>";
    });
    return s;
  }

  function legendSvg(y) {
    var s = '<g class="dbarc-legend">' +
      '<text class="dbarc-legcap" x="' + PAD + '" y="' + (y + 4) + '" fill="#8a8172">流量拥堵（请求/分钟）：</text>';
    var x = PAD + 150;
    CONG.forEach(function (t) {
      s += '<rect x="' + x + '" y="' + (y - 6) + '" width="22" height="9" rx="4.5" fill="' + t.color + '"/>' +
        '<text class="dbarc-lab" x="' + (x + 27) + '" y="' + (y + 2) + '" fill="#556274">' + t.name + " " + esc(t.range) + "</text>";
      x += 27 + (t.name.length + t.range.length + 1) * 7.2 + 26;
    });
    return s + "</g>";
  }

  function build(data) {
    var L = layout(data), pos = L.pos;
    var bands = bandsSvg(L.bandMeta);
    var edges = "", nodes = "";
    (data.edges || []).forEach(function (e) { edges += edgeSvg(e, pos, L.W); });
    Object.keys(pos).forEach(function (k) { nodes += nodeSvg(pos[k]); });
    var H = L.H + LEGEND_H;
    return '<svg class="dbarc" viewBox="0 0 ' + L.W + " " + H + '" width="100%" height="' + H + '" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMin meet">' +
      '<defs>' +
      '<marker id="dbarc-arrow" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto"><path d="M0 0 L9 4.5 L0 9 z" fill="context-stroke"/></marker>' +
      '<marker id="dbarc-arrow-faint" markerWidth="7" markerHeight="7" refX="5" refY="3.5" orient="auto"><path d="M0 0 L7 3.5 L0 7 z" fill="#c3cbd6"/></marker>' +
      "</defs>" +
      bands + edges + nodes + legendSvg(L.H + LEGEND_H / 2) + "</svg>";
  }

  function css() {
    if (document.getElementById("dbarc-style")) return;
    var st = document.createElement("style"); st.id = "dbarc-style";
    st.textContent =
      ".dbarc{font-family:inherit;max-width:100%}" +
      ".dbarc text{font-family:'Songti SC','SimSun',-apple-system,'PingFang SC',sans-serif}" +
      ".dbarc-lbl{font-size:11.5px;font-weight:700}" +
      ".dbarc-sub,.dbarc-meta{font-size:9px}" +
      ".dbarc-tier{font-size:13px;font-weight:700;letter-spacing:.5px}" +
      ".dbarc-tier-no{font-size:9.5px;letter-spacing:1px}" +
      ".dbarc-legcap{font-size:11px;letter-spacing:.5px;font-weight:600}" +
      ".dbarc-lab{font-size:9.5px;font-weight:600}" +
      ".dbarc-labbg{stroke-width:1}" +
      ".dbarc-node.off{opacity:.55}" +
      ".dbarc-flow{animation-name:dbarc-dash;animation-timing-function:linear;animation-iteration-count:infinite}" +
      "@keyframes dbarc-dash{to{stroke-dashoffset:-40}}" +
      "@media(prefers-reduced-motion:reduce){.dbarc-flow{animation:none;opacity:.9}}";
    document.head.appendChild(st);
  }

  function metaText(d) {
    var eng = d.engine || {}, f = (d.flow && d.flow.per_min) || {};
    return "备机容器 " + (eng.containers_running || 0) + "/" + (eng.containers_total || 0) +
      " · " + (eng.cpus || "?") + "核/" + (eng.mem_total_gb || "?") + "GB · 组件 " + (d.nodes || []).length +
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

  global.DbTopo = { build: build, mount: mount, layout: layout, metaText: metaText, congestion: congestion, CONGESTION: CONG };
})(typeof window !== "undefined" ? window : globalThis);
