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
const page = readFileSync(join(ROOT, 'ferry-dash'), 'utf8');
const script = page.match(/<script>([\s\S]*)<\/script>/)?.[1];
if (!script) { console.error('FAIL: no <script> block found in ferry-dash'); process.exit(1); }

const PURE_START = '// ── pure order helpers';
const PURE_END = '// ── end pure order helpers';
const pureSrc = script.slice(script.indexOf(PURE_START), script.indexOf(PURE_END));
if (!pureSrc.includes('moveHop')) { console.error('FAIL: pure-helper markers not found'); process.exit(1); }
const pure = vm.runInNewContext(pureSrc + ';({moveHop,dropIndexFromY,availHops,sameJSON})');

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

console.log('wiring greps on the ferry-dash <script> block:');
check('preview posts the unified endpoint', script.includes("'/api/routes/order/preview'"));
check('apply posts the unified endpoint', script.includes("'/api/routes/order/apply'"));
check('legacy /api/routes/preview is not called', !script.includes("'/api/routes/preview'"));
check('legacy /api/routes/apply is not called', !script.includes("'/api/routes/apply'"));
check('request body is {order: EDIT}', script.includes('JSON.stringify({order:EDIT})'));
check('the primary row is rendered pinned (no draggable on it)',
  script.includes("'hop hoprow primary'") && !script.includes('p.draggable'));
check('fallback rows are draggable', script.includes('r.draggable=true'));
check('dragover refuses hops from other lanes',
  (script.match(/DRAG\.lane!==lane/g) || []).length >= 2);
check('drop routes through moveHop', script.includes('EDIT[lane]=moveHop(order,DRAG.from,idx)'));
check('↑/↓ keyboard path routes through moveHop too', script.includes('EDIT[lane]=moveHop(order,i,i-1)'));
check('a live gesture defers the 5s re-render', script.includes('if(DRAG||PICKER_OPEN) return;'));
check('the pinned-primary hint exists', script.includes('pinHint(sec)'));
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
check('the route editor falls back to the plain lane list when no fleet owns a lane', script.includes('let orderedLanes=lanes;'));
check('a fleet selection the yaml no longer has is shown as unknown, not as the first fleet', script.includes("' (unknown)'"));

console.log('');
if (fails) { console.error(runs + ' checks, ' + fails + ' FAILED'); process.exit(1); }
console.log(runs + ' checks passed');
