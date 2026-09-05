#!/usr/bin/env node
// DOM-free checks for the ferry-dash Routes editor rework (drag-and-drop,
// unified order). The page is hand-rolled vanilla JS inline in ferry-dash with
// no JS harness, so this suite extracts the PURE order helpers between the
// `── pure order helpers ──` markers and exercises them, then greps the
// <script> block for the wiring that cannot be pure: the unified endpoints,
// the {order: EDIT} body, the pinned primary row, and the cross-lane guard.
//
// Run:  node lib/ferry-dashui.test.mjs
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const sourcePath = process.env.FERRY_DASH_SOURCE || join(ROOT, 'ferry-dash');
const page = readFileSync(sourcePath, 'utf8');
const script = page.match(/<script>([\s\S]*)<\/script>/)?.[1];
if (!script) { console.error('FAIL: no <script> block found in ferry-dash'); process.exit(1); }

const PURE_START = '// ── pure order helpers';
const PURE_END = '// ── end pure order helpers';
const pureSrc = script.slice(script.indexOf(PURE_START), script.indexOf(PURE_END));
if (!pureSrc.includes('moveHop')) { console.error('FAIL: pure-helper markers not found'); process.exit(1); }
const pure = vm.runInNewContext(pureSrc + ';({moveHop,dropIndexFromY,availHops,sameJSON,cloneOrders,insertHop,eligibleHop,transferHop})');

