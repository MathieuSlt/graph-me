// Graph explorer: Sigma.js over graphology, laid out with ForceAtlas2 in the browser.
// Names come from personal data: they are only ever set as text (canvas labels, textContent).
import forceAtlas2 from "/static/vendor/graphology-layout-forceatlas2.esm.js";

const css = getComputedStyle(document.documentElement);
const KIND_COLOURS = Object.fromEntries(
  ["person", "project", "document", "org", "place"].map((k) => [k, css.getPropertyValue(`--${k}`).trim()]),
);
const GROUP_COLOURS = ["#2f5bd3", "#2f8f5b", "#c0392b", "#8e44ad", "#d68910", "#17a2b8",
                       "#e84393", "#6c5ce7", "#00b894", "#b33771", "#3d3d3d", "#a0522d"];

const container = document.getElementById("graph");
const form = document.getElementById("filters");
const statusEl = document.getElementById("graph-status");
const colourSel = document.getElementById("colour");
const hideMe = document.getElementById("hide-me");
const find = document.getElementById("find");
const panel = document.getElementById("panel");

const graph = new graphology.Graph({ type: "undirected", multi: false });
let renderer = null;
let selected = null;

function colourOf(attrs) {
  if (colourSel.value === "community") {
    return attrs.community == null ? "#9aa0a6" : GROUP_COLOURS[attrs.community % GROUP_COLOURS.length];
  }
  return KIND_COLOURS[attrs.kind] || "#9aa0a6";
}

function nodeAttrs(n, x, y) {
  return {
    label: n.name || "(unnamed)",
    kind: n.kind,
    community: n.community,
    mentions: n.mentions,
    isMe: n.is_me,
    size: 3 + Math.min(14, Math.log2(1 + n.mentions) * 2),
    x: x ?? Math.random() * 100,
    y: y ?? Math.random() * 100,
  };
}

function addData(data, around) {
  for (const n of data.nodes) {
    if (graph.hasNode(n.id)) continue;
    const x = around ? around.x + (Math.random() - 0.5) * 20 : undefined;
    const y = around ? around.y + (Math.random() - 0.5) * 20 : undefined;
    graph.addNode(n.id, nodeAttrs(n, x, y));
  }
  for (const e of data.edges) {
    if (e.source === e.target || !graph.hasNode(e.source) || !graph.hasNode(e.target)) continue;
    if (graph.hasEdge(e.source, e.target)) {
      graph.updateEdgeAttribute(e.source, e.target, "weight", (w) => (w || 0) + e.weight);
    } else {
      graph.addEdge(e.source, e.target, { weight: e.weight, label: e.type, size: 0.5 });
    }
  }
}

function layout(iterations) {
  // "me" is linked to almost everyone: laid out with the rest, it pulls them into one ring
  // around it. Lay out the others, then put "me" in the middle.
  const others = graph.copy();
  others.forEachNode((node, a) => { if (a.isMe) others.dropNode(node); });
  if (others.order > 1) {
    const settings = { ...forceAtlas2.inferSettings(others), barnesHutOptimize: others.order > 500 };
    forceAtlas2.assign(others, { iterations, settings });
  }
  let sx = 0, sy = 0;
  others.forEachNode((node, a) => {
    graph.mergeNodeAttributes(node, { x: a.x, y: a.y });
    sx += a.x; sy += a.y;
  });
  const n = Math.max(others.order, 1);
  graph.forEachNode((node, a) => { if (a.isMe) graph.mergeNodeAttributes(node, { x: sx / n, y: sy / n }); });
}

function reducers() {
  renderer.setSetting("nodeReducer", (node, attrs) => {
    const out = { ...attrs, color: colourOf(attrs) };
    if (hideMe.checked && attrs.isMe) out.hidden = true;
    if (selected) {
      if (node === selected) out.highlighted = true;
      else if (!graph.areNeighbors(node, selected)) { out.color = "#d8d8dc"; out.label = ""; }
    }
    return out;
  });
  renderer.setSetting("edgeReducer", (edge, attrs) => {
    const [s, t] = graph.extremities(edge);
    if (hideMe.checked && (graph.getNodeAttribute(s, "isMe") || graph.getNodeAttribute(t, "isMe"))) {
      return { ...attrs, hidden: true };
    }
    if (selected && s !== selected && t !== selected) return { ...attrs, hidden: true };
    return attrs;
  });
}

