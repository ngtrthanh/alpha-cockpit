const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const money = n => new Intl.NumberFormat('vi-VN',{maximumFractionDigits:0}).format(n||0)+' ₫';
let handbook=[];

async function api(url, options){const r=await fetch(url,options); const j=await r.json(); if(!r.ok) throw new Error(j.error||r.statusText); return j;}

function initTabs(){
  $$('.tab').forEach(b=>b.onclick=()=>{
    $$('.tab').forEach(x=>x.classList.remove('active')); b.classList.add('active');
    $$('.panel').forEach(x=>x.classList.remove('active')); $('#'+b.dataset.tab).classList.add('active');
  });
}

function scoreCard(d){
  return `<article class="card score-card"><div class="card-head"><span class="ticker">${d.ticker}</span><span>${Math.round(d.confidence*100)}% conf.</span></div><div class="score">${d.decision_index.toFixed(1)}</div><div class="bar"><i style="width:${d.decision_index}%"></i></div><div class="action">${d.status} · ${d.allocation_bias}</div><p class="muted">${d.timing_state} · ${d.suggested_qty ? d.suggested_qty+' cp · ' : ''}${money(d.suggested_value)}</p></article>`;
}

async function loadCockpit(){
  const [p,s,h]=await Promise.all([api('/api/portfolio'),api('/api/scores'),api('/api/health')]);
  $('#health').textContent=h.ok?'engine online':'offline'; $('#hurdle').textContent=p.deposit_rate.toFixed(1)+'%';
  $('#data-label').textContent=h.demo_data?'DEMO ENABLED':'LIVE DATA ONLY';
  $('#score-cards').innerHTML=s.length?s.map(scoreCard).join(''):'<article class="card"><b>No scored live data yet.</b><p class="muted">Check Runtime for Hermes/SSI status. No synthetic fallback is being used.</p></article>';
  $('#targets').innerHTML=p.targets.map(t=>`<div class="alloc-row"><b>${t.ticker}</b><div class="alloc-track"><i style="width:${t.target_weight*100}%"></i></div><span>${(t.target_weight*100).toFixed(1)}%</span></div>`).join('');
  $('#holdings').innerHTML=p.holdings.length?p.holdings.map(h=>`<div class="metric-row"><b>${h.ticker} · ${h.qty}</b><span>${money(h.value)}</span></div>`).join(''):'Chưa import giao dịch thật.';
}

function showGuide(id){
  const g=handbook.find(x=>x.id===id); if(!g)return;
  $$('.guide-link').forEach(x=>x.classList.toggle('active',Number(x.dataset.id)===id));
  $('#guide-content').innerHTML=`<div class="num">MỤC ${String(g.id).padStart(2,'0')} / 38</div><h2>${g.title}</h2><p>${g.body}</p><div class="card" style="margin-top:26px"><div class="card-head"><span>Thực hành</span><span>COCKPIT RULE</span></div><p class="muted">Đưa nguyên tắc này vào checklist trước khi thay đổi vị thế. Agent chỉ đánh dấu OK / WATCH / REVIEW / ALERT; quyết định giao dịch vẫn do người dùng xác nhận.</p></div>`;
}
function renderGuideNav(filter=''){
  const q=filter.trim().toLowerCase();
  const items=handbook.filter(g=>(g.title+' '+g.body).toLowerCase().includes(q));
  $('#guide-nav').innerHTML=items.map(g=>`<button class="guide-link" data-id="${g.id}"><b>${String(g.id).padStart(2,'0')}</b><span>${g.title}</span></button>`).join('');
  $$('.guide-link').forEach(b=>b.onclick=()=>showGuide(Number(b.dataset.id)));
}
async function loadHandbook(){handbook=await api('/api/handbook');renderGuideNav();showGuide(1);$('#guide-search').oninput=e=>renderGuideNav(e.target.value)}

const metricNames=['quality','growth','valuation','momentum','financial_strength','risk','liquidity','macro'];
function initScoreLab(){
  $('#metric-inputs').innerHTML=metricNames.map((m,i)=>`<label>${m.replaceAll('_',' ')}<input data-metric="${m}" type="number" min="0" max="100" value="${[75,70,65,55,78,65,80,60][i]}"></label>`).join('');
  $('#completeness').oninput=e=>$('#completeness-out').textContent=Math.round(Number(e.target.value)*100)+'%';
  $('#run-score').onclick=async()=>{
    const metrics={}; $$('[data-metric]').forEach(i=>metrics[i.dataset.metric]=Number(i.value));
    metrics.data_completeness=Number($('#completeness').value); metrics.freshness_hours=Number($('#freshness').value);
    const d=await api('/api/score',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticker:$('#ticker').value||'TEST',metrics})});
    $('#lab-score').textContent=d.decision_index.toFixed(1); $('#lab-confidence').textContent=Math.round(d.confidence*100)+'%'; $('#lab-action').textContent=d.status+' · '+d.allocation_bias; $('#lab-sizing').textContent=(d.suggested_qty?d.suggested_qty+' cp · ':'')+money(d.suggested_value)+' · '+d.timing_state; $('#lab-rationale').textContent=d.rationale;
  };
}

async function loadAudit(){const rows=await api('/api/audit');$('#audit-table').innerHTML=rows.map(r=>`<div class="audit-row"><time>${r.ts.replace('T',' ')}</time><b>${r.event_type}</b><span>${r.detail}</span></div>`).join('')}

async function loadRuntime(){
  const [rt,paper]=await Promise.all([api('/api/runtime'),api('/api/paper')]);
  const services=rt.services||[];
  $('#runtime-services').innerHTML=services.length?services.map(s=>`<article class="card"><div class="card-head"><b>${s.service}</b><span>${s.status}</span></div><p class="muted">last ok: ${s.last_ok||'—'}<br>error: ${s.last_error||'—'}</p></article>`).join(''):'<article class="card"><b>No service heartbeat yet.</b></article>';
  $('#paper-accounts').innerHTML=(paper.accounts||[]).map(a=>`<div class="metric-row"><b>${a.track}</b><span>${money(a.cash)}</span></div>`).join('')||'—';
  $('#paper-positions').innerHTML=(paper.positions||[]).map(p=>`<div class="metric-row"><b>${p.track} · ${p.ticker} · ${p.qty}</b><span>${money(p.avg_cost)}</span></div>`).join('')||'No positions yet.';
  $('#agent-reports').innerHTML=(rt.agent_reports||[]).map(r=>`<div class="audit-row"><time>${r.ts.replace('T',' ')}</time><b>${r.role}</b><span>${r.provider} · ${r.model||'default'} · ${r.status}</span></div>`).join('')||'No agent runs yet.';
}

async function boot(){initTabs();initScoreLab();await Promise.all([loadCockpit(),loadHandbook(),loadAudit(),loadRuntime()]);$('#refresh-audit').onclick=loadAudit;}
boot().catch(e=>{console.error(e);$('#health').textContent='error';});