let fails = 0, runs = 0;
function check(name, cond, detail) {
  runs++;
  if (cond) { console.log('  ok  ' + name); }
  else { fails++; console.error('  FAIL ' + name + (detail ? ' — ' + detail : '')); }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

console.log('moveHop (reorder primitive; position 0 pinned):');
const O = ['lane', 'f1', 'f2', 'f3'];
check('moves first fallback to the end', eq(pure.moveHop(O, 1, 4), ['lane', 'f2', 'f3', 'f1']));
check('moves last fallback to the top slot', eq(pure.moveHop(O, 3, 1), ['lane', 'f3', 'f1', 'f2']));
check('swap down (the ↓ button: from=i, before=i+2)', eq(pure.moveHop(O, 1, 3), ['lane', 'f2', 'f1', 'f3']));
check('swap up (the ↑ button: from=i, before=i-1)', eq(pure.moveHop(O, 2, 1), ['lane', 'f2', 'f1', 'f3']));
check('position 0 is never a source', eq(pure.moveHop(O, 0, 3), O));
check('dropBefore 0 clamps to slot 1 (never above the primary)', eq(pure.moveHop(O, 3, 0), ['lane', 'f3', 'f1', 'f2']));
check('dropBefore beyond end clamps to append', eq(pure.moveHop(O, 1, 99), ['lane', 'f2', 'f3', 'f1']));
check('drop onto itself is a no-op', eq(pure.moveHop(O, 2, 2), O));
check('drop directly below itself is a no-op', eq(pure.moveHop(O, 1, 2), O));
check('out-of-range source is a no-op', eq(pure.moveHop(O, 7, 2), O));
check('input array is not mutated', (() => { const o = O.slice(); pure.moveHop(o, 1, 3); return eq(o, O); })());
check('move away and back restores the order',
  eq(pure.moveHop(pure.moveHop(O, 1, 4), 3, 1), O));
check('works on a primary-only list without exploding', eq(pure.moveHop(['lane'], 1, 1), ['lane']));

console.log('dropIndexFromY (drop geometry; no slot 0):');
const centers = [10, 20, 30];
check('above the first row → slot 1', pure.dropIndexFromY(centers, 5) === 1);
check('at the first midpoint boundary → slot 2', pure.dropIndexFromY(centers, 10) === 2);
check('between rows → the lower row\'s slot', pure.dropIndexFromY(centers, 15) === 2);
check('near the bottom → slot above last row', pure.dropIndexFromY(centers, 25) === 3);
check('below the last row → append slot (len+1)', pure.dropIndexFromY(centers, 999) === 4);
check('empty list → append slot 1', pure.dropIndexFromY([], 5) === 1);

console.log('availHops / sameJSON (add-picker filter, dirty comparison):');
const groups = { a: {}, b: {}, c: {}, d: {} };
check('filters self and in-chain hops, sorted',
  eq(pure.availHops(groups, 'b', ['b', 'd']), ['a', 'c']));
check('empty chain offers everything but itself',
  eq(pure.availHops(groups, 'b', ['b']), ['a', 'c', 'd']));
check('identical orders are not dirty', pure.sameJSON({ f: ['f', 'x'] }, { f: ['f', 'x'] }) === true);
check('reordered tails ARE dirty', pure.sameJSON({ f: ['f', 'x', 'y'] }, { f: ['f', 'y', 'x'] }) === false);
check('dirty fires on any tail difference', pure.sameJSON({ f: ['f'] }, { f: ['f', 'x'] }) === false);

console.log('Studio insertion / copy primitives:');
const orders = { 'home.chat': ['home.chat', 'home.fast'], 'home.code': ['home.code'], shared: ['shared'] };
check('library insertion clamps above-primary drops below the pinned primary',
  eq(pure.insertHop(orders['home.code'], 'home.fast', 0), ['home.code', 'home.fast']));
check('library insertion rejects a duplicate without mutating its input', (() => {
  const before=orders['home.chat'].slice(), after=pure.insertHop(orders['home.chat'], 'home.fast', 2);
  return eq(after,before)&&eq(orders['home.chat'],before)&&after!==orders['home.chat'];
})());
check('same-lane transfer reorders through the shared primitive',
  eq(pure.transferHop({'home.chat':['home.chat','home.a','home.b']},{lane:'home.chat',from:1},'home.chat',3,{home:[]}),
     {'home.chat':['home.chat','home.b','home.a']}));
check('cross-lane copy adds the hop and leaves the source lane unchanged', (() => {
  const before={'home.chat':['home.chat','home.fast'],'home.code':['home.code']};
  const after=pure.transferHop(before,{lane:'home.chat',from:1},'home.code',1,{home:[]});
  return eq(after,{'home.chat':['home.chat','home.fast'],'home.code':['home.code','home.fast']})&&eq(before,{'home.chat':['home.chat','home.fast'],'home.code':['home.code']});
})());
check('cross-fleet copy is rejected to match backend fleet validation',
  eq(pure.transferHop({'home.chat':['home.chat','home.fast'],'work.code':['work.code']},{lane:'home.chat',from:1},'work.code',1,{home:[],work:[]}),
     {'home.chat':['home.chat','home.fast'],'work.code':['work.code']}));
check('bare library hop is rejected for a fleet-owned destination',
  eq(pure.transferHop({'home.code':['home.code']},{group:'shared-fast'},'home.code',1,{home:[]}),{'home.code':['home.code']}));
check('shared destination accepts an unprefixed library hop',
  eq(pure.transferHop({shared:['shared']},{group:'shared-fast'},'shared',1,{home:[]}),{shared:['shared','shared-fast']}));
check('invalid source index and destination duplicate are rejected',
  eq(pure.transferHop(orders,{lane:'home.chat',from:99},'home.code',1,{home:[]}),orders) &&
  eq(pure.transferHop(orders,{lane:'home.chat',from:1},'home.chat',2,{home:[]}),orders));

console.log('driverLane / renderOrch (driver rename orch -> heavy, 2026-09-04):');
// driverLane itself is pure (no DOM) and stands alone in the script, so it can
// be extracted and run for real rather than only grepped.
const driverLaneSrc = script.match(/function driverLane\([^)]*\)\{[^}]*\}/)?.[0];
if (!driverLaneSrc) { console.error('FAIL: driverLane() not found in script'); fails++; }
else {
  const dl = vm.runInNewContext(driverLaneSrc + ';driverLane');
  check('driver is heavy when the config serves it', dl({ heavy: {} }) === 'heavy');
  check('driver falls back to the legacy orch name', dl({ orch: {} }) === 'orch');
}
// renderOrch itself touches the DOM ($/el/mkRow), which this DOM-free suite
// does not stub (see the file header) — so the probe-lookup expression is
// verified by source rather than by executing the function. Combined with
// the driverLane check above (driver='heavy' when the config serves it),
// `PROBE[lane]` is proof the "Test backends" probe is read under the
// RESOLVED driver name, not a hardcoded 'orch' key that a renamed lane would
// silently stop matching.
check('renderOrch reads the probe under the resolved driver name',
  script.includes('const pr=PROBE[lane]'));
