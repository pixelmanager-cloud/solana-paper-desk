const $=s=>document.querySelector(s);const node=(tag,text,cls)=>{const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n};
const human=s=>s.toLowerCase().replaceAll('_',' ');
async function refresh(){try{const response=await fetch('/api/scans');if(!response.ok)throw Error('Dashboard unavailable');const scans=await response.json();$('#scans').replaceChildren();if(!scans.length)$('#scans').append(node('p','No investigations yet. Paste a mint above to start.'));for(const scan of scans){const a=node('article','');a.append(node('code',scan.mint));a.append(node('p',scan.status+' · '+new Date(scan.created*1000).toLocaleString(),'status'));const r=scan.result;if(r){if(r.error)a.append(node('p',r.error));else{a.append(node('h3','Research-only evidence · not a paper entry'));a.append(node('p','Original investigation observation: '+decisionTime(r.observed_at)));a.append(node('p','Observation age at display: '+decisionAge(r.observed_at,Date.now())));const grid=node('div','','grid');for(const [title,items]of[['Findings',r.findings],['Unresolved checks',r.unknowns]]){const box=node('div','');box.append(node('h4',title));const ul=node('ul','');for(const item of items||[])ul.append(node('li',human(item)));if(!items?.length)ul.append(node('li','None observed in this bounded sample; this is not a safety pass.'));box.append(ul);grid.append(box)}a.append(grid);if(r.holder_evidence)a.append(node('p',r.holder_evidence.verified?`Holder coverage: saved chain snapshot at slot ${r.holder_evidence.slot}. Wallet and pool classification remains incomplete.`:'Holder coverage: provisional sample or indexed data; complete chain coverage is unverified.'));if(r.roundtrip_quote)a.append(node('p',`Round-trip quote: ${(Number(r.roundtrip_quote.quoted_return_lamports)/1e9).toFixed(6)} SOL returned from 0.01 SOL · quotes only; not simulated fills`));if(r.sell_simulation)a.append(node('p',r.sell_simulation.simulation_ok&&r.sell_simulation.balance_effects?.passed&&r.sell_simulation.account_controls?.passed?'Public-holder sell simulation, balance checks and account-control checks passed. Full route policy and your wallet are not approved.':'Public-holder sell diagnostics failed or have unresolved checks.'));a.append(node('p',`${r.history?.transactions||0} historical transactions · ${r.holders?.length||0} reported holder owners · ${r.shared_funding_candidates?.length||0} shared-funding candidates`));}const d=node('details','');d.append(node('summary','Inspect full evidence report'));d.append(node('pre',JSON.stringify(r,null,2)));a.append(d)}$('#scans').append(a)}}catch(e){$('#message').textContent=e.message}}
$('#scan').addEventListener('submit',async e=>{e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;try{const response=await fetch('/api/scans',{method:'POST',headers:{'Content-Type':'application/json','X-Desk-Request':'1'},body:JSON.stringify({mint:$('#mint').value.trim()})});const r=await response.json();if(!response.ok)throw Error(r.error||'Could not start scan');$('#message').textContent='Queued. A bounded investigation can take a few minutes.';await refresh()}catch(e){$('#message').textContent=e.message}finally{b.disabled=false}});$('#refresh').addEventListener('click',refresh);refresh();setInterval(refresh,5000);

async function launches(){try{const r=await fetch('/api/launches');if(!r.ok)return;const data=await r.json();$('#launches').replaceChildren();if(!data.launches.length)$('#launches').append(node('p','No launches captured yet.'));for(const item of data.launches.slice(0,8)){const row=node('p','');const b=node('button',item.mint,'secondary mint');b.addEventListener('click',()=>{$('#mint').value=item.mint;$('#mint').focus();$('#scan').scrollIntoView({behavior:'smooth'})});row.append(b);row.append(node('small','  '+new Date(item.received_at*1000).toLocaleTimeString()));$('#launches').append(row)}}catch{}}launches();setInterval(launches,30000);

