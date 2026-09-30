/**
 * F.R.I.D.A.Y. — components/Galaxy.jsx
 * The Obsidian vault (memory/graph_export.py) as a 3D force-directed
 * graph — the "always one taskbar click away" view of long-term memory,
 * as opposed to MemoryPanel's flat list. Uses the vanilla `3d-force-graph`
 * package (+ `three` for custom node meshes) directly against a plain
 * DOM container, the same dependency-free-where-possible style as
 * Orb.jsx's canvas — no react-three-fiber wrapper.
 *
 * Heavy (3d-force-graph + three) on purpose kept out of the main bundle —
 * see the lazy() import in App.jsx. This panel is opened on demand, never
 * auto-opened (see useWindowManager's autoOpen list in App.jsx).
 */

import React, { useEffect, useRef, useState, useCallback } from "react";
import ForceGraph3D from "3d-force-graph";
import * as THREE from "three";
import MarkdownMessage from "./MarkdownMessage";

// Same category accents as MemoryPanel.jsx's CATEGORY_ACCENT — keep these
// two maps in sync so a color means the same thing in both panels.
// NOTE: these feed 3d-force-graph's Three.js node/link materials directly
// (see linkColor()/nodeColor() below) — Three.js parses real color values,
// not CSS var() syntax, so these stay real hex/rgba unlike the DOM-side
// styling elsewhere in this theming pass.
const GROUP_COLOR = {
  identity: "#e8935f",
  preferences: "#06b6d4",
  relationships: "#a855f7",
  wishes: "#ec4899",
  notes: "#737373",
};
const DEFAULT_COLOR = "#e8935f";
const BACKGROUND = "#05070a";
const DIM_LINK_COLOR = "rgba(232,147,95,0.22)";
const HIGHLIGHT_LINK_COLOR = "#f5f5f5";
const FOCUS_DISTANCE = 90;

function colorFor(group) {
  return GROUP_COLOR[group] || DEFAULT_COLOR;
}

// Small glowing orb per node — a core sphere plus a translucent halo,
// colored by category. Rebuilt (not just recolored) when a node's
// highlight state changes, since a custom nodeThreeObject replaces the
// library's own color/size handling entirely.
function buildNodeObject(node, highlightedIds) {
  const isHi = highlightedIds.has(node.id);
  const color = new THREE.Color(colorFor(node.group));

  const group = new THREE.Group();
  group.add(new THREE.Mesh(
    new THREE.SphereGeometry(isHi ? 5.5 : 4, 16, 16),
    new THREE.MeshLambertMaterial({
      color, emissive: color, emissiveIntensity: isHi ? 0.9 : 0.35,
    }),
  ));
  group.add(new THREE.Mesh(
    new THREE.SphereGeometry(isHi ? 9 : 6, 12, 12),
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity: isHi ? 0.22 : 0.08 }),
  ));
  return group;
}

function linkEndpointId(end) {
  return typeof end === "object" && end !== null ? end.id : end;
}

// Above this many matched notes, don't fly the camera to any single
// point at all -- just light the whole cluster. Flying to one spot out
// of, say, six matched notes would misrepresent where the answer
// actually came from just as much as flying to one node arbitrarily.
const MAX_FLY_TO_NODES = 4;

