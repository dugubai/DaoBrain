"""⑦ 数据大盘：只读聚合流量埋点(qa_events)与分级RAG内容配置，单一淡雅中国风页面。

页面用相对 api/* 取数，前端不接触任何密钥；DSN 仅服务端使用。观测只读，不写业务库。
新增 /api/topology：备机容器实时运行指标 + 主机节点探活 + 分级链路数据流（见 dashboard/topology.py）。
"""
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from common import observe
from common.config import get_settings
from common.corpus_fixtures import FIXTURES
from common.health import health_router
from dashboard import topology as topo

app = FastAPI(title="DaoBrain ⑦ dashboard")
app.include_router(health_router("dashboard"))


def _zone_sources(zone_value):
    items = [f for f in FIXTURES if f.zone.value == zone_value]
    return [{
        "source_id": f.source_id, "title": f.title, "locator": f.locator,
        "quote": (f.quote or "")[:60], "source_type": f.source_type.value,
        "keywords": list(f.keywords)[:8],
    } for f in items]


def tiers():
    s = get_settings()
    green = _zone_sources("green")
    red = _zone_sources("red")
    public_topics = sorted({t for f in FIXTURES if f.zone.value == "green" for t in f.public_topics})
    classic = sum(1 for f in FIXTURES if f.zone.value == "green" and f.source_type.value == "public_classic")
    faq = len(green) - classic
    return {
        "tiers": {
            "public": {
                "label": "公开文化检索层", "retriever": "rag_public",
                "model": "检索摘录（不调用生成模型）", "sources": faq,
                "note": "公开 FAQ / 编辑规范，纯合成，命中即摘录",
            },
            "green": {
                "label": "绿区本地模型层", "retriever": "rag_green",
                "model": "ollama:" + s.local_model_name, "mode": s.local_model_backend,
                "protocol": s.model_output_protocol, "endpoint": "/infer", "sources": classic,
                "note": "公开典籍原文，本地 GPU 小模型 select 抽取后经核验释放",
            },
            "red": {
                "label": "红区调教层", "retriever": "rag_red",
                "model": "daobrain_tuned（合成样例）", "endpoint": "/reason", "sources": len(red),
                "note": "内部/敏感合成案例，公开入口一律拒绝释放，仅本地核验链路",
            },
        },
        "routing": {
            "gateway_green_backend": s.gateway_green_backend,
            "green_path": "gateway → hybrid_iface/dispatch → rag_green → local_model → output_verify",
            "red_path": "gateway → hybrid_iface/dispatch → rag_red → daobrain_tuned",
            "public_cloud_path": "gateway_cloud → agent_orchestrator/plan → rag_public → external_model（external_stub 实时并行）",
        },
        "thresholds": {"top_k_default": 4, "top_k_max": 20,
                       "max_requests_per_minute": s.max_requests_per_minute,
                       "public_topics_count": len(public_topics)},
    }


def overview(limit):
    data = {"metrics": observe.metrics(0), "tier_stats": observe.tier_stats(),
            "channel_stats": observe.channel_stats(), "tiers": tiers(),
            "qa": observe.qa_list(limit)}
    return data


@app.get("/api/overview")
def api_overview(limit: int = Query(default=60, ge=1, le=500)):
    try:
        return overview(limit)
    except Exception as exc:
        raise HTTPException(503, "大盘数据暂不可用：%s" % type(exc).__name__)


@app.get("/api/qa/detail")
def api_qa_detail(id: int = Query(..., ge=1)):
    try:
        d = observe.qa_detail(id)
    except Exception:
        raise HTTPException(503, "问答存储暂不可用")
    if d is None:
        raise HTTPException(404, "记录不存在")
    return d


@app.get("/api/topology")
def api_topology():
    try:
        return topo.build()
    except Exception as exc:
        raise HTTPException(503, "拓扑数据暂不可用：%s" % type(exc).__name__)


@app.get("/arch", response_class=HTMLResponse)
def arch():
    """供中台嵌入的独立“动起来的架构图”页面：仅架构拓扑 + 实时数据流,自动刷新。"""
    return HTMLResponse(ARCH_PAGE)


