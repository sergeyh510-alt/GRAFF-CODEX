/* GRAFF frontend - live Codex reasoning graph.
 *
 * One data model (rawNodes / rawOrder / edgeList), two renderers:
 *   2D vis-network  -> "lanes" (time x type), hierarchy, physics
 *   3D 3d-force-graph -> "neural brain" (glowing spheres, time gradient)
 *
 * Derived view: type/actor/time filters, search highlighting,
 * ancestor/descendant path highlighting, optional tool_call -> tool_result
 * collapsing. The active mode and layout persist in localStorage. */
(function () {
  "use strict";

  // ------------------------------------------------------------------ //
  // configuration
  // ------------------------------------------------------------------ //
  var TYPE_STYLE = {
    reason:      { color: "#94a3b8", shape: "dot",      label: "рассуждение", legend: "" },
    message:     { color: "#cbd5e1", shape: "ellipse",  label: "сообщение",   legend: "" },
    tool_call:   { color: "#3b82f6", shape: "box",      label: "вызов инструмента", legend: "box" },
    tool_result: { color: "#2dd4bf", shape: "dot",      label: "результат",   legend: "" },
    file_edit:   { color: "#f59e0b", shape: "diamond",  label: "правка файла", legend: "diamond" },
    error:       { color: "#ef4444", shape: "triangle", label: "ошибка",      legend: "diamond" }
  };
  var USER_COLOR = "#a78bfa";
  var TYPE_ORDER = ["reason", "message", "tool_call", "tool_result", "file_edit", "error"];
  var TYPE_SIZE = { reason: 12, message: 15, tool_call: 19, tool_result: 13, file_edit: 17, error: 21 };
  var DATA_SIZE = 14;

  var MATCH_BORDER = "#ffd76a";
  var PATH_BORDER = "#ffffff";

  var MINUTES_WINDOW = [0, 0.5, 1, 2, 5, 15, 30, 60, 120];
  var MAX_RENDER = 4000;

  // ---- performance / quality ----
  // Quality mode re-enables the pretty-but-costly embellishments (glow
  // sprites, per-node tooltips, labels, high DPI).  Performance mode is the
  // default and keeps 400-1200 node sessions smooth.
  var GL_SPRITE_LIMIT = 300;      // glow sprites only below this node count
  var GL_BIG_GRAPH = 300;          // 'big graph' -> drop pixel ratio, skip labels
  var GL_REFRESH_MS = 1200;        // min ms between 3D data rebuilds
  var GL_SYNC_LIMIT = 1500;        // hard cap for 3D nodes
  var FPS_SAMPLE_MS = 1500;        // smoothness meter window

  // ---- 3D draw-call budget ------------------------------------------- //
  // In 3d-force-graph a link with a NON-ZERO width becomes a cylinder Mesh
  // (its own geometry per link) and a directional arrow a CONE Mesh; a
  // zero-width link is just a thin Line that reuses a cached LineBasicMaterial,
  // so it costs no per-link geometry/material.  With thousands of links that is
  // the difference between ~11k draw calls (CPU bound) and ~3.9k.  Thick/
  // curved/arrowed links are therefore opt-in and only for small graphs.
  var GL_BIG_LINK = 400;           // above this many links: lines only
  var GL_CURVE_LIMIT = 600;        // link curvature only below this many links
  var GL_ARROW_LIMIT = 600;        // arrows only below this many links
  var GL_NODE_RES = 18;            // sphere segments (small graphs)
  var GL_NODE_RES_BIG = 8;         // sphere segments (big graphs)
  var GL_BIG_GRAPH_MEM = 600;      // nodes above which we drop the pixel ratio
  var GL_AUTO_ROTATE_SPEED = 0.45; // OrbitControls autoRotateSpeed (was manual)

  // ---- lanes (2D diagram) ----
  var LANE_NAMES = TYPE_ORDER.slice();
  var LANE_HEIGHT = 110;
  var LANE_GAP = 60;
  var LANE_X0 = 60;
  var LANE_STEP_MAX = 47;
  var LANE_TOTAL_MAX = 30000;
  var LAYOUT_LIST = ["lanes", "hier-lr", "hier-ud", "physics"];
  var DEGENERATE_RATIO = 0.85;

  // ---- 3D brain ----
  var GL_BUILD_LIMIT = 1500;
  var GL_COLD = { r: 0x38, g: 0x6b, b: 0xd6 };
  var GL_WARM = { r: 0xff, g: 0x8a, b: 0x1e };
  var GL_FLY_DIST = 170;

  // ---- 3D chain start/end markers ----
  // Exactly TWO meshes live in the scene (never nodeThreeObject per node!), so
  // the whole feature costs two extra draw calls.  They track the earliest root
  // (start) and the newest event (end) and pulsate softly.
  var MARKER_GREEN = 0x3ddc84;   // chain start
  var MARKER_YELLOW = 0xffce38;  // chain end
  // Size and offset are expressed as a fraction of the node's OWN diameter
  // (2 * cbrt(val) * nodeRelSize), so the pointer looks the same on any graph
  // instead of being a fixed world size.  The visible triangle is about as big
  // as the ball (~1.0 diameter, +15% at the pulse peak).
  var MARKER_NODE_SIZE = 1.0;      // marker size in node diameters
  var MARKER_OFFSET_FACTOR = 1.0;  // sideways offset in node diameters
  var MARKER_PULSE_MS = 320;     // pulsation half-period

  var EDGE_REL_COLOR = {
    reason: "#8ba0b4", message: "#cbd5e1", tool_call: "#3b82f6",
    tool_result: "#2dd4bf", file_edit: "#f59e0b", error: "#ef4444"
  };
  var EDGE_LABELS = {
    reason: "think", message: "msg", tool_call: "call",
    tool_result: "result", file_edit: "edit", error: "error"
  };

  var STORE_KEY = "graff.ui.v2";

  // ------------------------------------------------------------------ //
  // state
  // ------------------------------------------------------------------ //
  var nodes = new vis.DataSet();
  var edges = new vis.DataSet();
  var network = null;
  var gl = null;                 // 3d-force-graph instance
  var ws = null;
  var reconnectTimer = null;

  var rawNodes = {};             // id -> server node object
  var rawOrder = [];             // insertion order of ids
  var childrenOf = {};           // id -> [child ids]
  var edgeList = [];             // server edge objects
  var edgeIds = {};              // "from>to" -> true
  var positions = {};            // id -> {x,y} for minimap
  var viewMap = {};              // rendered id -> raw id
  var viewMapReverse = {};       // raw id -> rendered id

  var autoMode = null;           // "lanes" when auto-selected for degenerate graphs

  var enabledTypes = {};
  TYPE_ORDER.forEach(function (t) { enabledTypes[t] = true; });

  var state = {
    mode: "2d",
    layout: "lanes",
    autoLanes: true,
    sizeByType: true,
    edgeColor: true,
    collapse: false,
    follow: true,
    showLabels: true,
    hideUser: false,
    windowSeconds: 0,
    cutoffTs: 0,
    search: "",
    matches: null,
    pathNodes: null,
    pathEdges: null,
    selected: null,
    lastNodeId: null,
    perf: true,          // performance mode (on by default)
    followSessions: true, // server-side 'follow new sessions' toggle
    linkWidth: false,     // 3D: thick links (per-link meshes -> costly)
    linkCurve: false,     // 3D: curved links (more geometry per link)
    markers: true,        // 3D: chain start/end pointer markers (on by default)
    laneShifts: {}        // 2D lanes: manual per-node horizontal offsets (id -> dx)
  };

  var status = { startedWall: Date.now(), counts: {} };
  var sessions = [];
  var currentSession = null;

  var rotateTimer = null;
  var manualRotateTimer = null;
  var laneBaseX = {};          // view id -> auto lane-slot x (before a user shift)
  var laneDragId = null;       // node currently dragged in lanes mode
  var SHIFT_BORDER = "#7dd3fc"; // outline of a manually shifted lane node
  var lastFitAt = 0;
  var settled3d = false;   // layout cooled down -> loop may pause
  var userInteracting = false; // pointer is dragging/zooming the 3D camera

  // ------------------------------------------------------------------ //
  // dom helpers
  // ------------------------------------------------------------------ //
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function esc(text) {
    return String(text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtClock(ts) {
    if (!ts) return "—";
    try { return new Date(ts * 1000).toLocaleTimeString(); } catch (e) { return "—"; }
  }
  function fmtBytes(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(1) + " MB";
  }
  function hexA(hex, alpha) {
    var v = String(hex).replace("#", "");
    if (v.length !== 6) return hex;
    var r = parseInt(v.substring(0, 2), 16);
    var g = parseInt(v.substring(2, 4), 16);
    var b = parseInt(v.substring(4, 6), 16);
    return "rgba(" + r + "," + g + "," + b + "," + alpha + ")";
  }
  function mixColor(a, b, t) {
    t = Math.max(0, Math.min(1, t));
    return {
      r: Math.round(a.r + (b.r - a.r) * t),
      g: Math.round(a.g + (b.g - a.g) * t),
      b: Math.round(a.b + (b.b - a.b) * t)
    };
  }
  function toast(message) {
    var node = el("div", "toast", message);
    document.body.appendChild(node);
    requestAnimationFrame(function () { node.setAttribute("data-show", "true"); });
    setTimeout(function () {
      node.setAttribute("data-show", "false");
      setTimeout(function () { node.remove(); }, 260);
    }, 2800);
  }

  function baseColor(raw) {
    if (!raw) return "#888";
    if (raw.actor === "user") return USER_COLOR;
    var style = TYPE_STYLE[raw.type];
    return style ? style.color : "#888";
  }
  function nodeSize(raw) {
    if (!state.sizeByType) return DATA_SIZE;
    return TYPE_SIZE[raw.type] || DATA_SIZE;
  }
  function edgeColorFor(type) { return EDGE_REL_COLOR[type] || "#4b5a6e"; }
  function collapseKey(raw) {
    if (!raw || !raw.meta) return null;
    return raw.meta.call_id ? "c:" + raw.meta.call_id : null;
  }

  // ------------------------------------------------------------------ //
  // derived view model
  // ------------------------------------------------------------------ //
  function buildView() {
    var items = [];
    var groups = {};
    var dropped = {};

    if (state.collapse) {
      for (var i = 0; i < rawOrder.length; i++) {
        var raw = rawNodes[rawOrder[i]];
        if (!raw || raw.type !== "tool_call") continue;
        var callId = raw.meta && raw.meta.call_id;
        if (!callId || groups[callId]) continue;
        var kids = childrenOf[raw.id] || [];
        var result = null;
        for (var j = 0; j < kids.length; j++) {
          var child = rawNodes[kids[j]];
          if (child && child.type === "tool_result" && child.meta && child.meta.call_id === callId) {
            result = child; break;
          }
        }
        if (result) { groups[callId] = { call: raw, result: result }; dropped[result.id] = callId; }
      }
    }

    for (var k = 0; k < rawOrder.length; k++) {
      var node = rawNodes[rawOrder[k]];
      if (!node || dropped[node.id]) continue;
      var key = state.collapse ? (node.meta && node.meta.call_id) : null;
      if (node.type === "tool_call" && key && groups[key]) {
        items.push({ id: key, group: groups[key], source: node });
      } else {
        items.push({ id: node.id, source: node });
      }
    }

    viewMap = {};
    viewMapReverse = {};
    for (var m = 0; m < items.length; m++) {
      viewMap[items[m].id] = items[m].source.id;
      viewMapReverse[items[m].source.id] = items[m].id;
    }
    if (state.collapse) {
      for (var ck in groups) viewMapReverse[groups[ck].result.id] = ck;
    }
    return items;
  }

  function makeVisNode(item) {
    var raw = item.source;
    var type = raw.type;
    var actor = raw.actor;
    var label = raw.label || "";

    if (item.group) {
      type = "tool_call";
      actor = item.group.call.actor;
      label = (item.group.call.label || "tool_call") + " → " + (item.group.result.label || "result");
    }

    var color = actor === "user" ? USER_COLOR : (TYPE_STYLE[type] ? TYPE_STYLE[type].color : "#888");
    var shape = actor === "user" ? "ellipse" : (TYPE_STYLE[type] ? TYPE_STYLE[type].shape : "dot");
    var size = state.sizeByType ? (TYPE_SIZE[type] || DATA_SIZE) : DATA_SIZE;

    return {
      id: item.id,
      label: state.showLabels ? label : "",
      shape: shape,
      size: size,
      color: {
        background: color, border: color,
        highlight: { background: color, border: "#ffffff" },
        hover: { background: color, border: "#ffffff" }
      },
      font: { color: "#e6edf3", size: 12, face: "Segoe UI", strokeWidth: 3, strokeColor: "#0a0e14" },
      borderWidth: 1.5,
      borderWidthSelected: 3,
      shadow: { enabled: true, color: "rgba(0,0,0,0.5)", size: 8, x: 0, y: 2 },
      _id: item.id,
      _type: type,
      _actor: actor,
      _ts: raw.ts,
      _label: label,
      _raw: raw.id,
      _isGroup: !!item.group,
      _resultId: item.group ? item.group.result.id : null
    };
  }

  function makeVisEdge(key, from, to, sourceRaw) {
    var color = state.edgeColor ? baseColor(sourceRaw) : "#4b5a6e";
    return {
      id: key,
      from: from,
      to: to,
      arrows: { to: { enabled: true, scaleFactor: 0.6 } },
      color: { color: hexA(color, 0.55), highlight: "#ffffff", hover: color },
      width: 1.35,
      selectionWidth: 2.6,
      smooth: { enabled: true, type: "cubicBezier", roundness: 0.45 },
      _raw: sourceRaw ? sourceRaw.id : null
    };
  }

  function render() {
    var items = buildView();
    var total = items.length;
    var start = Math.max(0, total - MAX_RENDER);
    var keepIds = {};
    var nodeBatch = [];

    for (var i = start; i < total; i++) {
      var item = items[i];
      keepIds[item.id] = true;
      nodeBatch.push(makeVisNode(item));
    }

    var existing = nodes.getIds();
    var remove = [];
    for (var e = 0; e < existing.length; e++) {
      if (!keepIds[existing[e]]) remove.push(existing[e]);
    }
    if (remove.length) nodes.remove(remove);
    if (nodeBatch.length) nodes.update(nodeBatch);

    var keepEdges = {};
    var edgeBatch = [];
    for (var j = 0; j < edgeList.length; j++) {
      var edge = edgeList[j];
      var from = viewMapReverse[edge.from];
      var to = viewMapReverse[edge.to];
      if (from === undefined || to === undefined || from === to) continue;
      var key = from + ">" + to;
      if (keepEdges[key]) continue;
      keepEdges[key] = true;
      var sourceRaw = rawNodes[edge.to] || rawNodes[edge.from];
      edgeBatch.push(makeVisEdge(key, from, to, sourceRaw));
    }
    var existingEdges = edges.getIds();
    var removeEdges = [];
    for (var z = 0; z < existingEdges.length; z++) {
      if (!keepEdges[existingEdges[z]]) removeEdges.push(existingEdges[z]);
    }
    if (removeEdges.length) edges.remove(removeEdges);
    if (edgeBatch.length) edges.update(edgeBatch);

    applyViewState();
    updateEmptyState(total);
    drawMinimap();
    if (state.selected && !nodes.get(state.selected)) state.selected = null;

    maybeAutoLanes();

    if (state.mode === "3d") {
      sync3d();
    } else {
      if (state.layout === "lanes") { positionLanes(); appendEdgeLabels(); }
      if (state.follow) scheduleFit();
    }
  }

  function scheduleFit() {
    var now = Date.now();
    if (now - lastFitAt > 2000) { lastFitAt = now; doFit(); }
  }

  // ------------------------------------------------------------------ //
  // lanes layout (2D diagram: x = time, y = event type)
  // ------------------------------------------------------------------ //
  function laneGeometry() {
    var geoms = { index: {}, center: [] };
    for (var i = 0; i < LANE_NAMES.length; i++) {
      geoms.index[LANE_NAMES[i]] = i;
      geoms.center[i] = i * (LANE_HEIGHT + LANE_GAP);
    }
    return geoms;
  }

  function positionLanes() {
    if (!network || state.layout !== "lanes") return;
    var ids = nodes.getIds();
    var visible = [];
    for (var i = 0; i < ids.length; i++) {
      var node = nodes.get(ids[i]);
      if (!node || node.hidden) continue;
      visible.push({ id: node.id, ts: (node._ts || 0), type: node._type });
    }
    if (!visible.length) return;
    // x follows the event order (time); ranking guarantees a minimum step even
    // when many events share a timestamp.
    visible.sort(function (a, b) { return (a.ts || 0) - (b.ts || 0); });
    var geoms = laneGeometry();
    var step = Math.max(14, Math.min(LANE_STEP_MAX, LANE_TOTAL_MAX / Math.max(1, visible.length)));
    var updates = [];
    laneBaseX = {};
    for (var m = 0; m < visible.length; m++) {
      var v = visible[m];
      var lane = geoms.index[v.type];
      if (lane === undefined) lane = geoms.index.message;
      var baseX = LANE_X0 + m * step;
      laneBaseX[v.id] = baseX;
      // A manual horizontal shift (drag) is preserved across re-layouts; only
      // the vertical position is forced back into the lane.
      var shift = laneShiftOf(v.id);
      updates.push({ id: v.id, x: baseX + shift, y: geoms.center[lane], fixed: { x: false, y: true } });
    }
    if (updates.length) nodes.update(updates);
    updateLayoutNote();
  }

  // ---- lane drag shifts (2D) ----------------------------------------- //
  function laneShiftOf(viewId) {
    var v = state.laneShifts[viewId];
    return (typeof v === "number" && isFinite(v)) ? v : 0;
  }

  function setLaneShift(viewId, dx, persist) {
    if (!viewId) return;
    if (!dx || Math.abs(dx) < 1) delete state.laneShifts[viewId];
    else state.laneShifts[viewId] = Math.round(dx * 10) / 10;
    // The drag stream is hot, so only the caller that ends it writes storage.
    if (persist !== false) savePrefs();
  }

  // Snap the whole visible graph onto its lanes again (base x + saved shift).
  function reflowLanes() {
    if (!network || state.layout !== "lanes") return;
    var ids = nodes.getIds();
    var updates = [];
    for (var i = 0; i < ids.length; i++) {
      var node = nodes.get(ids[i]);
      if (!node || node.hidden) continue;
      var shift = laneShiftOf(node.id);
      var base = laneBaseX[node.id];
      var x = (typeof base === "number") ? base : node.x;
      updates.push({ id: node.id, x: x + shift, fixed: { x: false, y: true } });
    }
    if (updates.length) nodes.update(updates);
  }

  function resetLaneShifts() {
    var had = false;
    for (var k in state.laneShifts) { if (state.laneShifts.hasOwnProperty(k)) { had = true; break; } }
    state.laneShifts = {};
    savePrefs();
    if (state.layout === "lanes") positionLanes();
    if (network) { applyEdgeSmoothness(); network.redraw(); if (had) network.fit({ animation: true }); }
    toast(had ? "Сдвиги сброшены" : "Сдвигов не было");
  }

  function appendEdgeLabels() {
    if (!edges.length || edges.length > 700) return;
    var updates = [];
    edges.forEach(function (edge) {
      var label = "";
      if (edge._raw && rawNodes[edge._raw]) label = EDGE_LABELS[rawNodes[edge._raw].type] || "";
      updates.push({
        id: edge.id,
        label: label,
        font: { color: "#8296aa", size: 10, strokeWidth: 0, background: "rgba(10,14,20,0.6)" }
      });
    });
    if (updates.length) edges.update(updates);
  }

  function clearEdgeLabels() {
    var updates = [];
    edges.forEach(function (edge) { updates.push({ id: edge.id, label: "" }); });
    if (updates.length) edges.update(updates);
  }

  function isDegenerate() {
    var deg = 0, total = 0;
    for (var i = 0; i < rawOrder.length; i++) {
      var raw = rawNodes[rawOrder[i]];
      if (!raw) continue;
      total++;
      var parents = raw.parent ? 1 : 0;
      var kids = (childrenOf[raw.id] || []).length;
      if (parents <= 1 && kids <= 1) deg++;
    }
    return total >= 4 && (deg / total) >= DEGENERATE_RATIO;
  }

  function maybeAutoLanes() {
    if (!state.autoLanes) return;
    if (state.mode !== "2d") return;   // 3D manages its own layout
    if (state.layout === "lanes") return;
    if (isDegenerate()) {
      state.layout = "lanes";
      var sel = $("layout-mode");
      if (sel) sel.value = "lanes";
      autoMode = "lanes";
      applyLayout();
      updateLayoutNote();
      toast("Сессия вырожденная (одна цепочка) — включены «дорожки»");
    }
  }

  function updateLayoutNote() {
    var note = $("layout-note");
    if (!note) return;
    if (autoMode === "lanes") {
      note.textContent = "Авто: у большинства узлов один родитель и один ребёнок — включены дорожки.";
    } else if (state.layout === "lanes") {
      note.textContent = "Дорожки: по горизонтали время, по вертикали тип события.";
    } else {
      note.textContent = "";
    }
  }

  // ------------------------------------------------------------------ //
  // visibility + highlighting (single pass over the rendered view)
  // ------------------------------------------------------------------ //
  function hiddenByWindow(ts) {
    return state.windowSeconds > 0 && state.cutoffTs > 0 && ts > 0 && ts < state.cutoffTs;
  }
  function itemRaw(node) {
    if (node._isGroup && node._resultId) {
      return rawNodes[node._resultId] || rawNodes[node._raw];
    }
    return rawNodes[node._raw] || rawNodes[node._id];
  }

  function rebuildIndexes() {
    childrenOf = {};
    for (var i = 0; i < rawOrder.length; i++) {
      var raw = rawNodes[rawOrder[i]];
      if (!raw || !raw.parent) continue;
      (childrenOf[raw.parent] = childrenOf[raw.parent] || []).push(raw.id);
    }
  }

  function computePath(rawId) {
    var raw = rawNodes[rawId];
    if (!raw) { state.pathNodes = null; state.pathEdges = null; return; }
    var pathNodes = {};
    var pathEdges = {};
    pathNodes[rawId] = true;
    var cursor = raw;
    var guard = 0;
    while (cursor && cursor.parent && guard++ < 10000) {
      pathNodes[cursor.parent] = true;
      pathEdges[cursor.parent + ">" + cursor.id] = true;
      cursor = rawNodes[cursor.parent];
    }
    var queue = [rawId];
    var seen = {};
    seen[rawId] = true;
    while (queue.length) {
      var current = queue.shift();
      var kids = childrenOf[current] || [];
      for (var k = 0; k < kids.length; k++) {
        var kid = kids[k];
        if (seen[kid]) continue;
        seen[kid] = true;
        pathNodes[kid] = true;
        pathEdges[current + ">" + kid] = true;
        queue.push(kid);
      }
    }
    state.pathNodes = pathNodes;
    state.pathEdges = pathEdges;
  }

  function applyViewState() {
    var visibleIds = {};
    var nodeUpdates = [];

    nodes.forEach(function (node) {
      var raw = itemRaw(node);
      var hidden = false;
      if (raw) {
        if (!enabledTypes[raw.type] && !node._isGroup) hidden = true;
        else if (state.hideUser && raw.actor === "user") hidden = true;
        else if (hiddenByWindow(raw.ts)) hidden = true;
      }
      if (node._isGroup && !enabledTypes.tool_call && !enabledTypes.tool_result) hidden = true;
      visibleIds[node.id] = !hidden;

      var color = raw ? baseColor(raw) : "#888";
      var border = color;
      var bw = 1.5;
      if (state.matches && state.matches.has(node.id)) { border = MATCH_BORDER; bw = 4; }
      if (state.pathNodes && isPathNode(node.id)) { border = PATH_BORDER; bw = 4.5; }
      // A lane node the user dragged sideways gets a light outline so the
      // manual shift is visible at a glance.
      if (state.layout === "lanes" && laneShiftOf(node.id) !== 0) { border = SHIFT_BORDER; if (bw < 2.5) bw = 2.5; }

      nodeUpdates.push({
        id: node.id,
        hidden: hidden,
        borderWidth: bw,
        borderWidthSelected: bw + 1,
        color: {
          background: color,
          border: border,
          highlight: { background: color, border: "#ffffff" },
          hover: { background: color, border: "#ffffff" }
        }
      });
    });
    if (nodeUpdates.length) nodes.update(nodeUpdates);

    var edgeUpdates = [];
    edges.forEach(function (edge) {
      var hidden = visibleIds[edge.from] === false || visibleIds[edge.to] === false;
      var isPathEdge = state.pathEdges && isPathEdgeVisible(edge);
      var color = state.edgeColor && edge._raw ? baseColor(rawNodes[edge._raw]) : "#4b5a6e";
      edgeUpdates.push({
        id: edge.id,
        hidden: hidden,
        width: isPathEdge ? 3.4 : 1.35,
        color: {
          color: isPathEdge ? "#ffffff" : hexA(color, 0.55),
          highlight: "#ffffff",
          hover: color
        }
      });
    });
    if (edgeUpdates.length) edges.update(edgeUpdates);

    if (state.mode === "3d") glHighlight3d();
  }

  function isPathNode(viewId) {
    if (!state.pathNodes) return false;
    var rawId = viewMap[viewId];
    return rawId !== undefined && state.pathNodes[rawId] === true;
  }
  function isPathEdgeVisible(edge) {
    if (!state.pathEdges) return false;
    var fromRaw = viewMap[edge.from];
    var toRaw = viewMap[edge.to];
    if (fromRaw === undefined || toRaw === undefined) return false;
    return state.pathEdges[fromRaw + ">" + toRaw] === true;
  }

  function updateEmptyState(total) {
    if (total === 0) {
      $("graph-empty").hidden = true;
      $("graph-waiting").hidden = false;
      return;
    }
    var few = total < 2;
    $("graph-empty").hidden = !few;
    $("graph-waiting").hidden = true;
    if (few) renderEmptySessions();
  }
  // ------------------------------------------------------------------ //
  // vis-network
  // ------------------------------------------------------------------ //
  function layoutOptions() {
    var hierarchical = (state.layout === "hier-lr" || state.layout === "hier-ud");
    var direction = state.layout === "hier-lr" ? "LR" : "UD";
    var physics = {
      enabled: false,
      stabilization: false,
      barnesHut: { gravitationalConstant: -9000, springLength: 150, springConstant: 0.045, damping: 0.62 }
    };
    return {
      layout: {
        hierarchical: {
          enabled: hierarchical,
          direction: direction,
          sortMethod: "directed",
          levelSeparation: 260,
          nodeSpacing: 200,
          treeSpacing: 200,
          blockShifting: true,
          edgeMinimization: true,
          parentCentralization: true
        }
      },
      physics: physics
    };
  }

  function buildNetwork() {
    if (!(window.vis && window.vis.Network)) {
      $("missing-vis").hidden = false;
      return false;
    }
    // Tear down a previous instance so its physics loop / canvases do not linger.
    if (network) {
      try { if (network.destroy) network.destroy(); } catch (e) {}
      network = null;
    }
    var options = {
      autoResize: true,
      interaction: { hover: true, tooltipDelay: 250, keyboard: { enabled: true }, multiselect: false, hideEdgesOnDrag: true },
      nodes: { borderWidth: 1.5, font: { color: "#e6edf3" } },
      edges: { selectionWidth: 2.6, arrowStrikethrough: false }
    };
    var merged = Object.assign(options, layoutOptions());
    network = new vis.Network($("stage-canvas"), { nodes: nodes, edges: edges }, merged);
    network.on("click", function (params) {
      if (params.nodes && params.nodes.length) selectNode(params.nodes[0]);
      else { state.selected = null; clearSelection(); }
    });
    network.on("dragStart", function (params) {
      if (state.layout !== "lanes" || !params.nodes || params.nodes.length !== 1) return;
      laneDragId = params.nodes[0];
      toast("Тяните влево/вправо: сдвиг вдоль дорожки. Shift — сброс.");
    });
    // In lanes mode the node is pinned vertically, so vis itself takes care of
    // the horizontal motion; here we only record how far it was shifted.
    network.on("dragging", function (params) {
      if (state.layout !== "lanes" || !laneDragId || !params.nodes) return;
      var id = laneDragId;
      if (params.nodes.length !== 1 || params.nodes[0] !== id) return;
      var base = laneBaseX[id];
      if (typeof base !== "number") return;
      var dx = 0;
      try { var pos = network.getPositions([id])[id]; if (pos) dx = pos.x - base; } catch (e) {}
      if (params.event && params.event.srcEvent && params.event.srcEvent.shiftKey) dx = 0;
      setLaneShift(id, dx, false);
    });
    network.on("dragEnd", function (params) {
      if (state.layout === "lanes" && params && params.nodes && params.nodes.length) {
        if (autoMode === "lanes") {
          autoMode = null;
          toast("Ручная правка — авто-дорожки выключены");
        }
        setLaneShift(laneDragId, laneShiftOf(laneDragId));  // persist once
        reflowLanes();    // snap x = base + saved shift exactly
        applyViewState(); // paint the "shifted" outline
      }
      laneDragId = null;
    });
    network.on("stabilizationIterationsDone", function () {
      network.fit({ animation: false });
    });
    applyEdgeSmoothness();
    return true;
  }

  function applyEdgeSmoothness() {
    if (!network) return;
    var lanes = state.layout === "lanes";
    var updates = [];
    edges.forEach(function (edge) {
      updates.push({
        id: edge.id,
        smooth: lanes
          ? { enabled: true, type: "cubicBezier", roundness: 0.5, forceDirection: "horizontal" }
          : { enabled: true, type: "cubicBezier", roundness: 0.45 },
        arrows: { to: { enabled: true, scaleFactor: lanes ? 0.75 : 0.6 } }
      });
    });
    if (updates.length) edges.update(updates);
  }

  function unfixAll() {
    var updates = [];
    nodes.forEach(function (node) { updates.push({ id: node.id, fixed: false }); });
    if (updates.length) nodes.update(updates);
  }

  function applyLayout() {
    if (!network) return;
    network.setOptions(layoutOptions());
    if (state.layout === "lanes") {
      applyEdgeSmoothness();
      positionLanes();
      appendEdgeLabels();
    } else {
      clearEdgeLabels();
      unfixAll();
    }
    setTimeout(function () { if (network) network.fit({ animation: true }); }, 240);
    updateLayoutNote();
  }

  function doFit() { if (network) network.fit({ animation: { duration: 500, easingFunction: "easeInOutQuad" } }); }

  // ------------------------------------------------------------------ //
  // selection, path highlighting and details
  // ------------------------------------------------------------------ //
  function selectNode(viewId, opts) {
    var node = nodes.get(viewId);
    if (!node) return;
    state.selected = viewId;
    var rawId = node._raw;
    computePath(rawId);
    if (node._isGroup && node._resultId) state.pathNodes[node._resultId] = true;
    applyViewState();
    if (state.mode === "3d") {
      glHighlight3d();
      if (opts && opts.fly) glFlyTo(viewId);
    } else if (network) {
      network.selectNodes([viewId]);
      if (!(opts && opts.noFocus)) {
        network.focus(viewId, { scale: network.getScale(), animation: { duration: 350, easingFunction: "easeInOutQuad" } });
      }
    }
    showDetails(node);
  }

  function clearSelection() {
    state.pathNodes = null;
    state.pathEdges = null;
    applyViewState();
    if (network) network.unselectAll();
    if (state.mode === "3d" && gl) {
      gl.nodeColor(function (n) { return n.color; });
      gl.nodeVal(function (n) { return n.val; });
    }
  }

  function showDetails(node) {
    var raw = itemRaw(node) || {};
    var type = node._type;
    var style = TYPE_STYLE[type] || TYPE_STYLE.message;
    var badge = $("details-type");
    badge.textContent = style.label;
    badge.style.color = node._actor === "user" ? USER_COLOR : style.color;
    $("details-actor").textContent = (node._actor || "codex") + (raw.meta && raw.meta.name ? " · " + raw.meta.name : "");
    $("details-time").textContent = fmtClock(raw.ts);
    $("details-title").textContent = node._label || style.label;

    var meta = raw.meta || {};
    var chips = [];
    if (meta.ordinal !== undefined && meta.ordinal !== null) chips.push("ordinal " + meta.ordinal);
    if (meta.call_id) chips.push("call " + String(meta.call_id).slice(-14));
    if (meta.status) chips.push("status " + meta.status);
    if (meta.name) chips.push("tool " + meta.name);
    $("details-meta").innerHTML = chips.map(function (c) { return '<span class="chip">' + esc(c) + "</span>"; }).join("");

    $("details-path").innerHTML = describePath(raw.id);
    $("details-text").textContent = raw.text || "(пустой текст события)";
    $("details").setAttribute("data-open", "true");
  }

  function describePath(rawId) {
    var ancestors = [];
    var cursor = rawNodes[rawId];
    var guard = 0;
    while (cursor && cursor.parent && guard++ < 500) {
      cursor = rawNodes[cursor.parent];
      if (cursor) ancestors.unshift(cursor);
    }
    var label = '<span class="path-label">предков:</span> ' + ancestors.length +
      ' · <span class="path-label">потомков:</span> ' + countDescendants(rawId);
    var chain = ancestors.slice(-4).map(function (a) {
      return '<span class="path-chip" title="' + esc(a.label || a.type) + '">' + esc((a.type || "") + " " + ((a.label || "").slice(0, 22))) + "</span>";
    }).join(" → ");
    return label + (chain ? '<div class="path-chain">' + chain + ' → <span class="path-chip current">этот узел</span></div>' : "");
  }

  function countDescendants(rawId) {
    var seen = {};
    var count = 0;
    var queue = [rawId];
    seen[rawId] = true;
    while (queue.length) {
      var current = queue.shift();
      var kids = childrenOf[current] || [];
      for (var i = 0; i < kids.length; i++) {
        if (seen[kids[i]]) continue;
        seen[kids[i]] = true;
        count++;
        queue.push(kids[i]);
      }
    }
    return count;
  }

  // ------------------------------------------------------------------ //
  // live ingestion
  // ------------------------------------------------------------------ //
  function ingestInit(message) {
    rawNodes = {};
    rawOrder = [];
    edgeList = [];
    edgeIds = {};
    state.selected = null;
    state.matches = null;
    state.pathNodes = null;
    state.pathEdges = null;
    if (message.status) applyStatus(message.status);
    if (message.sessions) applySessions(message.sessions);
    if (message.graph) ingestGraph(message.graph, false);
    rebuildIndexes();
    if (rawOrder.length) state.lastNodeId = rawOrder[rawOrder.length - 1];
    recomputeWindow();
    render();
    if (state.mode !== "3d" && network && rawOrder.length) { network.fit({ animation: false }); focusLatest(false); }
    $("graph-waiting").hidden = rawOrder.length > 0;
  }

  function ingestGraph(graph) {
    var added = [];
    if (graph.nodes) {
      for (var i = 0; i < graph.nodes.length; i++) {
        var raw = graph.nodes[i];
        if (!rawNodes[raw.id]) {
          rawNodes[raw.id] = raw;
          rawOrder.push(raw.id);
          added.push(raw);
        } else {
          rawNodes[raw.id].text = raw.text;
          rawNodes[raw.id].label = raw.label;
        }
      }
    }
    if (graph.edges) {
      for (var j = 0; j < graph.edges.length; j++) {
        var edge = graph.edges[j];
        var key = edge.from + ">" + edge.to;
        if (edgeIds[key]) continue;
        edgeIds[key] = true;
        edgeList.push(edge);
      }
    }
    return added;
  }

  function handleEvents(message) {
    var added = ingestGraph({ nodes: message.events || [], edges: [] });
    if (!added.length) {
      if (message.counts) applyCounts(message.counts);
      return;
    }
    for (var i = 0; i < added.length; i++) {
      var raw = added[i];
      if (raw.parent && rawNodes[raw.parent]) {
        var key = raw.parent + ">" + raw.id;
        if (!edgeIds[key]) { edgeIds[key] = true; edgeList.push({ from: raw.parent, to: raw.id, type: raw.type }); }
      }
    }
    rebuildIndexes();
    if (message.counts) applyCounts(message.counts);
    if (message.stats) applyStats(message.stats);

    state.lastNodeId = added[added.length - 1].id;
    if (state.search) recomputeMatches();

    var few = rawOrder.length < 2;
    $("graph-empty").hidden = !few;
    $("graph-waiting").hidden = true;

    recomputeWindow();
    render();
    if (state.mode !== "3d" && state.follow) focusLatest(true);
    if (state.selected && nodes.get(state.selected)) selectNode(state.selected);
  }

  function focusLatest(animate) {
    if (!network || !state.lastNodeId) return;
    var vid = viewMapReverse[state.lastNodeId] || state.lastNodeId;
    if (!nodes.get(vid)) return;
    try {
      network.focus(vid, { scale: state.layout === "physics" ? 1.0 : 1.05, animation: animate ? { duration: 480, easingFunction: "easeInOutQuad" } : false });
    } catch (e) { /* node not laid out yet */ }
  }

  // ------------------------------------------------------------------ //
  // search
  // ------------------------------------------------------------------ //
  function recomputeMatches() {
    state.matches = null;
    if (!state.search) { $("search-result").textContent = ""; return; }
    var needle = state.search.toLowerCase();
    var matches = {};
    var count = 0;
    for (var i = 0; i < rawOrder.length; i++) {
      var raw = rawNodes[rawOrder[i]];
      var hay = ((raw.text || "") + " " + (raw.label || "") + " " + ((raw.meta && raw.meta.name) || "")).toLowerCase();
      if (hay.indexOf(needle) !== -1) { matches[raw.id] = true; count++; }
    }
    state.matches = {};
    for (var id in matches) {
      var vid = viewMapReverse[id];
      if (vid !== undefined) state.matches[vid] = true;
    }
    $("search-result").textContent = count ? ("найдено: " + count) : "ничего не найдено";
  }

  function runSearch() {
    recomputeMatches();
    applyViewState();
    if (state.mode === "3d") glHighlight3d();
  }
  // ------------------------------------------------------------------ //
  // status, counters, legend, sessions
  // ------------------------------------------------------------------ //
  function applyStatus(s) {
    if (!s) return;
    if (s.started_at) status.startedWall = Date.now() - (s.uptime || 0) * 1000;
    $("session-name").textContent = s.session ? s.session.split(/[\\/]/).pop() : "сессия не найдена";
    $("session-name").title = s.session || "";
    currentSession = s.session || null;
    $("session-path").textContent = s.session || "";
    $("session-path").title = s.session || "";
    $("stat-lines").textContent = s.lines_read || 0;
    $("stat-errors").textContent = s.parse_errors || 0;
    if (s.counts) applyCounts(s.counts);
    if (s.ai) applyAiStatus(s.ai);
    if (typeof s.follow === "boolean" && !state.followDirty) {
      state.followSessions = s.follow;
      var fb = $("follow-sessions");
      if (fb) fb.checked = s.follow;
    }
    if (s.log) applyServerLog(s.log);
  }

  var serverLogLines = [];
  function pushServerLog(entry) {
    if (!entry) return;
    serverLogLines.push(entry);
    if (serverLogLines.length > 40) serverLogLines = serverLogLines.slice(-40);
    applyServerLog(serverLogLines);
    if (entry.kind === "session") toast(entry.text || "");
  }

  function applyServerLog(entries) {
    var el = $("session-log");
    if (!el) return;
    if (!entries || !entries.length) { el.textContent = ""; return; }
    var tail = entries.slice(-4);
    el.innerHTML = tail.map(function (entry) {
      var when = entry.ts ? new Date(entry.ts * 1000).toLocaleTimeString() : "";
      return '<span class="log-line">' + esc(when + " " + (entry.text || "")) + "</span>";
    }).join("<br>");
  }

  function applyStats() {
    $("stat-nodes").textContent = rawOrder.length;
  }

  function applyCounts(counts) {
    status.counts = counts || {};
    var total = 0;
    var parts = [];
    TYPE_ORDER.forEach(function (t) {
      var value = status.counts[t] || 0;
      total += value;
      parts.push('<span class="chip" data-type="' + t + '" style="border-color:' + hexA(TYPE_STYLE[t].color, 0.5) + '">' +
        TYPE_STYLE[t].label + ": " + value + "</span>");
    });
    var box = $("counters");
    if (box) box.innerHTML = parts.join("") + '<span class="chip">всего: ' + total + "</span>";
    $("stat-events").textContent = total;
    $("stat-nodes").textContent = rawOrder.length;
    updateFilterChips();
  }

  function buildLegend() {
    var list = $("legend");
    list.innerHTML = "";
    TYPE_ORDER.forEach(function (type) {
      var style = TYPE_STYLE[type];
      var item = el("li");
      item.setAttribute("data-type", type);
      item.setAttribute("data-off", "false");
      var swatch = el("span", "swatch " + (style.legend || ""));
      swatch.style.background = style.color;
      swatch.style.color = style.color;
      item.appendChild(swatch);
      item.appendChild(el("span", "lname", style.label));
      item.addEventListener("click", function () { toggleType(type); });
      list.appendChild(item);
    });
  }

  function buildTypeFilters() {
    var box = $("type-filters");
    box.innerHTML = "";
    var counters = el("div", "counters");
    counters.id = "counters";
    box.appendChild(counters);
    var grid = el("div", "filters");
    TYPE_ORDER.forEach(function (type) {
      var chip = el("span", "flt", TYPE_STYLE[type].label);
      chip.setAttribute("data-type", type);
      chip.setAttribute("data-on", "true");
      chip.style.color = TYPE_STYLE[type].color;
      chip.addEventListener("click", function () { toggleType(type); });
      grid.appendChild(chip);
    });
    box.appendChild(grid);
  }

  function updateFilterChips() {
    TYPE_ORDER.forEach(function (type) {
      var chip = document.querySelector('.filters .flt[data-type="' + type + '"]');
      if (chip) chip.textContent = TYPE_STYLE[type].label + " · " + (status.counts[type] || 0);
    });
  }

  function toggleType(type) {
    enabledTypes[type] = !enabledTypes[type];
    var legendItem = document.querySelector('.legend li[data-type="' + type + '"]');
    if (legendItem) legendItem.setAttribute("data-off", enabledTypes[type] ? "false" : "true");
    var chip = document.querySelector('.filters .flt[data-type="' + type + '"]');
    if (chip) chip.setAttribute("data-on", enabledTypes[type] ? "true" : "false");
    applyViewState();
    if (state.mode === "3d") sync3d();
    if (state.mode === "2d" && state.layout === "lanes") applyLayout();
  }

  function applySessions(list) {
    sessions = list || [];
    var select = $("session-select");
    select.innerHTML = "";
    sessions.forEach(function (s) {
      var when = s.mtime ? new Date(s.mtime * 1000).toLocaleString() : "—";
      var calls = s.tool_calls || 0;
      var nodes = s.nodes || s.events || 0;
      var info = " · " + nodes + " узлов · " + calls +
        " вызовов · " + fmtBytes(s.size || 0);
      var st = s.empty ? " · пусто" : "";
      var active = s.current ? "● " : "";
      var option = el("option", null, active + when + info + st);
      option.value = s.path;
      option.title = s.path;
      if (s.current) option.selected = true;
      select.appendChild(option);
    });
    renderEmptySessions();
  }

  function renderEmptySessions() {
    var box = $("empty-sessions");
    if (!box) return;
    box.innerHTML = "";
    var useful = sessions.filter(function (s) { return s.contentful; }).slice(0, 6);
    if (!useful.length) { box.innerHTML = '<p class="hint">Содержательных сессий не найдено.</p>'; return; }
    box.innerHTML = '<p class="hint">Последние содержательные сессии:</p>';
    useful.forEach(function (s) {
      var btn = el("button", "btn session-quick", (s.mtime ? new Date(s.mtime * 1000).toLocaleString() : s.name) + " · " + (s.nodes || s.events || 0) + " узлов · " + (s.tool_calls || 0) + " вызовов");
      btn.title = s.path;
      btn.addEventListener("click", function () { openSession(s.path); });
      box.appendChild(btn);
    });
  }

  function applyAiStatus(ai) {
    var box = $("ai-status");
    if (!box) return;
    if (!ai) { box.textContent = ""; return; }
    if (ai.running) { box.textContent = "идёт обогащение…"; box.style.color = "#ffd76a"; return; }
    if (!ai.available) {
      box.textContent = ai.error || "ключ не найден — задайте ROUTERAI_API_KEY или AITUNNEL_API_KEY";
      box.style.color = "#f59e0b";
      return;
    }
    var last = ai.last ? (" · последний прогон: +" + ai.last.enriched + " узлов, запросов " + ai.last.requests) : "";
    box.textContent = "готово: " + ai.model + " (" + ai.provider + ")" + last;
    box.style.color = "#2dd4bf";
  }

  function openSession(spec) {
    fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ spec: spec })
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (data.ok) toast("Открываю " + (data.name || data.session));
      else toast("Не удалось открыть: " + (data.error || "unknown"));
    }).catch(function () { toast("Ошибка запроса"); });
  }

  function refreshSessions() {
    fetch("/api/sessions").then(function (r) { return r.json(); }).then(function (data) {
      applySessions(data.sessions || []);
    }).catch(function () { toast("Не удалось получить список сессий"); });
  }

  function runEnrich() {
    var limit = parseInt(($("ai-limit").value || "40"), 10) || 40;
    var btn = $("btn-enrich");
    btn.disabled = true;
    $("ai-status").textContent = "запрос к провайдеру…";
    $("ai-status").style.color = "#ffd76a";
    fetch("/api/enrich", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit: limit })
    }).then(function (r) { return r.json(); }).then(function (data) {
      btn.disabled = false;
      if (data.ok) toast("ИИ добавил " + data.enriched + " узлов (запросов " + data.requests + ", из кэша " + data.cache_hits + ")");
      else toast("ИИ недоступен: " + (data.error || "unknown"));
      if (data.ai) applyAiStatus(data.ai);
    }).catch(function () {
      btn.disabled = false;
      toast("Ошибка запроса ИИ");
    });
  }

  // ------------------------------------------------------------------ //
  // minimap
  // ------------------------------------------------------------------ //
  var minimapTimer = null;
  function drawMinimap() {
    var svg = $("minimap");
    if (!svg || svg.hidden || state.mode === "3d") return;
    if (minimapTimer) return;
    minimapTimer = setTimeout(function () {
      minimapTimer = null;
      if (!network) return;
      var ids = nodes.getIds();
      if (!ids.length || ids.length > 3000) { svg.innerHTML = ""; return; }
      var pos;
      try { pos = network.getPositions(ids); } catch (e) { return; }
      var minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
      var pts = [];
      for (var i = 0; i < ids.length; i++) {
        var p = pos[ids[i]];
        if (!p) continue;
        minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x);
        minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);
        pts.push([ids[i], p]);
      }
      if (!pts.length) { svg.innerHTML = ""; return; }
      var spanX = Math.max(1, maxX - minX);
      var spanY = Math.max(1, maxY - minY);
      var parts = [];
      for (var j = 0; j < pts.length; j++) {
        var node = nodes.get(pts[j][0]);
        if (!node || node.hidden) continue;
        var x = 6 + (pts[j][1].x - minX) / spanX * 188;
        var y = 6 + (pts[j][1].y - minY) / spanY * 118;
        var color = node.color && node.color.background ? node.color.background : "#888";
        parts.push('<rect x="' + (x - 1.5).toFixed(1) + '" y="' + (y - 1.5).toFixed(1) + '" width="3" height="3" rx="1" fill="' + color + '"/>');
      }
      svg.innerHTML = parts.join("");
    }, 300);
  }
  // ------------------------------------------------------------------ //
  // 3D "neural brain"
  // ------------------------------------------------------------------ //
  var glLog = [];
  var glowTex = null;

  function pushLog(line) {
    glLog.push(line);
    if (glLog.length > 200) glLog.shift();
    var pre = $("missing-3d-log");
    if (pre) pre.textContent = glLog.join("\n");
  }

  // ---- real error capture: survives into the 3D error block ----
  function isOurAsset(url) {
    if (!url) return false;
    return url.indexOf("/static/") !== -1 || url.indexOf("/vendor/") !== -1 ||
           url.indexOf("app.js") !== -1 || url.indexOf("boot.js") !== -1;
  }
  function installErrorCapture() {
    if (installErrorCapture.done) return;
    installErrorCapture.done = true;
    window.addEventListener("error", function (event) {
      var where = event.filename || (event.target && (event.target.src || event.target.href)) || "";
      if (isOurAsset(where) || !where) {
        pushLog("• window error: " + (event.message || "(no message)") +
          (where ? " @ " + where : "") +
          (event.lineno ? (":" + event.lineno + (event.colno ? ":" + event.colno : "")) : ""));
      }
    }, true);
    window.addEventListener("unhandledrejection", function (event) {
      var r = event.reason;
      pushLog("• unhandledrejection: " + (r && (r.message || r.stack) ? (r.message || r.stack) : String(r)));
    });
  }

  function threeReady() { return typeof window.THREE !== "undefined"; }
  function getForceGraph3DFactory() {
    var f = window.ForceGraph3D;
    if (f && typeof f !== "function" && typeof f["default"] === "function") f = f["default"];
    return (typeof f === "function") ? f : null;
  }
  function graph3dReady() { return getForceGraph3DFactory() !== null; }

  function perfNow() {
    return (window.performance && window.performance.now) ? window.performance.now() : Date.now();
  }

  // three r137 (the vendored build) predates THREE.Timer, which 3d-force-graph
  // 1.80.1 drives in its render loop as `e.timer.update().getDelta()`. A real
  // Timer (three r138+) keeps milliseconds internally, returns SECONDS from
  // getDelta()/getElapsed(), and -- crucially -- returns `this` from
  // update()/setTimescale()/reset() so those call chains stay intact. This
  // stand-in mirrors that contract exactly.
  function makeThreeTimer() {
    var onVisibility = function () {
      if (this._document && this._document.hidden === false) this.reset();
    };
    var Timer = function () {
      this._previousTime = 0;
      this._currentTime = 0;
      this._startTime = perfNow();
      this._delta = 0;
      this._elapsed = 0;
      this._timescale = 1;
      this._document = null;
      this._pageVisibilityHandler = null;
    };
    Timer.prototype.connect = function (doc) {
      this._document = doc;
      if (doc && doc.hidden !== undefined && doc.addEventListener) {
        this._pageVisibilityHandler = onVisibility.bind(this);
        doc.addEventListener("visibilitychange", this._pageVisibilityHandler, false);
      }
      return this;
    };
    Timer.prototype.disconnect = function () {
      if (this._pageVisibilityHandler && this._document && this._document.removeEventListener) {
        this._document.removeEventListener("visibilitychange", this._pageVisibilityHandler);
      }
      this._pageVisibilityHandler = null;
      this._document = null;
      return this;
    };
    Timer.prototype.getDelta = function () { return this._delta / 1000; };
    Timer.prototype.getElapsed = function () { return this._elapsed / 1000; };
    Timer.prototype.getTimescale = function () { return this._timescale; };
    Timer.prototype.setTimescale = function (v) { this._timescale = v; return this; };
    Timer.prototype.reset = function () { this._currentTime = perfNow() - this._startTime; return this; };
    Timer.prototype.dispose = function () { this.disconnect(); };
    Timer.prototype.update = function (timestamp) {
      if (this._pageVisibilityHandler !== null && this._document && this._document.hidden === true) {
        this._delta = 0;
      } else {
        this._previousTime = this._currentTime;
        this._currentTime = (timestamp !== undefined ? timestamp : perfNow()) - this._startTime;
        this._delta = (this._currentTime - this._previousTime) * this._timescale;
        this._elapsed += this._delta;
      }
      return this;
    };
    return Timer;
  }

  // Single source of truth for "does this Timer behave like the real one?":
  // update(t) must return an object exposing a NUMERIC getDelta().
  function timerWorks(Timer) {
    if (typeof Timer !== "function") return false;
    try {
      var r = new Timer().update(perfNow());
      return !!(r && typeof r.getDelta === "function" && typeof r.getDelta() === "number");
    } catch (e) { return false; }
  }

  // Install the shim only when needed, and only if it actually passes the
  // self-test (so we never swap a working Timer for a broken one). Returns a
  // human-readable status that goes straight into the user-visible log.
  function installThreeShims() {
    if (!window.THREE) return "THREE отсутствует — шим не нужен";
    var rev = window.THREE.REVISION || "?";
    if (timerWorks(window.THREE.Timer)) return "THREE.Timer: нативный (r" + rev + ") — исправен";
    window.THREE.Timer = makeThreeTimer();
    if (!timerWorks(window.THREE.Timer)) return "THREE.Timer: шим r" + rev + " НЕ РАБОТАЕТ";
    return "THREE.Timer: шим r" + rev + " — исправен (update().getDelta() => число)";
  }

  // Run the self-test once, right after three loads, and mirror it into the log
  // shown in the error card so a single screenshot tells the whole story.
  var threeShimsLogged = false;
  function ensureThreeShims() {
    var status = installThreeShims();
    if (!threeShimsLogged) {
      threeShimsLogged = true;
      pushLog("• самотест three: " + status);
    }
    return status;
  }

  function describe(text, url) {
    return "[" + (url ? url.split("/").pop() : "?") + "] " + text;
  }

  // Second, independent path: fetch the source (cache: no-store), verify the
  // status/content-type/length and execute it with new Function().
  function loadViaFetch(url, checkFn) {
    return fetch(url, { cache: "no-store" }).then(function (resp) {
      var ct = resp.headers.get("content-type") || "";
      if (!resp.ok) {
        pushLog(describe("fetch HTTP " + resp.status, url));
        throw new Error("HTTP " + resp.status);
      }
      return resp.text().then(function (text) {
        pushLog(describe("fetch ok " + resp.status + ", ct=" + (ct || "?"), url) + ", " + text.length + " байт");
        if (!text || text.length < 50) throw new Error("empty/short body");
        try {
          (new Function(text + "\n//# sourceURL=" + url))();
        } catch (e) {
          pushLog(describe("exec error: " + (e && e.message ? e.message : e), url));
          throw e;
        }
        if (!checkFn()) {
          pushLog(describe("executed, but check failed", url));
          throw new Error("check failed");
        }
        pushLog(describe("ok (fetch+eval)", url));
        return url;
      });
    });
  }

  function loadViaTag(url, checkFn) {
    return new Promise(function (resolve, reject) {
      var el = document.createElement("script");
      el.src = url;
      el.async = false;
      el.onload = function () {
        if (checkFn()) { pushLog(describe("ok (script tag)", url)); resolve(url); }
        else { pushLog(describe("script loaded, but check failed", url)); reject(new Error("check failed after script")); }
      };
      el.onerror = function () { pushLog(describe("script onerror", url)); reject(new Error("script error")); };
      document.head.appendChild(el);
    });
  }

  function loadLibraryUrl(url, checkFn) {
    return loadViaTag(url, checkFn).catch(function () { return loadViaFetch(url, checkFn); });
  }

  function loadLibrary(urls, checkFn, label) {
    if (checkFn()) return Promise.resolve(label || "ready");
    var i = 0;
    function attempt() {
      if (i >= urls.length) return Promise.reject(new Error((label || "library") + ": все источники недоступны"));
      var url = urls[i++];
      return loadLibraryUrl(url, checkFn).catch(function () { return attempt(); });
    }
    return attempt();
  }

  var threeLoad = null;
  var graph3dLoad = null;

  function ensureThree() {
    if (threeReady()) { ensureThreeShims(); return Promise.resolve(); }
    if (threeLoad) return threeLoad;             // share one in-flight load
    threeLoad = loadLibrary([
      "/static/vendor/three.min.js?v=137.5",
      "https://cdnjs.cloudflare.com/ajax/libs/three.js/r137/three.min.js",
      "https://unpkg.com/three@0.137.5/build/three.min.js"
    ], threeReady, "three").then(function () { ensureThreeShims(); },
      function (err) { threeLoad = null; throw err; });
    return threeLoad;
  }
  function ensureGraph3d() {
    if (graph3dReady()) return Promise.resolve();
    if (graph3dLoad) return graph3dLoad;
    graph3dLoad = loadLibrary([
      "/static/vendor/3d-force-graph.min.js?v=1.80.1",
      "https://cdn.jsdelivr.net/npm/3d-force-graph@1.80.1/dist/3d-force-graph.min.js",
      "https://unpkg.com/3d-force-graph@1.80.1/dist/3d-force-graph.min.js"
    ], graph3dReady, "3d-force-graph").catch(function (err) { graph3dLoad = null; throw err; });
    return graph3dLoad;
  }

  // ------------------------------------------------------------------ //
  // 3D chain start/end markers (exactly two scene objects)
  // ------------------------------------------------------------------ //
  // The whole feature is two Meshes (+ two optional glow sprites in quality
  // mode), never a per-node nodeThreeObject, so the draw-call budget is
  // untouched.  Every frame we copy the tracked node's coordinates onto the
  // markers and pulsate them; nothing is rebuilt.

  var markerStart = null;   // { mesh, glow } for the earliest root
  var markerEnd = null;     // { mesh, glow } for the newest event
  var markerRaf = null;
  var markerLogged = false;

  function markerWanted() {
    return !!state.markers && state.mode === "3d" && !!gl && !document.hidden;
  }

  // view id of the earliest root and of the newest event (collapsed-aware).
  function chainEnds() {
    var startRaw = null, endRaw = null;
    for (var i = 0; i < rawOrder.length; i++) {
      var raw = rawNodes[rawOrder[i]];
      if (!raw) continue;
      if (!endRaw || (raw.ts || 0) >= (endRaw.ts || 0)) endRaw = raw;
      if (raw.parent && rawNodes[raw.parent]) continue;   // not a root
      if (!startRaw || (raw.ts || 0) < (startRaw.ts || 0)) startRaw = raw;
    }
    if (!startRaw) startRaw = endRaw;
    var startView = startRaw ? (viewMapReverse[startRaw.id] || startRaw.id) : null;
    var endView = endRaw ? (viewMapReverse[endRaw.id] || endRaw.id) : null;
    return { start: startView, end: endView };
  }

  function markerMesh(color) {
    var geo = new THREE.ConeGeometry(0.5, 1, 3);  // unit size; scaled per node
    geo.rotateX(Math.PI / 2);  // tip points +Z -> lookAt(camera) shows a triangle
    var mat = new THREE.MeshBasicMaterial({ color: color, transparent: true, opacity: 0.95 });
    var mesh = new THREE.Mesh(geo, mat);
    mesh.frustumCulled = false;
    return mesh;
  }

  function markerGlow(color) {
    var sprite = new THREE.Sprite(new THREE.SpriteMaterial({
      map: glowTexture(),
      color: color,
      blending: THREE.AdditiveBlending,
      transparent: true,
      opacity: 0.55,
      depthWrite: false
    }));
    sprite.scale.set(5, 5, 1);  // unit base; placeMarker scales it per frame
    return sprite;
  }

  function ensureMarkers() {
    if (typeof THREE === "undefined" || !gl) return;
    var scene;
    try { scene = gl.scene(); } catch (e) { return; }
    if (!scene) return;
    if (!markerStart) {
      markerStart = { mesh: markerMesh(MARKER_GREEN), glow: markerGlow(MARKER_GREEN) };
      markerEnd = { mesh: markerMesh(MARKER_YELLOW), glow: markerGlow(MARKER_YELLOW) };
      scene.add(markerStart.mesh); scene.add(markerStart.glow);
      scene.add(markerEnd.mesh); scene.add(markerEnd.glow);
      if (!markerLogged) {
        markerLogged = true;
        pushLog("• метки начала/конца: 2 объекта (конусы" +
          (state.perf ? "" : " + 2 спрайта свечения") + ")");
      }
    }
    var glowOn = !state.perf;   // halos only in quality mode (keeps perf at +2)
    markerStart.glow.visible = glowOn;
    markerEnd.glow.visible = glowOn;
  }

  function nodeById(data, id) {
    if (!data || !data.nodes) return null;
    for (var i = 0; i < data.nodes.length; i++) if (data.nodes[i].id === id) return data.nodes[i];
    return null;
  }

  // The library sizes a node sphere as radius = cbrt(val) * nodeRelSize.
  function glNodeRelSize() {
    try {
      if (gl && gl.nodeRelSize) {
        var v = gl.nodeRelSize();
        if (typeof v === "number" && v > 0) return v;
      }
    } catch (e) {}
    return 4;  // 3d-force-graph default
  }

  function nodeRadiusOf(node) {
    return Math.cbrt(Math.max(0, (node && node.val) || 1)) * glNodeRelSize();
  }

  // Place one marker beside its node, facing the camera, and pulsate it.
  function placeMarker(marker, node, camPos, phase) {
    // A marker is shown whenever its node is present in the 3D data set; the
    // coordinates default to the origin until the layout assigns them.
    var visible = !!(marker && node);
    if (marker) marker.mesh.visible = visible;
    if (marker && marker.glow) marker.glow.visible = visible && !state.perf;
    if (!visible) return;
    var nx = node.x || 0, ny = node.y || 0, nz = node.z || 0;
    var cam = gl.camera();
    var right = { x: 1, y: 0, z: 0 };
    if (cam && cam.matrixWorld) {
      var e = cam.matrixWorld.elements;
      right = { x: e[0], y: e[1], z: e[2] };
    }
    // Relative to the node's own diameter, so the marker is about the size of the
    // ball and sits right next to it, at any zoom and on any graph.
    var diameter = 2 * nodeRadiusOf(node);
    var size = diameter * MARKER_NODE_SIZE;
    var off = diameter * MARKER_OFFSET_FACTOR;
    var px = nx + right.x * off;
    var py = ny + right.y * off;
    var pz = nz + right.z * off;
    marker.mesh.position.set(px, py, pz);
    var scale = size * (0.85 + 0.30 * phase);
    marker.mesh.scale.set(scale, scale, scale);
    marker.mesh.material.opacity = 0.55 + 0.45 * phase;
    if (camPos) marker.mesh.lookAt(camPos.x, camPos.y, camPos.z);
    if (marker.glow) {
      marker.glow.position.set(px, py, pz);
      var g = scale * 5;
      marker.glow.scale.set(g, g, 1);
      marker.glow.material.opacity = 0.25 + 0.5 * phase;
    }
  }

  function updateMarkers(now) {
    if (!markerWanted()) { hideMarkers(); return; }
    ensureMarkers();
    if (!markerStart) return;
    var data;
    try { data = gl.graphData(); } catch (e) { return; }
    var ends = chainEnds();
    var phase = (now % (2 * MARKER_PULSE_MS)) / MARKER_PULSE_MS;
    if (phase > 1) phase = 2 - phase;
    var camPos = null;
    try { camPos = gl.camera().position; } catch (e) {}
    placeMarker(markerStart, nodeById(data, ends.start), camPos, phase);
    placeMarker(markerEnd, nodeById(data, ends.end), camPos, phase);
    // The library pauses the loop once the layout settles and rotation is off;
    // then nobody would draw our pulsation, so we draw a single frame ourselves.
    if (settled3d && !rotateTimer && !userInteracting) forceRender();
  }

  function forceRender() {
    try {
      var r = gl.renderer();
      if (r && r.render) r.render(gl.scene(), gl.camera());
    } catch (e) {}
  }

  function hideMarkers() {
    if (markerStart) markerStart.mesh.visible = false;
    if (markerEnd) markerEnd.mesh.visible = false;
    if (markerStart && markerStart.glow) markerStart.glow.visible = false;
    if (markerEnd && markerEnd.glow) markerEnd.glow.visible = false;
  }

  function markerLoop() {
    markerRaf = null;
    if (!markerWanted()) return;
    updateMarkers(perfNow());
    markerRaf = requestAnimationFrame(markerLoop);
  }

  function startMarkerTimer() {
    if (markerRaf !== null) return;
    if (!markerWanted()) return;
    markerRaf = requestAnimationFrame(markerLoop);
  }

  function stopMarkerTimer() {
    if (markerRaf !== null) { cancelAnimationFrame(markerRaf); markerRaf = null; }
    hideMarkers();
  }

  function glowTexture() {
    if (glowTex) return glowTex;
    var size = 128;
    var canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    var ctx = canvas.getContext("2d");
    var g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    g.addColorStop(0, "rgba(255,255,255,0.95)");
    g.addColorStop(0.25, "rgba(255,255,255,0.55)");
    g.addColorStop(0.6, "rgba(255,255,255,0.12)");
    g.addColorStop(1, "rgba(255,255,255,0)");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, size, size);
    glowTex = new THREE.CanvasTexture(canvas);
    return glowTex;
  }

  function makeGlowObject(node) {
    if (typeof THREE === "undefined") return undefined;
    var radius = 4 + Math.sqrt(node.val || 1) * 4.5;
    if (node.type === "error") radius *= 1.5;
    var group = new THREE.Group();
    var geo = new THREE.SphereGeometry(radius, 16, 12);
    var mat = new THREE.MeshBasicMaterial({ color: new THREE.Color(node.color) });
    group.add(new THREE.Mesh(geo, mat));
    var sprite = new THREE.Sprite(new THREE.SpriteMaterial({
      map: glowTexture(),
      color: new THREE.Color(node.color),
      blending: THREE.AdditiveBlending,
      transparent: true,
      opacity: 0.6,
      depthWrite: false
    }));
    var scale = radius * 6;
    sprite.scale.set(scale, scale, 1);
    group.add(sprite);
    return group;
  }

  function start3d() {
    installErrorCapture();
    if (threeReady() && graph3dReady()) { ensureThreeShims(); hide3dError(); init3d(); sync3d(); return; }
    var st = $("gl-status");
    if (st) st.textContent = "загружаю 3D-библиотеки…";
    ensureThree().then(ensureGraph3d).then(function () {
      if (st) st.textContent = "";
      hide3dError();
      init3d();
      sync3d();
    }).catch(function (err) {
      if (st) st.textContent = "3D недоступен";
      pushLog("• загрузка не удалась: " + (err && err.message ? err.message : err));
      show3dError("Не удалось загрузить three.js / 3d-force-graph.");
    });
  }

  function hide3dError() {
    var box = $("missing-3d");
    if (box) box.hidden = true;
  }

  function webglSupported() {
    try {
      var canvas = document.createElement("canvas");
      return !!(canvas.getContext && (canvas.getContext("webgl") || canvas.getContext("experimental-webgl")));
    } catch (e) { return false; }
  }

  function diagLines() {
    return [
      "THREE: " + (window.THREE ? (window.THREE.REVISION || "?") : "нет") +
        ", THREE.Timer: " + (window.THREE && window.THREE.Timer ? "есть" : "нет"),
      "ForceGraph3D: " + typeof window.ForceGraph3D +
        (window.ForceGraph3D && typeof window.ForceGraph3D["default"] === "function" ? " (+default)" : ""),
      "WebGL: " + (webglSupported() ? "доступен" : "недоступен"),
      "UA: " + (navigator && navigator.userAgent ? navigator.userAgent : "?")
    ];
  }

  function show3dError(message) {
    var box = $("missing-3d");
    var pre = $("missing-3d-log");
    if (pre) {
      pre.textContent = (message ? message + "\n" : "") +
        diagLines().join("\n") + "\n--- журнал ---\n" + glLog.join("\n");
    }
    if (box) box.hidden = false;
  }

  // Number of real (non-collapsed) nodes currently in the 3D data set.
  function glRealCount() {
    try {
      var data = gl.graphData();
      return data && data.nodes ? data.nodes.length : 0;
    } catch (e) { return 0; }
  }

  // Number of links currently in the 3D data set.
  function glRealLinks() {
    try {
      var data = gl.graphData();
      return data && data.links ? data.links.length : 0;
    } catch (e) { return 0; }
  }

  // Whether the costly per-node glow sprites should be built.  Sprites add a
  // mesh + sprite material per node (plus the base sphere), i.e. hundreds of
  // draw calls, so they are limited to small graphs and to quality mode.
  function spritesWanted() {
    if (state.perf) return false;
    var count = glRealCount();
    if (count <= 0 || count > GL_SPRITE_LIMIT) return false;
    if (glRealLinks() > GL_ARROW_LIMIT) return false;
    return true;
  }

  function init3d() {
    var box = $("gl3d");
    if (!box) return;
    if (gl) {
      try {
        gl.width(box.clientWidth).height(box.clientHeight);
        apply3dPerf();
        ensureMarkers();
        startMarkerTimer();
        if (gl.resumeAnimation) gl.resumeAnimation();
      } catch (e) {}
      return;
    }
    var factory = getForceGraph3DFactory();
    if (!factory) { show3dError("3d-force-graph не загрузился (window.ForceGraph3D отсутствует)."); return; }
    try {
      // OrbitControls (controlType "orbit") is the one that supports the cheap,
      // library-driven controls().autoRotate; the default trackball does not.
      gl = factory({ controlType: "orbit" })(box)
        .backgroundColor("#04070c")
        .showNavInfo(false)
        .nodeColor(function (n) { return n.color; })
        .nodeVal(function (n) { return n.val; })
        .nodeOpacity(0.95)
        .linkColor(function (l) { return l.color; })
        // Links are thin lines by default (linkWidth 0): a non-zero width
        // would turn every link into its own cylinder Mesh.
        .linkWidth(0)
        .linkOpacity(0.45)
        .linkDirectionalArrowLength(0)
        .linkCurvature(0)
        .onNodeClick(function (n) { on3dNodeClick(n); })
        .onBackgroundClick(function () { stopRotate(); })
        // After the layout cools down nothing moves (unless the camera
        // rotates), so stop the render loop to save the CPU/GPU.
        .onEngineStop(function () {
          settled3d = true;
          if (!wantRotate()) pauseLoopIfIdle();
        });
      try { gl.d3Force("charge").strength(-120); } catch (e) {}
      bind3dControls();
      apply3dPerf();
      if (wantRotate()) startRotate(); else stopRotate();
      ensureMarkers();
      startMarkerTimer();
    } catch (e) {
      gl = null;
      glLog.push("• ошибка инициализации: " + e.message);
      show3dError("Ошибка инициализации 3D.");
    }
  }

  // Push the performance/quality knobs onto the live 3D instance.  Cheap to
  // call: it only sets properties, except when sprites are toggled.
  var last3dPerfSig = null;

  function apply3dPerf() {
    if (!gl) return;
    var count = glRealCount();
    var big = count > GL_BIG_GRAPH;
    // Sprites are ~2 extra objects per node, so only for small graphs and only
    // in quality mode -- never together with nodeThreeObjectExtend.
    var sprites = spritesWanted();
    // Links: line (shared) vs per-link cylinder mesh; curvature/arrows add
    // geometry per link.  All three are opt-in and gated by the link count.
    var links = glRealLinks();
    var wantThick = !!state.linkWidth && links <= GL_BIG_LINK;
    var wantCurve = !!state.linkCurve && links <= GL_CURVE_LIMIT;
    var wantArrows = (!!state.linkCurve || !!state.pathEdges) && links <= GL_ARROW_LIMIT;
    var pixel = (state.perf && count > GL_BIG_GRAPH_MEM) ? 1 : (window.devicePixelRatio || 1);

    // Setters like linkWidth/nodeThreeObject rebuild objects, so only actually
    // touch the instance when one of the knobs changed (they are cheap to have
    // been set once, expensive to re-set every second).
    var sig = [big ? 1 : 0, sprites ? 1 : 0, wantThick ? 1 : 0, wantCurve ? 1 : 0,
      wantArrows ? 1 : 0, pixel, state.perf ? 1 : 0, count > 0 ? 1 : 0].join("|");
    if (sig === last3dPerfSig) {
      update3dLimitNote(links, wantThick, wantCurve);
      renderPerfNote();
      return;
    }
    last3dPerfSig = sig;
    try {
      // DOM tooltips are one element per node -> disable on big graphs.
      gl.nodeLabel(state.perf ? null : function (n) { return n.label || ""; });
    } catch (e) {}
    try {
      if (gl.renderer && gl.renderer()) gl.renderer().setPixelRatio(pixel);
    } catch (e) {}
    try {
      // Fewer warmup/cooldown ticks when the graph is big; never spin forever.
      gl.cooldownTicks(big ? 40 : 100);
      gl.warmupTicks(big ? 20 : 60);
    } catch (e) {}
    try {
      // Sprites: swap the node object factory in/out without rebuilding data.
      gl.nodeThreeObject(sprites ? makeGlowObject : null);
      gl.nodeThreeObjectExtend(false);
      // Coarser spheres for big graphs (fewer triangles per node).
      gl.nodeResolution(big ? GL_NODE_RES_BIG : GL_NODE_RES);
    } catch (e) {}
    try {
      gl.linkWidth(wantThick ? function (l) { return l.width; } : 0);
      gl.linkCurvature(wantCurve ? 0.12 : 0);
      gl.linkDirectionalArrowLength(wantArrows ? 3.4 : 0);
    } catch (e) {}
    update3dLimitNote(links, wantThick, wantCurve);
    renderPerfNote();
  }

  // Tell the user in plain words when the 3D view is capped or when the
  // expensive link styles were silently downgraded.
  function update3dLimitNote(links, wantThick, wantCurve) {
    var el = $("gl-limit");
    if (!el) return;
    var notes = [];
    if (state.mode === "3d") {
      if (state.collapse) notes.push("включена свёртка цепочек tool_call → tool_result");
      var raw = rawOrder.length;
      if (raw > GL_SYNC_LIMIT) {
        notes.push("показаны последние " + GL_SYNC_LIMIT + " узлов из " + raw +
          " — сверните цепочки или отфильтруйте типы, чтобы увидеть остальное");
      }
      if (state.linkWidth && !wantThick && links > GL_BIG_LINK) {
        notes.push("толстые связи отключены: " + links + " связей > " + GL_BIG_LINK);
      }
      if (state.linkCurve && !wantCurve && links > GL_CURVE_LIMIT) {
        notes.push("изогнутые связи/стрелки отключены: " + links + " связей > " + GL_CURVE_LIMIT);
      }
    }
    el.textContent = notes.join(" · ");
  }

  // Track the camera's InteractionState so we can (a) pause the loop when idle
  // and (b) resume it while the user drags/zooms.
  function bind3dControls() {
    var ctrl;
    try { ctrl = gl.controls(); } catch (e) { return; }
    if (!ctrl || !ctrl.addEventListener) return;
    ctrl.addEventListener("start", function () { userInteracting = true; resumeLoop(); });
    ctrl.addEventListener("change", function () { resumeLoop(); });
    ctrl.addEventListener("end", function () {
      userInteracting = false;
      // let orbit damping settle, then re-evaluate
      setTimeout(pauseLoopIfIdle, 250);
    });
  }

  // OrbitControls autoRotate drives the camera from inside the animation loop
  // (no manual per-frame camera math); OrbitControls is stopped by the wrapper
  // when the animation loop is paused, exactly like three's Period/Infinity
  // rotation modes.
  function wantRotate() {
    return state.mode === "3d" && !!gl && !document.hidden && !userInteracting;
  }
  function startRotate() {
    if (!gl) return;
    rotateTimer = true;
    var ctrl = safeControls();
    if (ctrl && ("autoRotate" in ctrl)) {
      // Preferred: the library rotates the camera from within its own frame.
      ctrl.autoRotate = true;
      ctrl.autoRotateSpeed = GL_AUTO_ROTATE_SPEED;
      if (gl.resumeAnimation) { try { gl.resumeAnimation(); } catch (e) {} }
    } else {
      // Fallback for a control type without autoRotate (e.g. trackball).
      startManualRotate();
    }
    updateRotateButton();
  }
  function stopRotate() {
    rotateTimer = null;
    stopManualRotate();
    var ctrl = safeControls();
    if (ctrl && ("autoRotate" in ctrl)) { try { ctrl.autoRotate = false; } catch (e) {} }
    // No rotation + settled layout -> nothing moves; pause the loop.
    pauseLoopIfIdle();
    updateRotateButton();
  }
  function safeControls() {
    if (!gl || !gl.controls) return null;
    try { return gl.controls(); } catch (e) { return null; }
  }
  function startManualRotate() {
    stopManualRotate();
    manualRotateTimer = setInterval(function () {
      if (!gl || !wantRotate()) return;
      var cam = gl.camera();
      if (!cam) return;
      var pos = cam.position;
      var radius = Math.hypot(pos.x, pos.z) || 1;
      var theta = Math.atan2(pos.x, pos.z) + GL_AUTO_ROTATE_SPEED * Math.PI / 180;
      pos.x = radius * Math.sin(theta);
      pos.z = radius * Math.cos(theta);
      cam.lookAt(0, 0, 0);
      resumeLoop();
    }, Math.round(1000 / 30));
  }
  function stopManualRotate() {
    if (manualRotateTimer) { clearInterval(manualRotateTimer); manualRotateTimer = null; }
  }
  function resumeLoop() {
    settled3d = false;
    if (gl && gl.resumeAnimation) { try { gl.resumeAnimation(); } catch (e) {} }
  }
  function pauseLoopIfIdle() {
    if (!gl || state.mode !== "3d") return;
    if (rotateTimer || userInteracting || !settled3d) return;
    if (gl.pauseAnimation) { try { gl.pauseAnimation(); } catch (e) {} }
  }
  function updateRotateButton() {
    var b = $("btn-rotate");
    if (!b) return;
    b.textContent = rotateTimer ? "Пауза вращения" : "Включить вращение";
    b.setAttribute("data-active", rotateTimer ? "true" : "false");
  }

  function stop3d() {
    stopRotate();
    stopManualRotate();
    stopMarkerTimer();
    if (gl) { try { if (gl.pauseAnimation) gl.pauseAnimation(); } catch (e) {} }
  }

  // ------------------------------------------------------------------ //
  // performance mode + smoothness meter
  // ------------------------------------------------------------------ //
  var fpsSamples = [];
  var fpsTimer = null;
  var perfExtra = "";
  var lastFrameMs = 0;

  function renderPerfNote() {
    var fps = estimateFps();
    var mode = state.perf ? "производительность" : "качество";
    var r = rendererInfo();
    var parts;
    if (state.mode === "3d") {
      parts = ["3D · " + mode,
        "узлов " + glNodeCount + " / связей " + glLinkCount,
        "draw calls " + r.calls,
        "треугольников " + fmtInt(r.triangles),
        (fps ? "~" + fps + " fps" : "~— fps"),
        "кадр " + (r.ms ? r.ms.toFixed(1) + " мс" : "—")];
    } else {
      parts = ["2D · " + mode, "узлов " + rawOrder.length, (fps ? "~" + fps + " fps" : "~— fps")];
    }
    if (perfExtra) parts.push(perfExtra);
    var el = $("perf-note");
    if (el) el.textContent = parts.join(" · ");
    setText("gl-fps", fps ? String(fps) : "—");
    setText("gl-frame", r.ms ? r.ms.toFixed(1) + " мс" : "—");
    setText("gl-counts", (state.mode === "3d")
      ? (glNodeCount + " / " + glLinkCount)
      : (rawOrder.length + " / " + edgeList.length));
    setText("gl-calls", String(r.calls));
    setText("gl-tris", fmtInt(r.triangles));
    // What is really active right now, so the modes are visibly different.
    setText("gl-mode", state.mode === "3d" ? "3D" : "2D-дорожки");
    setText("gl-layout", state.mode === "3d" ? "—" : state.layout);
    setText("gl-physics", state.mode === "3d" ? "—" : (state.layout === "physics" ? "вкл" : "выкл"));
  }

  function setText(id, value) { var n = $(id); if (n) n.textContent = value; }

  function fmtInt(n) {
    n = Math.round(n || 0);
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  }

  // renderer.info().render reports the real WebGL work: draw calls + triangles.
  // draw calls is the number that matters for 3D (# of objects submitted).
  function rendererInfo() {
    var out = { calls: 0, triangles: 0, ms: lastFrameMs };
    try {
      if (gl && gl.renderer && gl.renderer()) {
        var r = gl.renderer();
        var info = r.info && r.info.render;
        if (info) { out.calls = info.calls || 0; out.triangles = info.triangles || 0; }
      }
    } catch (e) {}
    return out;
  }

  function startFpsMeter() {
    if (fpsTimer) return;
    fpsSamples = [];
    fpsTimer = setInterval(renderPerfNote, FPS_SAMPLE_MS);
    renderPerfNote();
  }

  function stopFpsMeter() {
    if (fpsTimer) { clearInterval(fpsTimer); fpsTimer = null; }
  }

  function estimateFps() {
    if (!fpsSamples.length) return 0;
    var sum = 0;
    for (var i = 0; i < fpsSamples.length; i++) sum += fpsSamples[i];
    return Math.round(sum / fpsSamples.length);
  }

  // Smoothness probe: count requestAnimationFrame ticks for one window and
  // derive both fps and the average frame time (ms).
  function sampleFps() {
    if (document.hidden) return;
    if (state.mode === "3d" && !gl) return;
    var started = perfNow();
    var frames = 0;
    function step() {
      frames++;
      var elapsed = perfNow() - started;
      if (elapsed < 700) requestAnimationFrame(step);
      else {
        if (frames > 1) {
          var ms = elapsed / (frames - 1);
          lastFrameMs = ms;
          fpsSamples.push(1000 / ms);
          if (fpsSamples.length > 8) fpsSamples = fpsSamples.slice(-8);
        }
      }
    }
    requestAnimationFrame(step);
  }

  function setPerfNote(text) {
    perfExtra = text || "";
    renderPerfNote();
  }

  function updatePerfNote() {
    var el = $("perf-note");
    if (!el) return;
    el.setAttribute("data-perf", state.perf ? "true" : "false");
  }

  // Apply performance/quality everywhere (2D + 3D) and refresh what's on screen.
  function applyPerfMode(announce) {
    var st = $("stage-canvas");
    if (st) st.setAttribute("data-perf", state.perf ? "true" : "false");
    // 2D: shadows are per-node; drop them in performance mode.
    var updates = [];
    nodes.forEach(function (n) {
      updates.push({ id: n.id, shadow: state.perf ? false : { enabled: true, color: "rgba(0,0,0,0.5)", size: 8, x: 0, y: 2 } });
    });
    if (updates.length) nodes.update(updates);
    if (state.mode === "3d") { apply3dPerf(); sync3d(true); }
    updatePerfNote();
    if (announce) toast(state.perf ? "Режим производительности" : "Режим качества");
  }

  // Tell the server to (stop) following new sessions.
  function setFollowOnServer(follow) {
    fetch("/api/follow", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ follow: !!follow })
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (data && typeof data.follow === "boolean") {
        state.followSessions = data.follow;
        var cb = $("follow-sessions");
        if (cb) cb.checked = data.follow;
      }
    }).catch(function () { /* offline: keep local value */ });
  }

  function glNodes(items) {
    var tsMin = null, tsMax = null;
    for (var i = 0; i < items.length; i++) {
      var t = items[i].source ? items[i].source.ts : 0;
      if (!t) continue;
      if (tsMin === null || t < tsMin) tsMin = t;
      if (tsMax === null || t > tsMax) tsMax = t;
    }
    var span = (tsMin !== null && tsMax !== null && tsMax > tsMin) ? (tsMax - tsMin) : 0;
    var out = [];
    for (var j = 0; j < items.length; j++) {
      var item = items[j];
      var node = nodes.get(item.id);
      if (!node || node.hidden) continue;
      var type = node._type;
      var ts = item.source ? item.source.ts : 0;
      var ratio = (span > 0 && ts) ? (ts - tsMin) / span : 0.5;
      var col = mixColor(GL_COLD, GL_WARM, ratio);
      if (type === "error") col = { r: 255, g: Math.max(60, col.g - 40), b: Math.max(40, col.b - 60) };
      var base = TYPE_SIZE[type] || DATA_SIZE;
      var val = Math.max(1, Math.pow(base / 6, 2.1));
      if (type === "error") val *= 1.8;
      out.push({
        id: item.id,
        label: node._label || "",
        type: type,
        val: val,
        color: "rgb(" + col.r + "," + col.g + "," + col.b + ")"
      });
    }
    return out;
  }

  function glLinks(items, idSet) {
    var present = idSet || {};
    if (!idSet) {
      for (var p = 0; p < items.length; p++) present[items[p].id] = true;
    }
    // Per-link color/width is only worth its cost for small graphs; on big
    // ones the shared material (linkColor/linkWidth) is used instead.
    var big = glRealLinks() > GL_BIG_LINK || items.length > GL_BIG_LINK;
    var out = [];
    for (var j = 0; j < edgeList.length; j++) {
      var edge = edgeList[j];
      var from = viewMapReverse[edge.from];
      var to = viewMapReverse[edge.to];
      if (!present[from] || !present[to] || from === to) continue;
      var link = { source: from, target: to };
      if (!big) {
        var src = rawNodes[edge.to] || rawNodes[edge.from] || {};
        link.color = hexA(edgeColorFor(src.type), 0.5);
        link.width = src.type === "error" ? 1.4 : 0.7;
      }
      out.push(link);
    }
    return out;
  }

  var lastSync3dAt = 0;
  var last3dSignature = null;
  var glNodeCount = 0;   // nodes actually pushed into the scene (may be capped)
  var glLinkCount = 0;

  // Hard cap: never push more than GL_SYNC_LIMIT nodes into the WebGL scene.
  function glNodeList(items) {
    var all = glNodes(items);
    if (all.length <= GL_SYNC_LIMIT) return all;
    return all.slice(all.length - GL_SYNC_LIMIT);
  }

  function sync3d(force) {
    if (!gl || state.mode !== "3d") return;

    // Rate-limit rebuilds: the live stream appends events constantly and each
    // rebuild restarts the layout, so at most one rebuild every GL_REFRESH_MS.
    var now = Date.now();
    if (!force && now - lastSync3dAt < GL_REFRESH_MS) return;
    lastSync3dAt = now;

    var items = buildView();
    var real = 0;
    for (var i = 0; i < items.length; i++) if (!items[i].group) real++;

    // Too many real nodes: collapse tool chains (3D defaults to collapsed).
    if (real > GL_SYNC_LIMIT && !state.collapse) {
      state.collapse = true;
      var cb = $("collapse-chains");
      if (cb) cb.checked = true;
      setPerfNote("3D: " + real + " узлов — включена свёртка tool_call → tool_result");
      render();
      return;
    }

    try {
      var glNodeListData = glNodeList(items);
      var glIdSet = {};
      for (var gi = 0; gi < glNodeListData.length; gi++) glIdSet[glNodeListData[gi].id] = true;
      var links = glLinks(items, glIdSet);

      // Skip the rebuild (and the layout restart) when the structure did not
      // change -- the live stream re-sends the same graph every second.
      var signature = glNodeListData.length + ":" + links.length + ":" +
        (glNodeListData.length ? glNodeListData[glNodeListData.length - 1].id : "");
      if (!force && signature === last3dSignature) { renderPerfNote(); return; }
      last3dSignature = signature;

      glNodeCount = glNodeListData.length;
      glLinkCount = links.length;
      settled3d = false;   // new data -> the layout runs again
      gl.graphData({ nodes: glNodeListData, links: links });
      apply3dPerf();
      if (state.pathNodes) glHighlight3d();
      // Chain-end markers follow the two nodes across every rebuild.
      if (state.markers) { ensureMarkers(); startMarkerTimer(); }
      else stopMarkerTimer();
    } catch (e) {
      glLog.push("• ошибка данных: " + e.message);
    }
  }

  function glHighlight3d() {
    if (!gl || state.mode !== "3d") return;
    var path = state.pathNodes || {};
    gl.nodeColor(function (n) { return path[n.id] ? "#ffffff" : dimRgb(n.color); });
    gl.nodeVal(function (n) { return path[n.id] ? (n.val || 1) * 1.6 : (n.val || 1); });
  }

  function dimRgb(rgb) {
    var m = /rgb\((\d+),\s*(\d+),\s*(\d+)\)/.exec(rgb);
    if (!m) return "#33404f";
    return "rgb(" + Math.round(m[1] * 0.32) + "," + Math.round(m[2] * 0.32) + "," + Math.round(m[3] * 0.32) + ")";
  }

  function glFlyTo(nodeId) {
    if (!gl) return;
    var data;
    try { data = gl.graphData(); } catch (e) { return; }
    var n = null;
    for (var i = 0; i < data.nodes.length; i++) {
      if (data.nodes[i].id === nodeId) { n = data.nodes[i]; break; }
    }
    if (!n || n.x === undefined) return;
    var r = Math.hypot(n.x, n.y, n.z) || 1;
    var k = (r + GL_FLY_DIST) / r;
    try { gl.cameraPosition({ x: n.x * k, y: n.y * k, z: n.z * k }, { x: n.x, y: n.y, z: n.z }, 1200); } catch (e) {}
  }

  function on3dNodeClick(n) {
    stopRotate();
    state.selected = n.id;
    selectNode(n.id, { fly: true });
  }

  function fit3d() {
    if (!gl) return;
    try { gl.zoomToFit(700, 60); } catch (e) {}
  }
  // ------------------------------------------------------------------ //
  // mode switching + preferences
  // ------------------------------------------------------------------ //
  function updateModeTabs() {
    var tabs = document.querySelectorAll(".mode-tab");
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].setAttribute("data-active", tabs[i].getAttribute("data-mode") === state.mode ? "true" : "false");
    }
  }

  function updatePanels() {
    var layoutPanel = $("panel-layout");
    var panel3d = $("panel-3d");
    var is3d = state.mode === "3d";
    if (layoutPanel) layoutPanel.hidden = is3d;
    if (panel3d) panel3d.hidden = !is3d;
  }

  function switchMode(mode) {
    if (mode === state.mode) return;
    state.mode = mode;
    var canvas = $("stage-canvas");
    var glbox = $("gl3d");
    var mini = $("minimap");

    if (mode === "3d") {
      if (!state.collapse) {
        state.collapse = true;
        var cb = $("collapse-chains");
        if (cb) cb.checked = true;
      }
      stopFpsMeter();
      if (mini) mini.hidden = true;
      canvas.hidden = true;
      glbox.hidden = false;
      updateModeTabs();
      updatePanels();
      render();
      start3d();
      startFpsMeter();
      sampleFps();
    } else {
      stop3d();
      $("missing-3d").hidden = true;
      glbox.hidden = true;
      canvas.hidden = false;
      if (mini) mini.hidden = false;
      updateModeTabs();
      updatePanels();
      render();
      applyLayout();
      if (network) network.redraw();
      applyPerfMode();
    }
    savePrefs();
    updateRotateButton();
  }

  function savePrefs() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify({
        mode: state.mode,
        layout: state.layout,
        autoLanes: state.autoLanes,
        collapse: state.collapse,
        follow: state.follow,
        showLabels: state.showLabels,
        sizeByType: state.sizeByType,
        edgeColor: state.edgeColor,
        hideUser: state.hideUser,
        perf: state.perf,
        followSessions: state.followSessions,
        linkWidth: state.linkWidth,
        linkCurve: state.linkCurve,
        markers: state.markers,
        laneShifts: state.laneShifts
      }));
    } catch (e) { /* storage disabled */ }
  }

  function modeFromUrl() {
    try {
      var m = new URLSearchParams(location.search).get("mode");
      if (m === "2d" || m === "3d") return m;
    } catch (e) {}
    return null;
  }

  function loadPrefs() {
    var p;
    try { p = JSON.parse(localStorage.getItem(STORE_KEY) || "null"); } catch (e) { p = null; }
    if (!p) { var onlyUrl = modeFromUrl(); if (onlyUrl) state.mode = onlyUrl; return; }
    // Migration: the removed "float" mode silently falls back to 2D and the
    // normalized value is written back, so the app never opens in a mode the UI
    // no longer offers.
    if (p.mode === "float") {
      state.mode = "2d";
      savePrefs();
    } else if (p.mode === "2d" || p.mode === "3d") {
      state.mode = p.mode;
    }
    if (LAYOUT_LIST.indexOf(p.layout) >= 0) state.layout = p.layout;
    if (typeof p.autoLanes === "boolean") state.autoLanes = p.autoLanes;
    if (typeof p.collapse === "boolean") state.collapse = p.collapse;
    if (typeof p.follow === "boolean") state.follow = p.follow;
    if (typeof p.showLabels === "boolean") state.showLabels = p.showLabels;
    if (typeof p.sizeByType === "boolean") state.sizeByType = p.sizeByType;
    if (typeof p.edgeColor === "boolean") state.edgeColor = p.edgeColor;
    if (typeof p.hideUser === "boolean") state.hideUser = p.hideUser;
    if (typeof p.perf === "boolean") state.perf = p.perf;
    if (typeof p.followSessions === "boolean") state.followSessions = p.followSessions;
    if (typeof p.linkWidth === "boolean") state.linkWidth = p.linkWidth;
    if (typeof p.linkCurve === "boolean") state.linkCurve = p.linkCurve;
    if (typeof p.markers === "boolean") state.markers = p.markers;
    if (p.laneShifts && typeof p.laneShifts === "object") {
      // keep only finite numbers, so a corrupt entry cannot push a node away
      for (var sid in p.laneShifts) {
        if (p.laneShifts.hasOwnProperty(sid) && typeof p.laneShifts[sid] === "number" &&
            isFinite(p.laneShifts[sid])) state.laneShifts[sid] = p.laneShifts[sid];
      }
    }
    var urlMode = modeFromUrl();
    if (urlMode) state.mode = urlMode; // ?mode=... overrides localStorage
    if (state.mode === "3d") state.collapse = true; // 3D defaults to collapsed chains
  }

  function syncUiFromState() {
    var set = function (id, val) { var n = $(id); if (n) { if (n.type === "checkbox") n.checked = !!val; else n.value = val; } };
    var sel = $("layout-mode");
    if (sel && LAYOUT_LIST.indexOf(state.layout) >= 0) sel.value = state.layout;
    set("collapse-chains", state.collapse);
    set("follow", state.follow);
    set("show-labels", state.showLabels);
    set("node-size", state.sizeByType);
    set("color-edges", state.edgeColor);
    set("auto-lanes", state.autoLanes);
    set("filter-actors", state.hideUser);
    set("perf-mode", state.perf);
    set("follow-sessions", state.followSessions);
    set("gl-thick-links", state.linkWidth);
    set("gl-curved-links", state.linkCurve);
    set("gl-markers", state.markers);
    var slider = $("time-window");
    if (slider) {
      var idx = 0;
      for (var i = 0; i < MINUTES_WINDOW.length; i++) if (MINUTES_WINDOW[i] * 60 === state.windowSeconds) { idx = i; break; }
      slider.value = String(idx);
    }
  }
  // ------------------------------------------------------------------ //
  // websocket
  // ------------------------------------------------------------------ //
  function connect() {
    if (ws) { try { ws.close(); } catch (e) {} }
    var protocol = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(protocol + "://" + location.host + "/ws");
    ws.onopen = function () {
      $("conn-dot").setAttribute("data-state", "online");
      $("conn-label").textContent = "в эфире";
    };
    ws.onmessage = function (event) {
      var message;
      try { message = JSON.parse(event.data); } catch (e) { return; }
      if (message.type === "init") ingestInit(message);
      else if (message.type === "events") handleEvents(message);
      else if (message.type === "stats") { if (message.counts) applyCounts(message.counts); if (message.status) applyStatus(message.status); }
      else if (message.type === "reload") ingestInit(message);
      else if (message.type === "session_opened") {
        if (message.result && message.result.ok) toast("Открыта " + (message.result.name || "сессия"));
        else if (message.result) toast("Не удалось: " + (message.result.error || ""));
      } else if (message.type === "log") { pushServerLog(message.entry); }
      else if (message.type === "follow") {
        state.followSessions = !!message.follow;
        var fb = $("follow-sessions");
        if (fb) fb.checked = state.followSessions;
      }
      else if (message.type === "pong") { /* heartbeat */ }
    };
    ws.onclose = function () {
      $("conn-dot").setAttribute("data-state", "offline");
      $("conn-label").textContent = "переподключение…";
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connect, 1500);
    };
    ws.onerror = function () { $("conn-dot").setAttribute("data-state", "waiting"); };
  }
  function send(message) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message));
  }

  // ------------------------------------------------------------------ //
  // export
  // ------------------------------------------------------------------ //
  function exportGraph(kind) {
    fetch("/api/graph/export?kind=" + encodeURIComponent(kind))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          $("export-status").textContent = "Сохранено: " + data.path;
          toast("Сохранено: " + data.path.split(/[\\/]/).pop());
        } else {
          $("export-status").textContent = "Ошибка: " + (data.error || "unknown");
          toast("Не удалось сохранить (" + kind + ")");
        }
      }).catch(function () { toast("Ошибка экспорта"); });
  }

  function exportPng() {
    var container = $("stage-canvas");
    var canvases = container.querySelectorAll("canvas");
    if (!canvases.length) { toast("Нет данных для экспорта"); return; }
    var rect = container.getBoundingClientRect();
    var out = document.createElement("canvas");
    out.width = Math.max(1, Math.floor(rect.width));
    out.height = Math.max(1, Math.floor(rect.height));
    var ctx = out.getContext("2d");
    ctx.fillStyle = "#0a0e14";
    ctx.fillRect(0, 0, out.width, out.height);
    canvases.forEach(function (canvas) {
      var box = canvas.getBoundingClientRect();
      try { ctx.drawImage(canvas, box.left - rect.left, box.top - rect.top, box.width, box.height); }
      catch (e) { /* skip non-drawable layer */ }
    });
    try {
      var link = document.createElement("a");
      link.download = "graff-" + new Date().toISOString().replace(/[:.]/g, "-") + ".png";
      link.href = out.toDataURL("image/png");
      link.click();
      toast("PNG сохранён");
    } catch (e) { toast("Не удалось сохранить PNG"); }
  }

  function deleteSelected() {
    if (!state.selected) { toast("Узел не выбран"); return; }
    var node = nodes.get(state.selected);
    if (!node) return;
    fetch("/api/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: node._raw, cascade: true })
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (data.ok) { toast("Скрыто узлов: " + data.deleted); clearSelection(); }
      else toast("Не удалось скрыть: " + (data.error || ""));
    }).catch(function () { toast("Ошибка запроса"); });
  }

  // ------------------------------------------------------------------ //
  // ui wiring
  // ------------------------------------------------------------------ //
  function wireUi() {
    var tabs = document.querySelectorAll(".mode-tab");
    for (var t = 0; t < tabs.length; t++) {
      (function (tab) {
        tab.addEventListener("click", function () { switchMode(tab.getAttribute("data-mode")); });
      })(tabs[t]);
    }

    var slider = $("time-window");
    slider.max = String(MINUTES_WINDOW.length - 1);
    slider.addEventListener("input", function (event) {
      var index = parseInt(event.target.value, 10) || 0;
      var minutes = MINUTES_WINDOW[index] || 0;
      state.windowSeconds = minutes * 60;
      $("time-window-value").textContent = minutes === 0 ? "всё" : (minutes < 1 ? (minutes * 60) + " сек" : minutes + " мин");
      recomputeWindow();
      applyViewState();
      if (state.mode === "3d") sync3d();
    });

    $("filter-actors").addEventListener("change", function (e) {
      state.hideUser = e.target.checked;
      applyViewState();
      if (state.mode === "3d") sync3d();
      savePrefs();
    });

    $("layout-mode").addEventListener("change", function (e) {
      state.layout = e.target.value;
      if (autoMode === "lanes" && e.target.value !== "lanes") autoMode = null;
      applyLayout();
      savePrefs();
    });
    $("node-size").addEventListener("change", function (e) {
      state.sizeByType = e.target.checked;
      var updates = [];
      nodes.forEach(function (n) { updates.push({ id: n.id, size: state.sizeByType ? (TYPE_SIZE[n._type] || DATA_SIZE) : DATA_SIZE }); });
      if (updates.length) nodes.update(updates);
      savePrefs();
    });
    $("color-edges").addEventListener("change", function (e) {
      state.edgeColor = e.target.checked;
      var updates = [];
      edges.forEach(function (edge) {
        var color = state.edgeColor && edge._raw ? baseColor(rawNodes[edge._raw]) : "#4b5a6e";
        updates.push({ id: edge.id, color: { color: hexA(color, 0.55), highlight: "#ffffff", hover: color } });
      });
      if (updates.length) edges.update(updates);
      savePrefs();
    });
    $("collapse-chains").addEventListener("change", function (e) {
      state.collapse = e.target.checked;
      state.selected = null;
      clearSelection();
      render();
      savePrefs();
    });
    $("follow").addEventListener("change", function (e) {
      state.follow = e.target.checked;
      if (state.follow && state.mode !== "3d") focusLatest(true);
      savePrefs();
    });
    $("show-labels").addEventListener("change", function (e) {
      state.showLabels = e.target.checked;
      var updates = [];
      nodes.forEach(function (n) { updates.push({ id: n.id, label: state.showLabels ? (n._label || "") : "" }); });
      if (updates.length) nodes.update(updates);
      savePrefs();
    });
    $("auto-lanes").addEventListener("change", function (e) {
      state.autoLanes = e.target.checked;
      if (state.autoLanes) maybeAutoLanes();
      savePrefs();
    });

    $("perf-mode").addEventListener("change", function (e) {
      state.perf = e.target.checked;
      applyPerfMode(true);
      savePrefs();
    });
    $("follow-sessions").addEventListener("change", function (e) {
      state.followSessions = e.target.checked;
      setFollowOnServer(state.followSessions);
      savePrefs();
    });

    $("btn-fit").addEventListener("click", function () { if (network) network.fit({ animation: true }); });
    $("btn-latest").addEventListener("click", function () { focusLatest(true); });
    $("btn-rotate").addEventListener("click", function () { if (rotateTimer) stopRotate(); else startRotate(); });
    $("gl-thick-links").addEventListener("change", function (e) {
      state.linkWidth = e.target.checked;
      apply3dPerf();
      savePrefs();
    });
    $("gl-curved-links").addEventListener("change", function (e) {
      state.linkCurve = e.target.checked;
      apply3dPerf();
      savePrefs();
    });
    $("btn-fit3d").addEventListener("click", fit3d);
    if ($("gl-markers")) $("gl-markers").addEventListener("change", function (e) {
      state.markers = e.target.checked;
      if (state.markers && state.mode === "3d") { ensureMarkers(); startMarkerTimer(); }
      else stopMarkerTimer();
      if (state.mode === "3d") sync3d(true);
      savePrefs();
    });
    if ($("btn-reset-shifts")) $("btn-reset-shifts").addEventListener("click", resetLaneShifts);

    $("missing-3d-retry").addEventListener("click", function () {
      hide3dError();
      glLog = [];
      var pre = $("missing-3d-log");
      if (pre) pre.textContent = "";
      start3d();
    });
    $("missing-3d-to2d").addEventListener("click", function () { switchMode("2d"); });

    var searchTimer = null;
    $("search-box").addEventListener("input", function (e) {
      state.search = e.target.value || "";
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = setTimeout(runSearch, 160);
    });
    $("search-box").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { if (searchTimer) clearTimeout(searchTimer); runSearch(); gotoFirstMatch(); }
    });

    $("btn-refresh").addEventListener("click", refreshSessions);
    $("btn-open").addEventListener("click", function () {
      var select = $("session-select");
      if (!select.value) { toast("Сессия не выбрана"); return; }
      openSession(select.value);
    });
    $("btn-enrich").addEventListener("click", runEnrich);

    $("btn-save-json").addEventListener("click", function () { exportGraph("json"); });
    $("btn-save-graphml").addEventListener("click", function () { exportGraph("graphml"); });
    $("btn-save-gexf").addEventListener("click", function () { exportGraph("gexf"); });
    $("btn-save-png").addEventListener("click", exportPng);

    $("details-close").addEventListener("click", function () { $("details").setAttribute("data-open", "false"); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { $("details").setAttribute("data-open", "false"); clearSelection(); }
    });
    $("btn-copy").addEventListener("click", function () {
      var text = $("details-text").textContent || "";
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(function () { toast("Текст скопирован"); });
    });
    $("btn-delete").addEventListener("click", deleteSelected);

    window.addEventListener("resize", function () {
      if (gl) { var box = $("gl3d"); try { gl.width(box.clientWidth).height(box.clientHeight); } catch (e) {} }
    });
  }

  function gotoFirstMatch() {
    if (!state.matches) return;
    for (var id in state.matches) {
      if (state.matches.hasOwnProperty(id)) { selectNode(id); return; }
    }
  }
  // ------------------------------------------------------------------ //
  // boot
  // ------------------------------------------------------------------ //
  function recomputeWindow() {
    if (state.windowSeconds <= 0) { state.cutoffTs = 0; return; }
    var latest = 0;
    for (var i = 0; i < rawOrder.length; i++) {
      var ts = rawNodes[rawOrder[i]].ts || 0;
      if (ts > latest) latest = ts;
    }
    state.cutoffTs = latest > 0 ? latest - state.windowSeconds : 0;
  }

  function tickUptime() {
    var seconds = Math.floor((Date.now() - status.startedWall) / 1000);
    if (seconds < 0) seconds = 0;
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var s = seconds % 60;
    $("stat-uptime").textContent = (h > 0 ? String(h).padStart(2, "0") + ":" : "") +
      String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
  }

  function failSafe(label) {
    return function () { console.error("GRAFF " + label, arguments); };
  }

  function boot() {
    loadPrefs();
    buildLegend();
    buildTypeFilters();
    buildNetwork();
    wireUi();
    syncUiFromState();
    updateModeTabs();
    updatePanels();
    updateRotateButton();

    var is3d = state.mode === "3d";
    $("stage-canvas").hidden = is3d;
    $("gl3d").hidden = !is3d;
    $("minimap").hidden = is3d;
    $("graph-waiting").hidden = false;
    $("graph-empty").hidden = true;

    if (is3d) { render(); start3d(); }
    else {
      applyLayout();
    }

    applyPerfMode();
    startFpsMeter();
    setInterval(sampleFps, 3000);

    // Pause all animation while the tab is hidden (browser + 3D).
    document.addEventListener("visibilitychange", onVisibilityChange);

    connect();
    setInterval(tickUptime, 1000);
    setInterval(function () { send({ type: "ping", t: Date.now() }); }, 15000);
    setInterval(drawMinimap, 4000);
    setInterval(function () { if (state.mode === "3d") sync3d(false); }, 4000);
    fetch("/api/sessions").then(function (r) { return r.json(); }).then(function (d) { applySessions(d.sessions || []); }).catch(failSafe("sessions"));
    fetch("/api/ai").then(function (r) { return r.json(); }).then(applyAiStatus).catch(failSafe("ai"));
    fetch("/api/state").then(function (r) { return r.json(); }).then(function (d) {
      if (d && typeof d.follow === "boolean") {
        state.followSessions = d.follow;
        var cb = $("follow-sessions");
        if (cb) cb.checked = d.follow;
      }
    }).catch(failSafe("state"));
  }

  function onVisibilityChange() {
    if (document.hidden) {
      // Freeze everything while the tab is hidden (no CPU/GPU burn).
      stopRotate();
      stopMarkerTimer();
      if (gl) { try { if (gl.pauseAnimation) gl.pauseAnimation(); } catch (e) {} }
      stopFpsMeter();
    } else {
      if (state.mode === "3d" && gl) {
        apply3dPerf();
        sync3d(true);
        if (wantRotate()) startRotate(); else stopRotate();
        startMarkerTimer();
      }
      startFpsMeter();
      sampleFps();
    }
  }

  // small surface for debugging / tests
  window.GRAFF = {
    state: state,
    laneNames: LANE_NAMES,
    laneHeight: LANE_HEIGHT,
    laneGap: LANE_GAP,
    glBuildLimit: GL_BUILD_LIMIT,
    switchMode: switchMode,
    // diagnostics: load the 3D libraries without constructing a graph
    ensure3d: function () { return ensureThree().then(ensureGraph3d); },
    start3d: start3d,
    ready: { three: threeReady, graph3d: graph3dReady },
    // Timer shim introspection: what three exposes and whether it is sane.
    timer: function () {
      var out = { revision: window.THREE ? (window.THREE.REVISION || null) : null,
        present: !!(window.THREE && window.THREE.Timer), works: false, delta: null };
      if (out.present) {
        try {
          var r = new window.THREE.Timer().update(perfNow());
          out.delta = (r && typeof r.getDelta === "function") ? r.getDelta() : null;
          out.works = typeof out.delta === "number";
        } catch (e) { out.error = e && e.message ? e.message : String(e); }
      }
      return out;
    },
    log: function () { return glLog.slice(); },
    perf: function () {
      var r = rendererInfo();
      return { perf: state.perf, nodes: rawOrder.length,
        glNodes: glNodeCount, glLinks: glLinkCount,
        spriteLimit: GL_SPRITE_LIMIT, syncLimit: GL_SYNC_LIMIT,
        linkLimit: GL_BIG_LINK, curveLimit: GL_CURVE_LIMIT,
        drawCalls: r.calls, triangles: r.triangles, frameMs: r.ms,
        fps: estimateFps() };
    },
    renderer: rendererInfo,
    markers: function () {
      function snap(m, hex) {
        if (!m) return null;
        return {
          visible: m.mesh.visible,
          color: "#" + (hex >>> 0).toString(16).padStart(6, "0"),
          pos: { x: m.mesh.position.x, y: m.mesh.position.y, z: m.mesh.position.z },
          scale: m.mesh.scale.x,
          opacity: m.mesh.material.opacity,
          glowScale: m.glow ? m.glow.scale.x : null
        };
      }
      return { start: snap(markerStart, MARKER_GREEN), end: snap(markerEnd, MARKER_YELLOW) };
    },
    laneShifts: function () {
      var out = {};
      for (var k in state.laneShifts) if (state.laneShifts.hasOwnProperty(k)) out[k] = state.laneShifts[k];
      return out;
    },
    setLaneShift: setLaneShift,
    resetLaneShifts: resetLaneShifts,
    setPerf: function (value) {
      state.perf = !!value;
      var cb = $("perf-mode");
      if (cb) cb.checked = state.perf;
      applyPerfMode(true);
      savePrefs();
      return state.perf;
    },
    setFollow: setFollowOnServer,
    // debug snapshot: what is active + live node coordinates
    diag: function () {
      return {
        mode: state.mode,
        layout: state.layout,
        physics: (state.mode !== "3d") && (state.layout === "physics"),
        lanes: state.layout === "lanes"
      };
    },
    positions: function () {
      if (!network) return {};
      try { return network.getPositions(); } catch (e) { return {}; }
    },
    sync3d: sync3d,
    glScene: function () { try { return gl ? gl.scene() : null; } catch (e) { return null; } },
    dom3d: function () {
      var box = $("missing-3d");
      var pre = $("missing-3d-log");
      var st = $("gl-status");
      return {
        missingHidden: box ? box.hidden : null,
        log: pre ? pre.textContent : "",
        status: st ? st.textContent : ""
      };
    }
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();