function showPanel(node) {
  const a = graph.getNodeAttributes(node);
  panel.hidden = false;
  document.getElementById("panel-name").textContent = a.label;
  document.getElementById("panel-meta").textContent =
    `${a.kind} · ${a.mentions} item${a.mentions === 1 ? "" : "s"} · ${graph.degree(node)} links shown`;
  document.getElementById("panel-open").href = `/entity/${encodeURIComponent(node)}`;
  const list = document.getElementById("panel-neighbours");
  list.replaceChildren();
  const neighbours = graph.neighbors(node)
    .map((n) => [n, graph.getNodeAttributes(n)])
    .sort((x, y) => y[1].mentions - x[1].mentions)
    .slice(0, 30);
  for (const [id, attrs] of neighbours) {
    const li = document.createElement("li");
    const link = document.createElement("a");
    link.href = `/entity/${encodeURIComponent(id)}`;
    link.textContent = attrs.label;
    link.className = `entity kind-${attrs.kind}`;
    li.append(link, ` · ${attrs.kind}`);
    list.append(li);
  }
}

function select(node) {
  selected = node;
  if (node) {
    showPanel(node);
    const { x, y } = renderer.getNodeDisplayData(node);
    renderer.getCamera().animate({ x, y, ratio: 0.5 }, { duration: 400 });
  } else {
    panel.hidden = true;
  }
  renderer.refresh();
}

async function getJSON(url) {
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function load() {
  statusEl.textContent = "loading…";
  const params = new URLSearchParams(new FormData(form));
  for (const [k, v] of [...params]) if (!v) params.delete(k);
  let data;
  try {
    data = await getJSON(`/api/graph?${params}`);
  } catch (err) {
    statusEl.textContent = `could not load the graph (${err.message})`;
    return;
  }
  selected = null;
  panel.hidden = true;
  graph.clear();
  addData(data);
  statusEl.textContent = "laying out…";
  await new Promise((r) => setTimeout(r)); // let the status paint before the layout blocks
  layout(graph.order > 1000 ? 80 : 150);
  if (!renderer) {
    renderer = new Sigma(graph, container, {
      labelRenderedSizeThreshold: 8,
      labelColor: { color: css.getPropertyValue("--fg").trim() || "#222" },
      defaultEdgeColor: "#c9c9cf",
      zIndex: true,
    });
    renderer.on("clickNode", ({ node }) => select(node));
    renderer.on("clickStage", () => select(null));
    renderer.on("doubleClickNode", ({ node, event }) => {
      event.preventSigmaDefault();
      window.location.href = `/entity/${encodeURIComponent(node)}`;
    });
    reducers();
  } else {
    renderer.refresh();
  }
  const shown = `${graph.order} of ${data.total_entities} entities`;
  statusEl.textContent = data.truncated ? `${shown} (most mentioned first; click a node to expand)` : shown;
  const wanted = decodeURIComponent(window.location.hash.slice(1));
  if (wanted) await focus(wanted);
}

async function expand(node) {
  const around = graph.getNodeAttributes(node);
  const data = await getJSON(`/api/graph/${encodeURIComponent(node)}`);
  addData(data, around);
  showPanel(node);
  renderer.refresh();
}

async function focus(id) {
  if (!graph.hasNode(id)) {
    try {
      addData(await getJSON(`/api/graph/${encodeURIComponent(id)}`), { x: 0, y: 0 });
    } catch {
      return;
    }
  }
  if (graph.hasNode(id)) select(id);
}

form.addEventListener("submit", (ev) => { ev.preventDefault(); load(); });
colourSel.addEventListener("change", () => renderer && renderer.refresh());
hideMe.addEventListener("change", () => renderer && renderer.refresh());
document.getElementById("panel-expand").addEventListener("click", () => selected && expand(selected));
find.addEventListener("keydown", (ev) => {
  if (ev.key !== "Enter") return;
  ev.preventDefault(); // Enter would submit the filters and reload the graph
  const q = find.value.trim().toLowerCase();
  if (!q) return;
  let best = null;
  graph.forEachNode((node, a) => {
    if (a.label.toLowerCase().includes(q) && (!best || a.mentions > graph.getNodeAttribute(best, "mentions"))) best = node;
  });
  if (best) select(best);
  else statusEl.textContent = `no node matches “${find.value}”`;
});

// Classic scripts (graphology, Sigma) are deferred; modules run after them.
load();