ARCH_PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DaoBrain 架构拓扑 · 实时</title>
<style>
:root{--bg:#f3efe6;--ink:#3b3630;--muted:#8a8172;--line:#d8cfbd;--card:#faf7f0;--accent:#7d9b86;--accent2:#b08968;--danger:#c0685a;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Songti SC","SimSun",serif;line-height:1.6}
header{padding:16px 20px 12px;border-bottom:1px solid var(--line)}
h1{margin:0;font-size:18px;letter-spacing:2px}
.sub{color:var(--muted);font-size:12px;margin-top:5px}
.wrap{padding:16px 20px;max-width:1180px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-top:16px}
.card h2{margin:0 0 12px;font-size:15px;letter-spacing:1px;border-left:3px solid var(--accent);padding-left:10px}
.tiny{color:var(--muted);font-size:12px;font-weight:400;letter-spacing:0;margin-left:8px}
.muted{color:var(--muted)}
.topo{display:flex;gap:12px;overflow-x:auto;padding-bottom:6px}
.topoc{min-width:190px;flex:0 0 190px}
.topot{font-size:12px;color:var(--muted);letter-spacing:1px;border-bottom:1px solid var(--line);padding:0 0 6px;margin-bottom:8px;text-align:center}
.node{border:1px solid var(--line);border-radius:10px;padding:9px 10px;margin-bottom:9px;background:#fffdf7;position:relative}
.node.off{opacity:.62;background:#f6e9e6}
.nlabel{font-size:13px;font-weight:700;line-height:1.35;padding-left:14px}
.ndot{position:absolute;left:9px;top:12px;width:9px;height:9px;border-radius:50%;background:#3f7d54}
.ndot.restarting{background:#c9a13b;animation:blink 1s infinite}
.ndot.down{background:var(--danger)}
@keyframes blink{50%{opacity:.25}}
.nmeta{font-size:11px;color:var(--muted);margin-top:3px;padding-left:14px}
.host{display:inline-block;padding:0 6px;border-radius:8px;font-size:10px;margin-right:4px}
.host.b{background:#e6efe4;color:#3f7d54}.host.h{background:#e4e9f3;color:#3b5b8a}
.mline{font-size:11px;color:var(--muted);padding-left:14px;margin-top:4px;display:flex;align-items:center;gap:6px}
.mtrack{flex:1;height:6px;background:#ece3d2;border-radius:4px;overflow:hidden}
.mfill{height:100%;border-radius:4px;background:linear-gradient(90deg,var(--accent),var(--accent2))}
.mfill.hot{background:linear-gradient(90deg,#d69a4b,#c0685a)}
.topoflow{display:flex;flex-wrap:wrap;gap:10px}
.eflow{flex:0 0 calc(33.3% - 7px);border:1px solid var(--line);border-radius:9px;padding:8px 10px;background:#fffdf7;font-size:12px;min-width:230px}
.eflow .eh{display:flex;justify-content:space-between;align-items:center}
.eflow .path{color:var(--ink);font-weight:600}
.eflow .rate{font-weight:700;color:#3f7d54}
.eflow .rate.zero{color:var(--muted)}
.arrow{height:8px;margin:7px 0;border-radius:4px;background:repeating-linear-gradient(90deg,#c9d6c4 0 12px,transparent 12px 22px);background-size:200% 100%}
.arrow.on{animation:flow 1.1s linear infinite}
@keyframes flow{from{background-position:0 0}to{background-position:-34px 0}}
</style></head><body>
<header><h1>DaoBrain 架构拓扑 · 实时运行</h1>
<div class="sub">容器分布(备机/主机) · 运行状态 · CPU/内存/网络/进程 · 每条链路实时数据流(req/min,箭头表示正在流动) · 自动刷新 3s</div></header>
<div class="wrap">
<div class="card"><h2>架构拓扑 · 实时运行<span class="tiny" id="topometa"></span></h2><div class="topo" id="topo"></div></div>
<div class="card"><h2>数据流 · 实时（近15分钟 req/min）<span class="tiny" id="flowmeta"></span></h2><div class="topoflow" id="topoflow"></div></div>
</div>
<script>
function esc(x){return (x==null?'':String(x)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function mbar(label,val,unit,pct,hot){const w=Math.max(0,Math.min(100,(pct==null?val:pct)));
  return `<div class="mline"><span style="min-width:30px">${label}</span>
  <div class="mtrack"><div class="mfill${hot?' hot':''}" style="width:${w}%"></div></div>
  <span style="min-width:70px;text-align:right">${esc(val)}${unit||''}</span></div>`;}
function nodeHtml(n){
  let dot='ndot'; if(!n.up){dot+=' down';} else if(n.state==='restarting'){dot='ndot restarting';}
  const host=n.host==='主机'?'<span class="host h">主机</span>':'<span class="host b">备机</span>';
  let body='';
  if(n.host==='主机'){ body=`<div class="mline" style="padding-left:14px">跨机仅探活 · ${esc(n.status||'')}</div>`; }
  else {
    body=mbar('CPU',(n.cpu==null?'—':n.cpu),'%',n.cpu,(n.cpu>=70))
        +mbar('MEM',(n.mem_mb==null?'—':n.mem_mb),'MB',n.mem_percent,(n.mem_percent>=80));
  }
  const foot=(n.host==='主机')?'':`<div class="nmeta">↻${n.restarts} · 网 ${n.net_mbps}MB/s · pid ${n.pids} · ${esc(n.health||'')}</div>`;
  return `<div class="node ${n.up?'on':'off'}"><div class="${dot}"></div>
    <div class="nlabel">${esc(n.label)}</div>
    <div class="nmeta">${host}: ${n.port} · ${esc(n.state||'')}</div>
    ${body}${foot}</div>`;
}
function edgeHtml(e){const on=(e.signal&&e.signal>0)?'on':'';const rc=(e.signal>0)?'rate':'rate zero';
  return `<div class="eflow"><div class="eh"><span class="path">${esc(e.label)}</span>
  <span class="${rc}">${e.signal} /min</span></div>
  <div class="nmeta">${esc(e.from)} → ${esc(e.to)}</div>
  <div class="arrow ${on}"></div></div>`;
}
async function loadTopo(){
  let d; try{ d=await (await fetch('api/topology',{headers:{'Accept':'application/json'}})).json(); }
  catch(e){ document.getElementById('topo').innerHTML='<div class="muted">拓扑数据暂不可用</div>'; return; }
  const eng=d.engine||{};
  document.getElementById('topometa').textContent='备机容器 '+(eng.containers_running||0)+'/'+(eng.containers_total||0)
    +' · '+eng.cpus+'核/'+eng.mem_total_gb+'GB · 节点 '+d.nodes.length
    +(d.cycle_seconds?(' · 采集'+d.cycle_seconds+'s'):'')+(d.collector_error?(' · 采集告警 '+esc(d.collector_error)):'');
  const byl={}; d.nodes.forEach(n=>{(byl[n.layer]=byl[n.layer]||[]).push(n);});
  document.getElementById('topo').innerHTML=(d.layers||[]).map(L=>{
    const col=byl[L.layer]||[]; if(!col.length)return '';
    return `<div class="topoc"><div class="topot">${esc(L.title)}</div>${col.map(nodeHtml).join('')}</div>`;
  }).join('');
  const f=(d.flow&&d.flow.per_min)||{};
  document.getElementById('flowmeta').textContent='总 '+((d.flow&&d.flow.counts&&d.flow.counts.total)||0)
    +' 条 / '+((d.flow&&d.flow.window_min)||15)+'分钟 · 本地'+(f.local||0)+'/min 绿区'+(f.green||0)+'/min 红区'+(f.red||0)+'/min 公有云'+(f.cloud||0)+'/min';
  document.getElementById('topoflow').innerHTML=(d.edges||[]).map(edgeHtml).join('')||'<div class="muted">暂无链路</div>';
}
loadTopo();
setInterval(loadTopo,3000);
</script>
</body></html>
"""


@app.get("/", response_class=HTMLResponse)
def page():
    return HTMLResponse(PAGE)


PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DaoBrain 数据大盘</title>
<style>
:root{--bg:#f3efe6;--ink:#3b3630;--muted:#8a8172;--line:#d8cfbd;--card:#faf7f0;--accent:#7d9b86;--accent2:#b08968;--danger:#c0685a;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Songti SC","SimSun",serif;line-height:1.6}
header{padding:26px 28px 18px;border-bottom:1px solid var(--line)}
h1{margin:0;font-size:22px;letter-spacing:2px}
.sub{color:var(--muted);font-size:13px;margin-top:6px}
.wrap{padding:22px 28px;max-width:1180px;margin:0 auto}
.grid{display:grid;gap:16px}
.kpis{grid-template-columns:repeat(6,1fr)}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.kpi .n{font-size:26px;font-weight:700}
.kpi .l{color:var(--muted);font-size:12px;margin-top:2px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-top:18px}
.card h2{margin:0 0 14px;font-size:16px;letter-spacing:1px;border-left:3px solid var(--accent);padding-left:10px}
.row{display:flex;justify-content:space-between;font-size:13px;padding:6px 0;border-bottom:1px dashed var(--line)}
.row:last-child{border-bottom:none}
.bar{height:10px;border-radius:6px;background:linear-gradient(90deg,var(--accent),var(--accent2))}
.barwrap{background:#eee7d8;border-radius:6px;flex:1;margin:0 10px;height:10px;align-self:center}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600}
tr.click{cursor:pointer}tr.click:hover{background:#efe8d8}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;border:1px solid var(--line)}
.ok{color:#3f7d54}.no{color:var(--danger)}
.pill-rel{background:#e6f0e6;color:#3f7d54}.pill-ref{background:#f3e2df;color:var(--danger)}
.pill-err{background:#f6ecd6;color:#9a7b2e}
.cols3{grid-template-columns:repeat(3,1fr)}
.tier{border:1px solid var(--line);border-radius:10px;padding:14px;background:#fffdf7}
.tier .t{font-size:15px;font-weight:700;margin-bottom:4px}
.tier .m{color:var(--muted);font-size:12px;margin-top:8px}
#detail{position:fixed;right:0;top:0;height:100%;width:420px;max-width:92vw;background:var(--card);border-left:1px solid var(--line);box-shadow:-6px 0 20px rgba(0,0,0,.06);padding:22px;overflow:auto;transform:translateX(100%);transition:transform .25s}
#detail.open{transform:none}
.close{float:right;cursor:pointer;color:var(--muted)}
.muted{color:var(--muted)}
.tiny{color:var(--muted);font-size:12px;font-weight:400;letter-spacing:0;margin-left:8px}
footer{color:var(--muted);font-size:12px;padding:24px 28px;text-align:center}
.kv{display:flex;justify-content:space-between;font-size:13px;padding:4px 0}
.ans{background:#fffdf7;border:1px dashed var(--line);border-radius:8px;padding:12px;white-space:pre-wrap;font-size:13px;margin-top:8px}
.topo{display:flex;gap:12px;overflow-x:auto;padding-bottom:6px}
.topoc{min-width:186px;flex:0 0 186px}
.topot{font-size:12px;color:var(--muted);letter-spacing:1px;border-bottom:1px solid var(--line);padding:0 0 6px;margin-bottom:8px;text-align:center}
.node{border:1px solid var(--line);border-radius:10px;padding:9px 10px;margin-bottom:9px;background:#fffdf7;position:relative}
.node.off{opacity:.62;background:#f6e9e6}
.nlabel{font-size:13px;font-weight:700;line-height:1.35;padding-left:14px}
.ndot{position:absolute;left:9px;top:12px;width:9px;height:9px;border-radius:50%;background:#3f7d54}
.ndot.restarting{background:#c9a13b;animation:blink 1s infinite}
.ndot.down{background:var(--danger)}
@keyframes blink{50%{opacity:.25}}
.nmeta{font-size:11px;color:var(--muted);margin-top:3px;padding-left:14px}
.host{display:inline-block;padding:0 6px;border-radius:8px;font-size:10px;margin-right:4px}
.host.b{background:#e6efe4;color:#3f7d54}.host.h{background:#e4e9f3;color:#3b5b8a}
.mline{font-size:11px;color:var(--muted);padding-left:14px;margin-top:4px;display:flex;align-items:center;gap:6px}
.mtrack{flex:1;height:6px;background:#ece3d2;border-radius:4px;overflow:hidden}
.mfill{height:100%;border-radius:4px;background:linear-gradient(90deg,var(--accent),var(--accent2))}
.mfill.hot{background:linear-gradient(90deg,#d69a4b,#c0685a)}
.topoflow{display:flex;flex-wrap:wrap;gap:10px}
.eflow{flex:0 0 calc(33.3% - 7px);border:1px solid var(--line);border-radius:9px;padding:8px 10px;background:#fffdf7;font-size:12px;min-width:230px}
.eflow .eh{display:flex;justify-content:space-between;align-items:center}
.eflow .path{color:var(--ink);font-weight:600}
.eflow .rate{font-weight:700;color:#3f7d54}
.eflow .rate.zero{color:var(--muted)}
.arrow{height:8px;margin:7px 0;border-radius:4px;background:repeating-linear-gradient(90deg,#c9d6c4 0 12px,transparent 12px 22px);background-size:200% 100%}
.arrow.on{animation:flow 1.1s linear infinite}
@keyframes flow{from{background-position:0 0}to{background-position:-34px 0}}
</style></head>
<body>
<header><h1>DaoBrain · 全链路数据大盘</h1>
<div class="sub">公有云入口 → 本地分流 · 分级 RAG 解决情况 · 架构拓扑与容器实时运行 · 问答流水（时间倒序，可点击详情）· 数据来源：真实问答链路埋点落库 + 备机 Docker 实时指标</div></header>
<div class="wrap">
<div class="grid kpis" id="kpis"></div>
<div class="card"><h2>架构拓扑 · 实时运行<span class="tiny" id="topometa"></span></h2><div class="topo" id="topo"></div></div>
<div class="card"><h2>数据流 · 实时（近15分钟 req/min）<span class="tiny" id="flowmeta"></span></h2><div class="topoflow" id="topoflow"></div></div>
<div class="card"><h2>全路径流量分道（公有云入口 / 本地分流）</h2><div id="flow"></div></div>
<div class="cols3 grid" style="margin-top:18px">
  <div class="card" style="margin:0"><h2>区域分布</h2><div id="zones"></div></div>
  <div class="card" style="margin:0"><h2>模型分布</h2><div id="models"></div></div>
  <div class="card" style="margin:0"><h2>处置结果</h2><div id="outcomes"></div></div>
</div>
<div class="card"><h2>分级 RAG 解决情况</h2><div id="tierstats"></div></div>
<div class="card"><h2>分级 RAG 内容配置</h2><div class="grid cols3" id="tiers"></div>
  <div class="muted" id="routing" style="margin-top:14px;font-size:12px"></div></div>
<div class="card"><h2>问答流水</h2>
  <table><thead><tr><th>#</th><th>时间(UTC)</th><th>区域</th><th>层级</th><th>分道</th><th>处置</th><th>模型</th><th>延迟ms</th><th>提问</th></tr></thead>
  <tbody id="qa"></tbody></table></div>
</div>
<div id="detail"></div>
<footer>DaoBrain 汉唐医道 · 观测为尽力而为落库，被拒绝/敏感请求不记录正文 · 主机节点仅经 80 端口探活，容器级负载跨机不可见</footer>
<script>
function esc(s){return (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function bar(list,valKey,labelKey,fmt){const max=Math.max(1,...list.map(x=>x[valKey]));
  return list.map(x=>`<div class="row"><span style="min-width:88px">${esc(fmt?fmt(x[labelKey]):x[labelKey]||'—')}</span>
  <div class="barwrap"><div class="bar" style="width:${Math.round(x[valKey]/max*100)}%"></div></div>
  <b style="min-width:42px;text-align:right">${x[valKey]}</b></div>`).join('')||'<div class="muted">暂无数据</div>';}
const zmap={green:'绿区',red:'红区',unknown:'未分类'};
function pill(o){if(o==='release')return '<span class="tag pill-rel">已释放</span>';
 if(o==='refuse_verify')return '<span class="tag pill-ref">核验拒绝</span>';
 if(o==='refuse_entry')return '<span class="tag pill-ref">入口拒绝</span>';
 if(o==='dependency_error')return '<span class="tag pill-err">依赖异常</span>';return esc(o);}
function mbar(label,val,unit,pct,hot){const w=Math.max(0,Math.min(100,(pct==null?val:pct)));
  return `<div class="mline"><span style="min-width:30px">${label}</span>
  <div class="mtrack"><div class="mfill${hot?' hot':''}" style="width:${w}%"></div></div>
  <span style="min-width:70px;text-align:right">${esc(val)}${unit||''}</span></div>`;}
function nodeHtml(n){
  let dot='ndot'; if(!n.up){dot+=' down';} else if(n.state==='restarting'){dot='ndot restarting';}
  const host=n.host==='主机'?'<span class="host h">主机</span>':'<span class="host b">备机</span>';
  let body='';
  if(n.host==='主机'){ body=`<div class="mline" style="padding-left:14px">跨机仅探活 · ${esc(n.status||'')}</div>`; }
  else {
    body=mbar('CPU',(n.cpu==null?'—':n.cpu),'%',n.cpu,(n.cpu>=70))
        +mbar('MEM',(n.mem_mb==null?'—':n.mem_mb),'MB',n.mem_percent,(n.mem_percent>=80));
  }
  const foot=(n.host==='主机')?'':`<div class="nmeta">↻${n.restarts} · 网 ${n.net_mbps}MB/s · pid ${n.pids} · ${esc(n.health||'')}</div>`;
  return `<div class="node ${n.up?'on':'off'}"><div class="${dot}"></div>
    <div class="nlabel">${esc(n.label)}</div>
    <div class="nmeta">${host}: ${n.port} · ${esc(n.state||'')}</div>
    ${body}${foot}</div>`;
}
function edgeHtml(e){const on=(e.signal&&e.signal>0)?'on':'';const rc=(e.signal>0)?'rate':'rate zero';
  return `<div class="eflow"><div class="eh"><span class="path">${esc(e.label)}</span>
  <span class="${rc}">${e.signal} /min</span></div>
  <div class="nmeta">${esc(e.from)} → ${esc(e.to)}</div>
  <div class="arrow ${on}"></div></div>`;
}
async function loadTopo(){
  let d; try{ d=await (await fetch('api/topology')).json(); }
  catch(e){ document.getElementById('topo').innerHTML='<div class="muted">拓扑数据暂不可用</div>'; return; }
  const eng=d.engine||{};
  document.getElementById('topometa').textContent='备机容器 '+(eng.containers_running||0)+'/'+(eng.containers_total||0)
    +' · '+eng.cpus+'核/'+eng.mem_total_gb+'GB · 节点 '+d.nodes.length
    +(d.cycle_seconds?(' · 采集'+d.cycle_seconds+'s'):'')+(d.collector_error?(' · ⚠'+esc(d.collector_error)):'');
  const byl={}; d.nodes.forEach(n=>{(byl[n.layer]=byl[n.layer]||[]).push(n);});
  document.getElementById('topo').innerHTML=(d.layers||[]).map(L=>{
    const col=byl[L.layer]||[]; if(!col.length)return '';
    return `<div class="topoc"><div class="topot">${esc(L.title)}</div>${col.map(nodeHtml).join('')}</div>`;
  }).join('');
  const f=(d.flow&&d.flow.per_min)||{};
  document.getElementById('flowmeta').textContent='总 '+((d.flow&&d.flow.counts&&d.flow.counts.total)||0)
    +' 条 / '+((d.flow&&d.flow.window_min)||15)+'分钟 · 本地'+(f.local||0)+'/min 绿区'+(f.green||0)+'/min 红区'+(f.red||0)+'/min 公有云'+(f.cloud||0)+'/min';
  document.getElementById('topoflow').innerHTML=(d.edges||[]).map(edgeHtml).join('')||'<div class="muted">暂无链路</div>';
}
async function load(){
 const d=await (await fetch('api/overview')).json();
 const t=d.metrics.totals;
 document.getElementById('kpis').innerHTML=[
  ['总请求',t.total],['已释放',t.released],['拒绝',t.refused],['依赖异常',t.errors],
  ['平均延迟',t.avg_latency+'ms'],['P95',t.p95_latency+'ms']
 ].map(([l,n])=>`<div class="kpi"><div class="n">${n}</div><div class="l">${l}</div></div>`).join('');
 const chan=d.channel_stats.reduce((m,r)=>{m[r.channel||'未派发']=(m[r.channel||'未派发']||0)+r.n;return m;},{});
 const chl=Object.entries(chan).map(([k,v])=>({k,v}));
 const cname={'public_cloud':'公有云入口','local':'本地分流','未派发':'未派发(入口拒绝)'};
 document.getElementById('flow').innerHTML=bar(chl,'v','k',x=>cname[x]||x)||'';
 if(!chl.length)document.getElementById('flow').innerHTML='<div class="muted">暂无数据</div>';
 document.getElementById('zones').innerHTML=bar(d.metrics.by_zone,'n','k',x=>zmap[x]||x||'未分类');
 document.getElementById('models').innerHTML=bar(d.metrics.by_model,'n','k');
 document.getElementById('outcomes').innerHTML=bar(d.metrics.by_outcome,'n','k');
 document.getElementById('tierstats').innerHTML='<table><thead><tr><th>层级</th><th>总量</th><th>已释放</th><th>核验拒绝</th><th>入口拒绝</th><th>依赖异常</th><th>均延</th></tr></thead><tbody>'+
  (d.tier_stats.map(r=>`<tr><td>${esc(zmap[r.tier]||r.tier||'—')}</td><td>${r.total}</td><td class="ok">${r.released}</td><td>${r.refuse_verify}</td><td>${r.refuse_entry}</td><td>${r.errors}</td><td class="muted">${r.avg_latency}ms</td></tr>`).join('')||'<tr><td colspan="7" class="muted">暂无数据</td></tr>')+'</tbody></table>';
 const cfg=d.tiers;
 document.getElementById('tiers').innerHTML=['public','green','red'].map(k=>{const x=cfg.tiers[k];
  return `<div class="tier"><div class="t">${x.label}</div><div>语料条目：<b>${x.sources}</b> · 检索器：${x.retriever}</div>
  <div class="m">模型/处置：${esc(x.model)}${x.mode?'（'+x.mode+' · '+x.protocol+'）':''}${x.endpoint?' '+x.endpoint:''}</div>
  <div class="m">${esc(x.note)}</div></div>`;}).join('');
 document.getElementById('routing').innerHTML='绿区链路：'+esc(cfg.routing.green_path)+'<br>红区链路：'+esc(cfg.routing.red_path)+'<br>公有云：'+esc(cfg.routing.public_cloud_path)+
   ' ｜ top_k：默认'+cfg.thresholds.top_k_default+'/上限'+cfg.thresholds.top_k_max+'，限速 '+cfg.thresholds.max_requests_per_minute+'/分钟，公开话题 '+cfg.thresholds.public_topics_count+' 个';
 const tb=document.getElementById('qa');
 tb.innerHTML=(d.qa||[]).map(r=>`<tr class="click" data-id="${r.id}"><td class="muted">${r.id}</td><td>${esc(r.ts)}</td>
  <td>${esc(zmap[r.zone]||r.zone)}</td><td>${esc(zmap[r.tier]||r.tier)}</td><td>${esc(r.channel||'—')}</td>
  <td>${pill(r.outcome)}</td><td class="muted">${esc(r.model||'—')}</td><td>${r.latency_ms}</td>
  <td>${esc((r.query||'').slice(0,30))}</td></tr>`).join('')||'<tr><td colspan="9" class="muted">暂无记录</td></tr>';
 tb.querySelectorAll('tr.click').forEach(tr=>tr.onclick=()=>detail(tr.dataset.id));
}
async function detail(id){
 const d=await (await fetch('api/qa/detail?id='+id)).json();
 const el=document.getElementById('detail');
 const kv=(k,v)=>`<div class="kv"><span class="muted">${k}</span><span>${v}</span></div>`;
 el.innerHTML='<span class="close" onclick="this.parentElement.classList.remove(\'open\')">✕ 关闭</span>'
 +'<h2 style="border:none;padding:0">问答详情 #'+d.id+'</h2>'
 +kv('时间(UTC)',esc(d.ts))+kv('task_hash','<span class="muted">'+esc(d.task_hash)+'</span>')
 +kv('身份/persona',esc(d.persona||'—'))+kv('区域',esc(d.zone))+kv('层级',esc(d.tier||'—'))
 +kv('分道',esc(d.channel||'—'))+kv('路由目标','<span class="muted">'+esc(d.route_target||'—')+'</span>')
 +kv('核验',esc(d.verify))+kv('处置',pill(d.outcome))+kv('模型',esc(d.model||'—'))
 +kv('证据数',d.evidence_count)+kv('证据ID','<span class="muted">'+esc(d.evidence_ids||'—')+'</span>')
 +kv('延迟',d.latency_ms+'ms')
 +'<div class="muted" style="margin-top:14px">提问</div><div class="ans">'+esc(d.query)+'</div>'
 +(d.note?'<div class="muted" style="margin-top:10px">备注</div><div class="ans">'+esc(d.note)+'</div>':'')
 +(d.answer?'<div class="muted" style="margin-top:10px">已释放答案</div><div class="ans">'+esc(d.answer)+'</div>'
   :'<div class="muted" style="margin-top:10px">（未释放，按隐私规则不记录答案正文）</div>');
 el.classList.add('open');
}
load().catch(e=>{document.getElementById('kpis').innerHTML='<div class="card">数据加载失败：'+esc(e.message)+'</div>';});
loadTopo();
setInterval(loadTopo,6000);
</script>
</body></html>"""