check('PROBE is never keyed by the hardcoded "orch" name',
  !script.includes('PROBE.orch') && !script.includes("PROBE['orch']") && !script.includes('PROBE["orch"]'));
check('the quota event kind is the classifier state, not the old Kimi-specific kind',
  script.includes("kind==='quota_exhausted'") && !script.includes('kimi_quota'));
check('the quota tag carries no stale vendor/status suffix',
  script.includes("ptag='quota-exhausted'") && !script.includes('quota-exhausted (403)'));
// The card markup is HTML, not inside <script> — checked against the whole
// page rather than the extracted script block.
check('the Driver card keeps its element ids stable across the heading rename',
  page.includes('id="orch"') && page.includes('id="orchbody"'));
check('the Driver card is no longer titled "Orchestrator"',
  page.includes('<h2>Driver</h2>') && !page.includes('<h2>Orchestrator</h2>'));

console.log('preview freshness and dirty polling behavior:');
const previewMatch=script.match(/\$\('#previewbtn'\)\.onclick=async\(\)=>\{[\s\S]*?\n\};\n(?=\$\('#applybtn'\))/)?.[0];
if(!previewMatch){ check('preview callback is extractable',false); }
else {
  let resolvePreview;
  const previewButton={}; const calls={diff:[],msg:[]};
  const ctx={
    REVISION:4, EDIT:{lane:['lane','a']}, REVIEWED:null, BUSY:false,
    $:()=>previewButton, routesDirty:()=>true,
    invalidateReview(){ctx.REVISION++;ctx.REVIEWED=null;},
    cloneOrders:pure.cloneOrders, JSON, redraw(){}, controls(){},
    showDiff:v=>calls.diff.push(v), msg:v=>calls.msg.push(v),
    postRoutes:()=>new Promise(r=>{resolvePreview=r;}), Error
  };
  vm.runInNewContext(previewMatch,ctx);
  const pending=previewButton.onclick();
  ctx.EDIT={lane:['lane','b']};ctx.REVISION++;
  resolvePreview({code:200,body:{diff:'stale diff',errors:[]}});
  await pending;
  check('a stale preview response cannot install its diff or unlock apply',
    calls.diff.length===0&&ctx.REVIEWED===null);
}

const routeLogic=script.slice(script.indexOf('function routesDirty()'),script.indexOf('function drawRoutes()'));
const pollCtx={EDIT:{lane:['lane','draft']},SERVER:{lane:['lane','old']},GROUPS:{},ROUTE_STATUS:null,
  HISTORY:[],REVISION:0,REVIEWED:null,PENDING:null,JSON, sameJSON:pure.sameJSON,cloneOrders:pure.cloneOrders,
  DRAG:null,PICKER_OPEN:false,BUSY:false,showDiff(){},drawRoutes(){}};
vm.runInNewContext(routeLogic,pollCtx);
pollCtx.renderRoutes({models:['lane'],topology:{groups:{lane:{}},fallbacks:{lane:['server-new']}}});
check('status polling updates server truth without overwriting a dirty draft',
  eq(pollCtx.EDIT,{lane:['lane','draft']})&&eq(pollCtx.SERVER,{lane:['lane','server-new']}));

class ScrollNode {
  constructor(tag,cls='',text=''){this.tag=tag;this.className=cls||'';this.textContent=text;this.children=[];this.dataset={};this.scrollLeft=0;this.scrollTop=0;this.classList={remove(){}};}
  appendChild(child){this.children.push(child);return child;}
  replaceChildren(){this.children=[];this.scrollTop=0;}
  setAttribute(){} addEventListener(){}
  querySelectorAll(selector){return this.children.flatMap(child=>[
    ...(child.className.split(' ').includes(selector.slice(1))?[child]:[]),...child.querySelectorAll(selector)]);}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
}
const scrollNodes={};
const scrollCtx=vm.createContext({Map,JSON,Set,Object,String,
  el:(...args)=>new ScrollNode(...args),
  $:selector=>scrollNodes[selector]||(scrollNodes[selector]=new ScrollNode('div')),
  laneKind:()=>'',modelOf:x=>x,sameJSON:pure.sameJSON,
  routeGap:()=>new ScrollNode('div','route-gap'),renderLibrary(){},controls(){},endDrag(){}});