const paperValue=value=>typeof value==='string'&&value.trim()?value:'unavailable';
const paperLabel=value=>typeof value==='string'&&value?human(value):'unknown';
function paperOutcome(item,openMints){
  const outcome=item.outcome;
  const row=node('article','');
  let title='Saved simulated outcome';
  if(outcome.type==='fill')title=outcome.side==='buy'?'Simulated entry fill':outcome.side==='sell'?'Simulated sell fill':'Unrecognized simulated fill';
  if(outcome.type==='blocked_exit')title='Unresolved simulated exit outcome';
  if(outcome.type==='reject')title='Rejected paper event';
  if(outcome.type==='control')title='Saved paper control outcome';
  row.append(node('h4',title+(outcome.mint?' · '+outcome.mint:'')));
  row.append(node('p','Recorded event time: '+decisionTime(item.ts)));
  if(outcome.reason)row.append(node('p','Recorded reason: '+outcome.reason));
  if(Array.isArray(outcome.reasons)&&outcome.reasons.length)row.append(node('p','Recorded evidence blockers: '+outcome.reasons.join('; ')));
  if(outcome.type==='fill'){
    row.append(node('p','Simulated quantity: '+paperValue(outcome.quantity)+' tokens · fee: '+paperValue(outcome.fee_sol)+' SOL'));
    if(outcome.side==='buy')row.append(node('p','Recorded entry amount: '+paperValue(outcome.amount_sol)+' SOL · provenance: '+paperValue(outcome.provenance)));
    if(outcome.side==='sell'){
      row.append(node('p','Recorded net proceeds: '+paperValue(outcome.proceeds_sol)+' SOL · allocated simulated realized PnL: '+paperValue(outcome.realized_pnl_sol)+' SOL'));
      row.append(node('p',openMints.has(outcome.mint)?'An open simulated position for this mint remains at the saved checkpoint; this sell record does not certify full closure.':'No simulated position for this mint is open at the saved checkpoint. Full closed-position records are not provided.'));
    }
    row.append(node('p','Simulated accounting outcome, not a submitted transaction.','muted'));
  }
  if(outcome.type==='blocked_exit')row.append(node('p','No simulated sell fill is recorded by this outcome; do not treat it as a completed exit.'));
  if(outcome.type==='reject')row.append(node('p','This rejection records no simulated entry fill.'));
  return row;
}
function renderPaper(data){
  const box=$('#paper');
  box.replaceChildren(node('p','Paper-only simulated accounting. Research evidence and rejected candidates are not paper positions.','muted'));
  if(data.notice)box.append(node('p',data.notice,'muted'));
  const missing={NOT_CONFIGURED:'No active simulated paper ledger configured.',EMPTY_LEDGER:'Simulated ledger has no saved checkpoint.',LEDGER_UNAVAILABLE:'Simulated ledger unavailable.',RECOVERY_REQUIRED:'Simulated ledger recovery required; saved accounting cannot be confirmed.'};
  if(data.status!=='LEDGER_PRESENT'){
    box.append(node('p',(missing[data.status]||'Paper status unavailable.')+' No balances or positions can be confirmed.'));
    return;
  }
  if(!Array.isArray(data.positions)||!Array.isArray(data.recent_outcomes))throw Error('Invalid paper projection');
  box.append(node('p',`Saved strategy mode: ${paperLabel(data.strategy_mode)} · runner reported: ${paperLabel(data.runner_status)}. This checkpoint is not continuous monitoring status.`));
  box.append(node('p','Automatic paper entries: '+(data.automatic_entry_enabled===false?'disabled.':'status unavailable; no permission established.')));
  box.append(node('p','Saved ledger checkpoint time: '+decisionTime(data.last_event_at)));
  box.append(node('p',`Simulated cash: ${paperValue(data.cash_sol)} SOL · simulated realized PnL: ${paperValue(data.realized_pnl_sol)} SOL`));
  const unresolved=data.positions.some(position=>position.exit_blocked||position.mark_status!=='MODEL_ESTIMATE');
  const estimated=data.valuation_status==='MODEL_ESTIMATE'&&!unresolved&&typeof data.estimated_equity_sol==='string';
  box.append(node('p',estimated?'Model-only simulated equity: '+paperValue(data.estimated_equity_sol)+' SOL; no executable valuation is certified.':'Simulated equity withheld: valuation is stale, unavailable or an exit is unverified.'));
  box.append(node('h4','Simulated open positions at the saved checkpoint'));
  if(!data.positions.length)box.append(node('p','No simulated positions open at this checkpoint; this is not a complete closed-position history.'));
  const openMints=new Set(data.positions.map(position=>position.mint));
  for(const position of data.positions){
    const row=node('article','');
    row.append(node('h4','Simulated open position · '+position.mint));
    row.append(node('p',`${paperValue(position.quantity)} tokens · remaining allocated cost: ${paperValue(position.cost_left_sol)} SOL`));
    row.append(node('p','Provenance: '+paperValue(position.provenance)));
    row.append(node('p','Saved mark time: '+decisionTime(position.mark_at)));
    const age=Number.isSafeInteger(position.mark_age_seconds)&&position.mark_age_seconds>=0?position.mark_age_seconds.toLocaleString()+' seconds':'unavailable (invalid or future mark time)';
    row.append(node('p','Mark age at projection: '+age+' · mark status: '+paperLabel(position.mark_status)));
    row.append(node('p','Last saved model estimate (unverified): '+paperValue(position.last_model_value_sol)+' SOL'));
    if(position.exit_blocked)row.append(node('p','Unresolved simulated exit: '+position.exit_blocked+'. Position remains open; an exit fill is not confirmed.','status'));
    if(position.mark_status==='STALE')row.append(node('p','Stale valuation: current model PnL is withheld.','status'));
    if(position.mark_status==='MODEL_ESTIMATE'&&!position.exit_blocked&&typeof position.unrealized_pnl_sol==='string')row.append(node('p','Model-only unrealized PnL: '+position.unrealized_pnl_sol+' SOL; valuation is not verified.'));
    else row.append(node('p','Model unrealized PnL withheld; no current executable value can be confirmed.'));
    box.append(row);
  }
  const outcomes=node('details','');
  outcomes.append(node('summary','Recent simulated fills, rejections and unresolved exits'));
  outcomes.append(node('p','Bounded recent outcome window (up to 50 records), not complete closed-position or trade history.','muted'));
  if(!data.recent_outcomes.length)outcomes.append(node('p','No recent outcomes available in this projection.'));
  for(const item of data.recent_outcomes.slice(0,50))outcomes.append(paperOutcome(item,openMints));
  box.append(outcomes);
  const details=node('details','');
  details.append(node('summary','Ledger identity and raw recent outcomes'));
  details.append(node('pre',JSON.stringify({last_event_at:data.last_event_at,config_hash:data.config_hash,implementation_hash:data.implementation_hash,recent_outcomes:data.recent_outcomes},null,2)));
  box.append(details);
}
async function paper(){
  try{
    const response=await fetch('/api/paper');
    if(!response.ok)throw Error('Paper ledger unavailable');
    renderPaper(await response.json());
  }catch{$('#paper').textContent='Paper status unavailable; no current balances, positions or exits can be confirmed.'}
}
paper();setInterval(paper,15000);