export default function Galaxy({ graph, requestGraph, memoryMatch }) {
  const containerRef = useRef(null);
  const graphInstance = useRef(null);
  const highlightNodes = useRef(new Set());
  const highlightLinks = useRef(new Set());
  // Captures whatever memoryMatch already existed *before* this component
  // mounted, so a match that arrived while the panel was closed doesn't
  // get replayed the instant the user opens it -- opening the panel is
  // not the same as the match happening now.
  const appliedMatchTs = useRef(memoryMatch ? memoryMatch.ts : null);
  const [selected, setSelected] = useState(null); // the clicked node, or null

  useEffect(() => {
    requestGraph();
  }, [requestGraph]);

  // Re-invoking each accessor with its own current value forces
  // 3d-force-graph to re-evaluate it for every node/link right now --
  // the documented way to repaint after mutating the highlight sets
  // without touching graphData().
  const repaintHighlight = useCallback(() => {
    const Graph = graphInstance.current;
    if (!Graph) return;
    Graph.nodeThreeObject(Graph.nodeThreeObject());
    Graph.linkColor(Graph.linkColor());
    Graph.linkWidth(Graph.linkWidth());
  }, []);

  const clearSelection = useCallback(() => {
    highlightNodes.current = new Set();
    highlightLinks.current = new Set();
    repaintHighlight();
    setSelected(null);
  }, [repaintHighlight]);

  // Click-to-focus (and the single-match path for an automatic
  // memory_match event): highlight the node + its direct links, fly the
  // camera to it.
  const focusNode = useCallback((node) => {
    const Graph = graphInstance.current;
    if (!Graph) return;
    const links = new Set();
    const nodeIds = new Set([node.id]);
    (Graph.graphData().links || []).forEach((l) => {
      const sId = linkEndpointId(l.source);
      const tId = linkEndpointId(l.target);
      if (sId === node.id || tId === node.id) {
        links.add(l);
        nodeIds.add(sId);
        nodeIds.add(tId);
      }
    });
    highlightNodes.current = nodeIds;
    highlightLinks.current = links;
    repaintHighlight();
    setSelected(node);

    const dist = Math.hypot(node.x || 0, node.y || 0, node.z || 0) || 1;
    const distRatio = 1 + FOCUS_DISTANCE / dist;
    Graph.cameraPosition(
      { x: (node.x || 0) * distRatio, y: (node.y || 0) * distRatio, z: (node.z || 0) * distRatio },
      node,
      1000,
    );
  }, [repaintHighlight]);

  // Highlights a group of nodes (plus any links directly between them)
  // without touching the camera or opening the side panel -- used for
  // the >MAX_FLY_TO_NODES case, and as the highlight step before flying
  // to a small group's centroid.
  const highlightCluster = useCallback((nodes) => {
    const Graph = graphInstance.current;
    if (!Graph) return;
    const ids = new Set(nodes.map((n) => n.id));
    const links = new Set((Graph.graphData().links || []).filter((l) =>
      ids.has(linkEndpointId(l.source)) && ids.has(linkEndpointId(l.target))
    ));
    highlightNodes.current = ids;
    highlightLinks.current = links;
    repaintHighlight();
  }, [repaintHighlight]);

  // Flies the camera to frame a small group's centroid -- the multi-node
  // equivalent of focusNode's single-node fly-to.
  const flyToGroup = useCallback((nodes) => {
    const Graph = graphInstance.current;
    if (!Graph || nodes.length === 0) return;
    const cx = nodes.reduce((s, n) => s + (n.x || 0), 0) / nodes.length;
    const cy = nodes.reduce((s, n) => s + (n.y || 0), 0) / nodes.length;
    const cz = nodes.reduce((s, n) => s + (n.z || 0), 0) / nodes.length;
    const dist = Math.hypot(cx, cy, cz) || 1;
    const distRatio = 1 + (FOCUS_DISTANCE * 1.6) / dist; // pull back further to fit the group
    Graph.cameraPosition(
      { x: cx * distRatio, y: cy * distRatio, z: cz * distRatio },
      { x: cx, y: cy, z: cz },
      1000,
    );
  }, []);

  // Applies an incoming memory_match event: resolves the ids to real
  // graph nodes, then highlights (and, unless there are too many to
  // single out meaningfully, flies to) them -- the automatic version of
  // clicking them by hand.
  const applyMemoryMatch = useCallback((nodeIds) => {
    const Graph = graphInstance.current;
    if (!Graph || !nodeIds || nodeIds.length === 0) return;
    const idSet = new Set(nodeIds);
    const matched = (Graph.graphData().nodes || []).filter((n) => idSet.has(n.id));
    if (matched.length === 0) return;

    if (matched.length === 1) {
      focusNode(matched[0]);
      return;
    }
    setSelected(null);
    highlightCluster(matched);
    if (matched.length <= MAX_FLY_TO_NODES) {
      flyToGroup(matched);
    }
    // else: highlight only -- no camera move (see MAX_FLY_TO_NODES above).
  }, [focusNode, highlightCluster, flyToGroup]);

  // Instantiate the 3D graph once against the container div. All later
  // data/state changes go through graphInstance.current, not re-creation.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const Graph = ForceGraph3D()(el)
      .backgroundColor(BACKGROUND)
      .showNavInfo(false)
      .nodeLabel((n) => n.label)
      .nodeThreeObject((node) => buildNodeObject(node, highlightNodes.current))
      .nodeThreeObjectExtend(false)
      .linkColor((link) => (highlightLinks.current.has(link) ? HIGHLIGHT_LINK_COLOR : DIM_LINK_COLOR))
      .linkWidth((link) => (highlightLinks.current.has(link) ? 2 : 0.6))
      .linkOpacity(0.4)
      .onNodeClick(focusNode)
      .onBackgroundClick(clearSelection);

    const rect = el.getBoundingClientRect();
    Graph.width(rect.width).height(rect.height);
    graphInstance.current = Graph;

    const ro = new ResizeObserver(() => {
      const r = el.getBoundingClientRect();
      Graph.width(r.width).height(r.height);
    });
    ro.observe(el);

    return () => {
      ro.disconnect();
      if (typeof Graph._destructor === "function") Graph._destructor();
      el.innerHTML = "";
      graphInstance.current = null;
    };
  }, [focusNode, clearSelection]);

  // Push fresh vault data into the already-instantiated graph whenever
  // it arrives (initial load, or a reopen re-fetching via requestGraph).
  useEffect(() => {
    const Graph = graphInstance.current;
    if (!Graph || !graph) return;
    highlightNodes.current = new Set();
    highlightLinks.current = new Set();
    setSelected(null);
    Graph.graphData({
      nodes: (graph.nodes || []).map((n) => ({ ...n })),
      links: (graph.links || []).map((l) => ({ ...l })),
    });
  }, [graph]);

  // Reacts to a fresh memory_match push (see useFriday.js). Guarded by
  // appliedMatchTs so a match that already existed when this component
  // mounted -- i.e. happened while the panel was closed -- is treated as
  // already seen rather than replayed on open.
  useEffect(() => {
    if (!memoryMatch || appliedMatchTs.current === memoryMatch.ts) return;
    appliedMatchTs.current = memoryMatch.ts;
    applyMemoryMatch(memoryMatch.nodeIds);
  }, [memoryMatch, applyMemoryMatch]);

  const closePanel = clearSelection;

  const loaded = !!graph;
  const isEmpty = loaded && (graph.nodes || []).length === 0;

  return (
    <div style={styles.wrap}>
      <div ref={containerRef} style={styles.canvasHost} />

      {!loaded && <div style={styles.overlayText}>Loading…</div>}
      {isEmpty && (
        <div style={styles.overlayText}>
          Nothing in the vault to graph yet — talk to FRIDAY and it'll fill in.
        </div>
      )}

      {selected && (
        <div style={styles.sidePanel}>
          <div style={styles.sidePanelHeader}>
            <div style={{ ...styles.dot, background: colorFor(selected.group) }} />
            <span style={styles.sidePanelTitle}>{selected.label}</span>
            <button style={styles.closeBtn} onClick={closePanel} title="Close">✕</button>
          </div>
          <div style={styles.sidePanelBody}>
            {selected.excerpt
              ? <MarkdownMessage text={selected.excerpt} role="friday" />
              : <div style={styles.emptyExcerpt}>No content yet.</div>}
          </div>
        </div>
      )}
    </div>
  );
}