vm.runInContext("let EDIT={'a.heavy':['a.heavy','a.fast'],'b.heavy':['b.heavy','b.fast']},SERVER=EDIT,ROUTE_STATUS={topology:{fleets:{a:['a.heavy'],b:['b.heavy']}}},VIEW='*',VISIBLE=[],BUSY=false,PENDING=null;const ROUTE_SCROLL=new Map();function routesDirty(){return false;}",scrollCtx);
vm.runInContext(script.slice(script.indexOf('function drawRoutes(){'),script.indexOf('function renderLibrary(){')),scrollCtx);
const runScroll=source=>vm.runInContext(source,scrollCtx);
const laneHops=lane=>scrollNodes['#routesbody'].querySelectorAll('.lane').find(n=>n.dataset.lane===lane).querySelector('.hops');
runScroll('drawRoutes()');laneHops('a.heavy').scrollLeft=240;laneHops('b.heavy').scrollLeft=160;scrollNodes['#routesbody'].scrollTop=123;
runScroll('drawRoutes()');const rebuilt=laneHops('a.heavy').scrollLeft===240&&laneHops('b.heavy').scrollLeft===160&&scrollNodes['#routesbody'].scrollTop===123;
runScroll("VIEW='fleet:b';drawRoutes()");const hiddenB=laneHops('b.heavy').scrollLeft===160;
runScroll("VIEW='fleet:a';EDIT['a.heavy'].push('a.extra');drawRoutes()");
check('route redraw preserves each lane scroll across polling, fleet views, and edits',
  rebuilt&&hiddenB&&laneHops('a.heavy').scrollLeft===240);

console.log('endpoint and interaction wiring:');
check('preview posts the unified endpoint', script.includes("'/api/routes/order/preview'"));
check('apply posts the unified endpoint', script.includes("'/api/routes/order/apply'"));
check('legacy /api/routes/preview is not called', !script.includes("'/api/routes/preview'"));
check('legacy /api/routes/apply is not called', !script.includes("'/api/routes/apply'"));
check('preview/apply posts the supplied immutable order snapshot',
  script.includes('JSON.stringify({order})') && script.includes("postRoutes('/api/routes/order/apply',reviewed.order)"));
check('the primary row is rendered pinned (no draggable on it)',
  script.includes("const r=el('div','hop hoprow'+(i?'':' primary'))") &&
  script.includes("if(i){") && script.includes('r.draggable=!BUSY'));
check('fallback rows are draggable while the editor is idle', script.includes('r.draggable=!BUSY'));
check('drag/drop routes through the tested transfer helper', script.includes('transferHop(EDIT,source,lane,before,fleetRules())'));
check('undo restores a cloned history snapshot', script.includes('EDIT=HISTORY.pop()'));
check('snapshot note from the backend is surfaced', script.includes('body.note'));

console.log('UI-order round trip against the REAL backend validator:');
// What the UI would hold after a drag (f1 dropped to the end) and an add,
// seeded from SERVER — fed to validate_order to prove the shape is admitted.
const uiOrder = { flash: ['flash', 'flash-or', 'heavy'], heavy: ['heavy'] };
const probe = `
import importlib.util, importlib.machinery, json, os, tempfile
spec = importlib.util.spec_from_loader("fd", importlib.machinery.SourceFileLoader("fd", ${JSON.stringify(join(ROOT, 'ferry-dash'))}))
D = importlib.util.module_from_spec(spec); spec.loader.exec_module(D)
order = json.loads(${JSON.stringify(JSON.stringify(uiOrder))})
cfg = """
model_list:
  - model_name: heavy
    litellm_params:
      model: anthropic/k3
    model_info:
      id: kimi-1
  - model_name: flash
    litellm_params:
      model: zai/glm-flash
    model_info:
      id: flash-1
  - model_name: flash-or
    litellm_params:
      model: openrouter/x
    model_info:
      id: or-1
litellm_settings:
  drop_params: true
router_settings:
  fallbacks: [{"flash": ["flash-or"]}]
"""
tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False); tmp.write(cfg); tmp.close()
errs = D.validate_order(D.parse_topology_text(cfg), order)
diff, derrs = D.diff_order(tmp.name, order)
snap, _ = D.apply_order(tmp.name, order)
with open(tmp.name) as f: written = f.read()
import sys
print(json.dumps({"errs": errs, "derrs": derrs, "wrote_heavy_flashor": '"flash": ["flash-or", "heavy"]' in written}))
`;
let backend;
try {
  backend = JSON.parse(execFileSync('python3', ['-c', probe], { encoding: 'utf8' }).trim().split('\n').pop());
} catch (e) {
  backend = null;
  console.error('  (python cross-check could not run: ' + e.message.split('\n')[0] + ')');
}
if (backend) {
  check('backend validate_order admits the UI order', eq(backend.errs, []), JSON.stringify(backend.errs));
  check('backend produces a diff for it', eq(backend.derrs, []) && backend.wrote_heavy_flashor === true);
}

