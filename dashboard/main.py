"""⑦ 数据大盘：只读聚合流量埋点(qa_events)与分级RAG内容配置，单一淡雅中国风页面。

页面用相对 api/* 取数，前端不接触任何密钥；DSN 仅服务端使用。观测只读，不写业务库。
"""
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from common import observe
from common.config import get_settings
from common.corpus_fixtures import FIXTURES
from common.health import health_router

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
            "public_cloud_path": "gateway → agent_orchestrator/plan（external_stub，本次未启用）",
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
footer{color:var(--muted);font-size:12px;padding:24px 28px;text-align:center}
.kv{display:flex;justify-content:space-between;font-size:13px;padding:4px 0}
.ans{background:#fffdf7;border:1px dashed var(--line);border-radius:8px;padding:12px;white-space:pre-wrap;font-size:13px;margin-top:8px}
</style></head>
<body>
<header><h1>DaoBrain · 全链路数据大盘</h1>
<div class="sub">公有云入口 → 本地分流 · 分级 RAG 解决情况 · 问答流水（时间倒序，可点击详情）· 数据来源：真实问答链路埋点落库</div></header>
<div class="wrap">
<div class="grid kpis" id="kpis"></div>
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
<footer>DaoBrain 汉唐医道 · 观测为尽力而为落库，被拒绝/敏感请求不记录正文</footer>
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
</script>
</body></html>"""