function decisionDate(value){
  if(!Number.isSafeInteger(value)||value<0)return null;
  const date=new Date(value*1000);
  return Number.isFinite(date.getTime())?date:null;
}
function decisionTime(value){
  const date=decisionDate(value);
  return date?date.toLocaleString(undefined,{timeZoneName:'short'}):'Unavailable (missing or invalid timestamp)';
}
function decisionAge(observedAt,displayedAt){
  if(!decisionDate(observedAt))return 'Unavailable (missing or invalid observation time)';
  if(!Number.isSafeInteger(displayedAt)||displayedAt<0||!Number.isFinite(new Date(displayedAt).getTime()))return 'Unavailable (invalid browser clock)';
  const age=Math.floor(displayedAt/1000)-observedAt;
  if(age<0)return 'Unavailable (observation time is in the future relative to this browser clock)';
  return age.toLocaleString()+' seconds; calculated from the original observation and this browser clock';
}
function decisionBox(){
  let box=$('#entry-decisions');
  if(!box){box=node('section','');box.id='entry-decisions';$('#paper').after(box)}
  return box;
}
function renderDecisions(data,displayedAt=Date.now()){
  if(!Array.isArray(data.decisions))throw Error('Invalid decision projection');
  const box=decisionBox();
  box.replaceChildren(node('h3','Entry evidence decisions'),node('p','Immutable historical evaluations. Current evidence health is not continuously certified; these records do not grant current entry approval.','muted'));
  if(data.status!=='EVIDENCE_GATES_CONNECTED'){
    box.append(node('p',data.status==='NOT_CONFIGURED'?'No decision journal configured.':'Historical evaluations unavailable.'));
    return;
  }
  for(const decision of data.decisions){
    const row=node('details','');
    row.append(node('summary',`${decision.decision} · ${decision.mint} · historical evaluation`));
    if(decision.decision==='REJECT')row.append(node('p','Rejected candidate; this evaluation does not create a simulated entry. Paper positions are shown separately.'));
    row.append(node('p','Original observation time: '+decisionTime(decision.observed_at)));
    row.append(node('p','Observation age at display: '+decisionAge(decision.observed_at,displayedAt)));
    row.append(node('p','Historical evaluation time: '+decisionTime(decision.evaluated_at)));
    for(const [name,gate] of Object.entries(decision.entry_evidence.gates)){
      const verified=gate.status==='VERIFIED_COMPONENT';
      const scope=typeof gate.scope==='string'&&gate.scope.trim()?gate.scope:'No scope recorded; no broader verification is implied.';
      const label=verified?'VERIFIED_COMPONENT (historical component only)':human(gate.status);
      const reasons=gate.reasons.length?' — '+gate.reasons.map(human).join('; '):'';
      row.append(node('p',`${human(name)}: ${label}${reasons}${verified||gate.scope?' · Scope: '+scope:''}`));
    }
    if(decision.entry_evidence.continued_ownership_history){
      const h=decision.entry_evidence.continued_ownership_history;
      row.append(node('p',`Continued ownership history: ${h.observed_transaction_count} transactions; ${h.account_queries.verified}/${h.account_queries.required} account queries verified; ${decision.entry_evidence.ownership_requests_used}/18 investigation RPC requests used. Historical evidence only.`));
    }
    row.append(node('p','Historical entry blockers: '+decision.reasons.map(human).join('; ')));
    box.append(row);
  }
  if(!data.decisions.length)box.append(node('p','No historical evaluations available yet.'));
}
async function decisions(){
  try{
    const response=await fetch('/api/decisions');
    if(!response.ok)throw Error('Historical decisions unavailable');
    renderDecisions(await response.json());
  }catch{
    decisionBox().replaceChildren(node('h3','Entry evidence decisions'),node('p','Historical decisions unavailable. Current evidence health is not continuously certified.'));
  }
}
decisions();setInterval(decisions,15000);