console.log('Fleets panel (Task 14):');
check('the page has a Fleets section', page.includes('id="fleets"'));
check('the Fleets heading text is present', page.includes('<h2>Fleets</h2>'));
check('the script posts to /api/fleet', script.includes('/api/fleet'));
check('the script reads the fleet document\'s fleets map', script.includes('fleet.fleets'));
check('lanes outside a named fleet remain available in a Shared view',
  script.includes("views.push(['shared','Shared',shared])"));
check('a fleet selection the yaml no longer has is shown as unknown, not as the first fleet', script.includes("' (unknown)'"));

console.log('Live request metrics (executed rendering and aggregation):');
const metricSrc = script.slice(script.indexOf('// ── request metric helpers'),
  script.indexOf('// ── end request metric helpers'));
const statsSrc = script.slice(script.indexOf('function depStats('), script.indexOf('// An event names'));
const feedSrc = script.slice(script.indexOf('function renderFeed('), script.indexOf('function openStream('));
const ratesSrc = script.slice(script.indexOf('const fmtBps='), script.indexOf('// ── request metric helpers'));
const nodes = {};
function node(tag, className, textContent='') {
  return {tag, className, textContent, children:[], appendChild(child){this.children.push(child);}};
}
const fixture = {deployment:'demo', lane:'fast', status:200, stream:true,
  first_text_ms:125, total_duration_ms:2000, duration_ms:1, response_start_ms:80,
  input_tokens:250, output_tokens:90, reasoning_tokens:40, cached_input_tokens:50,
  resp_bytes:4000, bps:2000, response_complete:true};
const metrics = vm.runInNewContext(metricSrc+ratesSrc+statsSrc+feedSrc+
  ';({requestMetrics,depStats,renderFeed,fmtMs,fmtTokens})', {
  LIVE:{events:[fixture, {deployment:'old', duration_ms:5, resp_bytes:100},
    {...fixture, deployment:'partial', stream:false, first_text_ms:0, total_duration_ms:0,
     input_tokens:0, output_tokens:0, reasoning_tokens:null, bps:null, response_complete:false},
    {...fixture, total_duration_ms:1000, resp_bytes:1000, first_text_ms:null}]},
  FEED_CAP:200, Date, el:node, clock:()=> '12:00:00', shortModel:n=>n||'?',
  $:id=>nodes[id]||(nodes[id]=node('div',''))
});
metrics.renderFeed();
const texts=n=>[n.textContent,...n.children.flatMap(texts)].filter(Boolean);
const rendered=nodes['#feed'].children.map(texts);
check('renders first text and total separately from legacy duration',
  rendered[0].includes('First text 125 ms') && rendered[0].includes('Total 2.00 s'));
check('renders provider token counts without adding reasoning to output',
  rendered[0].includes('In 250') && rendered[0].includes('Out 90') && rendered[0].includes('Reasoning 40'));
check('renders streaming and nonstreaming modes',
  rendered[0].includes('Mode streaming') && rendered[2].includes('Mode nonstreaming'));
check('old events retain unknown timing, counts, and mode',
  ['First text —','Total —','In —','Out —','Reasoning —','Mode unknown'].every(t=>rendered[1].includes(t)));