const styles = {
  wrap: {
    position: "relative",
    width: "100%", height: "100%",
    overflow: "hidden",
    background: BACKGROUND,
  },
  canvasHost: {
    width: "100%", height: "100%",
  },
  overlayText: {
    position: "absolute", top: "50%", left: "50%",
    transform: "translate(-50%, -50%)",
    color: "#8a8a8a", fontSize: 11, maxWidth: 220, textAlign: "center",
    fontFamily: "'JetBrains Mono', monospace", letterSpacing: 1,
    pointerEvents: "none",
  },
  sidePanel: {
    position: "absolute", top: 10, right: 10, bottom: 10,
    width: 220, maxWidth: "42%",
    display: "flex", flexDirection: "column",
    background: "var(--panel-bg-solid)",
    backdropFilter: "blur(var(--panel-blur)) saturate(var(--panel-saturate))",
    border: "1px solid var(--panel-border)",
    borderRadius: 8,
    overflow: "hidden",
    boxShadow: "0 8px 24px rgba(0,0,0,0.5)",
  },
  sidePanelHeader: {
    display: "flex", alignItems: "center", gap: 8,
    padding: "8px 6px 8px 10px",
    borderBottom: "1px solid rgba(255,255,255,0.08)",
    flexShrink: 0,
  },
  dot: {
    width: 8, height: 8, borderRadius: "50%", flexShrink: 0,
  },
  sidePanelTitle: {
    flex: 1, minWidth: 0,
    fontSize: 11, fontWeight: 700, letterSpacing: "0.04em",
    color: "#e5e5e5",
    fontFamily: "'JetBrains Mono', monospace",
    whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
  },
  closeBtn: {
    background: "transparent", border: "none", color: "#666",
    cursor: "pointer", fontSize: 11, padding: "4px 6px",
    borderRadius: 4, lineHeight: 1, flexShrink: 0,
  },
  sidePanelBody: {
    padding: "8px 10px",
    overflowY: "auto",
    flex: 1,
    minHeight: 0,
  },
  emptyExcerpt: {
    fontSize: 11, color: "#737373",
    fontFamily: "'JetBrains Mono', monospace",
  },
};
