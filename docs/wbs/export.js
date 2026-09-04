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
  return {
    id: m.id,
    name: m.name,
    wave: SCH.WAVE[m.id] ?? 0,
    leaf_ids: ids,
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