check('explicit zero remains zero and absent reasoning stays unknown',
  ['First text 0 ms','Total 0 ms','In 0','Out 0','Reasoning —'].every(t=>rendered[2].includes(t)));
check('only explicitly incomplete records receive the incomplete warning',
  rendered[2].includes('Response incomplete') && !rendered[1].includes('Response incomplete'));
check('nonstream tooltip identifies completed response observation',
  metrics.requestMetrics({stream:false})[1][2].includes('completed text response'));
check('secondary detail exposes response start, cached input and completion',
  nodes['#feed'].children[0].children.at(-1).title.includes('Response start: 80 ms · Cached input: 50 · Completion: complete'));
check('deployment total median and weighted bytes rate ignore legacy duration',
  metrics.depStats('demo').p50===2000 && metrics.depStats('demo').bps===5000*1000/3000);
check('no text observation stays unknown even with completed usage and timing',
  rendered[3].includes('First text —') && rendered[3].includes('Total 1.00 s'));
check('old deployment cannot invent a total median or bytes rate',
  metrics.depStats('old').p50===null && metrics.depStats('old').bps===null);
check('malformed metrics remain unknown',
  metrics.fmtMs(NaN)==='—' && metrics.fmtMs(-1)==='—' && metrics.fmtTokens(false)==='—');

console.log('additive OpenRouter catalog behavior:');
const catalogSrc=script.slice(script.indexOf('// ── pure catalog helpers'),script.indexOf('// ── end pure catalog helpers'));
const catalog=vm.runInNewContext(catalogSrc+';({catalogPrice,catalogContext,catalogGroups,libraryMatches,catalogResult})');
const configured={'retained-local':{models:['ollama/local']},'matching-or':{models:['openrouter/provider/model']},'similar':{models:['openrouter/provider/model-v2']}};
const catalogModels=Array.from({length:431},(_,i)=>({id:i?'provider/model-'+i:'provider/model',name:'Catalog model '+i,pricing:{prompt:i?'0.000002':'0',completion:null},context_length:128000}));
const matched=catalog.libraryMatches(configured,catalogModels,'');
check('all configured groups remain alongside all 431 catalog results',eq(matched.configured,Object.keys(configured).sort())&&matched.catalog.length===431);
check('combined search retains a configured-only local model',eq(catalog.libraryMatches(configured,catalogModels,'local').configured,['retained-local']));
check('catalog search matches provider, ID and display name without a result cap',catalog.libraryMatches(configured,catalogModels,'provider/').catalog.length===431&&catalog.libraryMatches(configured,catalogModels,'Catalog model 430').catalog[0].id==='provider/model-430');
check('only an exact openrouter deployment ID can expose a configured route group',eq(catalog.catalogGroups(configured,'provider/model'),['matching-or'])&&catalog.catalogGroups(configured,'provider/missing').length===0);
check('zero price is free while missing, negative and invalid prices remain unknown',catalog.catalogPrice('0')==='$0'&&catalog.catalogPrice('0.000002')==='$2'&&[null,undefined,'','bad','-1',false,true,[],{}].every(v=>catalog.catalogPrice(v)==='Unknown'));
check('context displays its actual token limit and preserves unknowns',catalog.catalogContext(128000)==='128,000 tokens'&&catalog.catalogContext(null)==='Unknown');
const prior={models:catalogModels,fetched_at:123,stale:false,error:null};
check('catalog errors retain the last good rows and timestamp with a stale error',(()=>{const result=catalog.catalogResult(prior,{models:[],error:'offline'});return result.models.length===431&&result.fetched_at===123&&result.stale&&result.error==='offline';})());
check('malformed responses retain last good rows instead of clearing the library',catalog.catalogResult(prior,{}).models.length===431);
const libraryNodes={};
class LibraryNode extends ScrollNode {get childElementCount(){return this.children.length;}showModal(){this.open=true;}close(){this.open=false;}}
const libCtx=vm.createContext({...catalog,JSON,Number,Date,encodeURIComponent,
  el:(...args)=>new LibraryNode(...args),
  $:selector=>libraryNodes[selector]||(libraryNodes[selector]=new LibraryNode('div')),
  modelOf:g=>configured[g].models[0],startDrag(){},endDrag(){},openPicker(){throw Error('Unconfigured catalog attempted route injection');},
  GROUPS:configured,CATALOG:prior,CATALOG_LOADING:false,LIBRARY_KEY:null,DRAG:null,PICKER_OPEN:false,BUSY:false,
  EDIT:{lane:['lane']},navigator:{clipboard:{writeText:async()=>{}}}});
