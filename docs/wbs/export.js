// Exports the work breakdown to JSON so the application can compute progress without
// needing Node at runtime. Run alongside render.js whenever the WBS changes.
const fs = require("fs");
const path = require("path");

const MODS = [].concat(
  require(path.join(__dirname, "wbs-a.js")),
  require(path.join(__dirname, "wbs-b.js")),
  require(path.join(__dirname, "wbs-c.js")),
  require(path.join(__dirname, "wbs-d.js")),
  require(path.join(__dirname, "wbs-e.js")),
  require(path.join(__dirname, "wbs-f.js"))
);
const SCH = require(path.join(__dirname, "schedule.js"));

// Leaf numbering must match render.js exactly, or a commit closing M0.2.4 would tick a
// different box in the tracker than the one the status page counts.
function leafIds(node, prefix, out) {
  const kids = node.s || [];
  const keys = node.k || [];
  // render.js reads k-or-s; this reads s and then k. Identical while no node carries both,
  // and silently divergent the moment one does: the tracker would tick a different box than
  // the status page counts. Fail loudly rather than drift.
  if (kids.length && keys.length) {
    throw new Error(`${prefix} has both s and k children; render.js and export.js would number its leaves differently`);
  }
  kids.forEach((child, i) => {
    const id = `${prefix}.${i + 1}`;
    if (typeof child === "string") out.push(id);
    else leafIds(child, id, out);
  });
  keys.forEach((_, i) => out.push(`${prefix}.${kids.length + i + 1}`));
}

const modules = MODS.map((m) => {
  const ids = [];
  (m.tasks || []).forEach((t, i) => leafIds(t, `${m.id}.${i + 1}`, ids));
  // Only the leaves that differ from their module's wave, so the common case stays
  // absent rather than repeating the module wave 1148 times.
  const leaf_waves = {};
  const modWave = SCH.WAVE[m.id] ?? 0;
  ids.forEach((id) => {
    const w = (SCH.LEAF_WAVE || {})[id];
    if (w !== undefined && w !== modWave) leaf_waves[id] = w;
  });
  return {
    id: m.id,
    name: m.name,
    wave: modWave,
    leaf_ids: ids,
    leaf_waves,
  };
});

const out = {
  generated_by: "docs/wbs/export.js",
  start: SCH.START,
  wave_names: SCH.NAMES,
  modules,
};

fs.writeFileSync(path.join(__dirname, "..", "wbs.json"), JSON.stringify(out, null, 1));
const total = modules.reduce((a, m) => a + m.leaf_ids.length, 0);
console.log(`wrote wbs.json: ${modules.length} modules, ${total} leaves`);