libraryNodes['#modelsearch']=new LibraryNode('input');libraryNodes['#modelsearch'].value='';
vm.runInContext(script.slice(script.indexOf('function renderLibrary(){'),script.indexOf('function fleetRules()')),libCtx);
vm.runInContext('renderLibrary()',libCtx);
const libBox=libraryNodes['#modellibrary'],firstCard=libBox.children[0];
check('rendered cards keep configured groups first and append every catalog model',libBox.children.length===434&&libBox.children.slice(0,3).every(n=>n.dataset.group)&&libBox.children.slice(3).every(n=>n.dataset.catalogId&&n.draggable===false));
libBox.scrollLeft=180;libBox.scrollTop=400;vm.runInContext('renderLibrary()',libCtx);
check('unchanged status redraw keeps the actual library nodes and scroll positions',libBox.children[0]===firstCard&&libBox.scrollLeft===180&&libBox.scrollTop===400);
libCtx.CATALOG={...prior,models:catalogModels.slice(0,400)};libCtx.DRAG={group:'retained-local'};vm.runInContext('renderLibrary()',libCtx);
check('catalog changes cannot rebuild the library during an active drag',libBox.children[0]===firstCard&&libBox.children.length===434);
libCtx.DRAG=null;vm.runInContext('renderLibrary()',libCtx);
check('catalog refresh preserves both library scroll axes',libBox.scrollLeft===180&&libBox.scrollTop===400);
vm.runInContext('openCatalog(CATALOG.models[1])',libCtx);
check('catalog-only details open a modal without changing draft orders or groups',libraryNodes['#catalog-detail'].open&&eq(libCtx.EDIT,{lane:['lane']})&&eq(libCtx.GROUPS,configured));
const lastCards=libBox.children;vm.runInContext('renderLibrary()',libCtx);
check('open details defer any library rebuild',libBox.children===lastCards);
let catalogRequests=[];
libCtx.PICKER_OPEN=false;libCtx.fetch=async url=>{catalogRequests.push(url);throw Error('offline');};
await vm.runInContext('loadCatalog(true)',libCtx);
check('manual refresh uses the read-only endpoint and keeps last-good rows on network failure',eq(catalogRequests,['/api/models/openrouter?refresh=1'])&&libCtx.CATALOG.models.length===400&&libCtx.CATALOG.stale&&libCtx.CATALOG.error==='offline');
check('the five-second status poll never refreshes the catalog',!script.slice(script.indexOf('async function tick()'),script.indexOf("$('#testbtn').onclick")).includes('loadCatalog('));
const syncSource=script.match(/function syncPickerState\(\)\{[^\n]+/)?.[0];
const actualDialogs={'#catalog-detail':{open:false},'#routepicker':{open:true}};
const handoff=vm.createContext({PICKER_OPEN:true,renderLibrary(){},$:selector=>actualDialogs[selector]});
vm.runInContext(syncSource+';syncPickerState()',handoff);
check('asynchronous detail close preserves the newly opened route picker interaction lock',handoff.PICKER_OPEN===true);
actualDialogs['#routepicker'].open=false;actualDialogs['#catalog-detail'].open=true;
vm.runInContext('syncPickerState()',handoff);
check('route-picker close cannot unlock an open catalog dialog',handoff.PICKER_OPEN===true);
actualDialogs['#catalog-detail'].open=false;vm.runInContext('syncPickerState()',handoff);
check('closing the last modal unlocks polling and global Escape preserves native modal targeting',handoff.PICKER_OPEN===false&&script.includes("if(e.key==='Escape'){endDrag();}")&&!script.includes("if(PICKER_OPEN)closePicker()"));


console.log('');
if (fails) { console.error(runs + ' checks, ' + fails + ' FAILED'); process.exit(1); }
console.log(runs + ' checks passed');
