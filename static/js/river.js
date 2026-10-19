/* GRAFF frontend - "river" mode: the session as a stream of time.
 *
 * The other two views are untouched:
 *   2D vis-network      -> lanes / hierarchy / physics
 *   3D 3d-force-graph   -> "neural brain"
 * This file is the third one: a DIRECT three.js scene (no 3d-force-graph), so
 * the layout can be computed instead of simulated and so sparks can be drawn
 * with additive blending.
 *
 * The stream has three computed axes:
 *   x = time      (event order, snaked into bands like a video timeline)
 *   y = branching (parallel calls of one step spread sideways)
 *   z = layers    (successive passes of the stream sit deeper)
 *
 * A slot index is fixed the moment a node is created, so a new event can only
 * append at the tail: a node that is already drawn NEVER moves.  Runs of
 * routine repeats collapse into one "xN" group node that can be expanded.
 *
 * Node movement on screen therefore always means time, never a force layout.
 */
window.GRAFFRiver = (function () {
  "use strict";

  // ---------------------------------------------------------------- //
  // palette: saturated cores on a near-black background
  // ---------------------------------------------------------------- //
  var PALETTE = {
    reason: "#a855f7",
    message: "#e2f3ff",
    tool_call: "#2b8cff",
    tool_result: "#00e5a0",
    file_edit: "#ffb020",
    error: "#ff2d55",
    user: "#d8b4fe",
    start: "#7dff5a",
    end: "#ffd60a",
    cold: "#3b2a7d"
  };
  var TYPE_ORDER = ["reason", "message", "tool_call", "tool_result", "file_edit", "error"];

  // how far an old node drifts from its own colour into the deep violet
  var COOL_MIX = 0.82;

  // ---------------------------------------------------------------- //
  // geometry of the stream (all positions are pure functions of the slot)
  // ---------------------------------------------------------------- //
  var BAND_CAP = 104;     // slots per band - FIXED, which keeps positions stable
  var STEP_X = 26;        // world units between two slots of a band
  var BAND_DEPTH = 210;   // each following band sits this much deeper (z)
  var BRANCH_STEP = 34;   // sideways (y) step per branching level
  var NODE_RADIUS = 7;    // core sphere radius in world units

  // ---------------------------------------------------------------- //
  // animation budget
  // ---------------------------------------------------------------- //
  var FRESH_MS = 9000;      // younger than this -> the halo keeps pulsing
  var FRESH_CAP = 400;      // at most this many nodes animated per frame
  var APPEAR_MS = 200;      // appearance: scale 0.6 -> 1.0
  var FLASH_MS = 700;       // arrival flash decay
  var ERROR_FLASH_MS = 2400;// errors hold their red flash much longer
  var SPARK_POOL = 48;      // live sparks, never thousands
  var SPARK_TRAIL = 5;      // trail samples per spark
  var SPARK_STAGGER_MS = 45;// a batch leaves one after another, not in one volley
  var SPARK_SPEED = 0.9;    // world units per millisecond
  var SPARK_MIN_MS = 300;
  var SPARK_MAX_MS = 600;
  var RECOLOUR_MS = 4000;   // cooling is a colour property, not a per-frame effect
  var IDLE_MS = 500;        // nothing animating: a slow tick, not a frozen canvas

  function rgb(hexColor) {
    var v = String(hexColor).replace("#", "");
    return {
      r: parseInt(v.substring(0, 2), 16),
      g: parseInt(v.substring(2, 4), 16),
      b: parseInt(v.substring(4, 6), 16)
    };
  }
  function toHex(c) {
    function two(n) {
      n = Math.max(0, Math.min(255, Math.round(n)));
      var s = n.toString(16);
      return s.length === 1 ? "0" + s : s;
    }
    return "#" + two(c.r) + two(c.g) + two(c.b);
  }
  function mixRgb(a, b, t) {
    if (t < 0) t = 0; else if (t > 1) t = 1;
    return {
      r: a.r + (b.r - a.r) * t,
      g: a.g + (b.g - a.g) * t,
      b: a.b + (b.b - a.b) * t
    };
  }

  // Colour of a node: its own saturated hue, cooling into the deep violet as
  // the event recedes in time.  Errors keep a red-leaning hue even when old,
  // because red is reserved for errors and must stay readable.
  function coolColor(type, frac, actor) {
    var base = rgb(actor === "user" ? PALETTE.user : (PALETTE[type] || PALETTE.message));
    var cold = rgb(PALETTE.cold);
    if (frac < 0) frac = 0; else if (frac > 1) frac = 1;
    var t = frac * COOL_MIX;
    if (type === "error") {
      var errCold = { r: 0x6a, g: 0x10, b: 0x33 };
      return toHex(mixRgb(base, errCold, t));
    }
    return toHex(mixRgb(base, cold, t));
  }

  // ---------------------------------------------------------------- //
  // packing: routine repeats become one group node with a "xN" counter
  // ---------------------------------------------------------------- //
  function runKey(item) {
    // Kept for callers that only need a stable key for a single item.
    var raw = item.source || {};
    if (item.group) return "tool_call|codex|";
    var name = (raw.meta && raw.meta.name) ? String(raw.meta.name) : "";
    return (raw.type || "message") + "|" + (raw.actor || "codex") + "|" + name;
  }

  /* Fold a tool_call and the tool_result that answers THAT call into one
   * "routine unit".  The rollout alternates call/result constantly, so without
   * this step a run of identical calls would never be adjacent and nothing
   * would collapse.  Purely local: item i only ever looks at item i+1. */
  function routineUnits(items) {
    var units = [];
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      var raw = item.source || {};
      if (item.group || raw.type === "tool_call") {
        var next = items[i + 1];
        var nextRaw = next && next.source ? next.source : {};
        var sameCall = !item.group && next && nextRaw.type === "tool_result" &&
          raw.meta && nextRaw.meta && raw.meta.call_id && raw.meta.call_id === nextRaw.meta.call_id;
        units.push({ items: sameCall ? [item, next] : [item], type: "tool_call", actor: raw.actor || "codex", name: (raw.meta && raw.meta.name) || "" });
        if (sameCall) i++;
      } else {
        units.push({ items: [item], type: raw.type || "message", actor: raw.actor || "codex", name: (raw.meta && raw.meta.name) || "" });
      }
    }
    return units;
  }

  function unitKey(unit) {
    return unit.type + "|" + unit.actor + "|" + unit.name;
  }

  /* Turn the ordered view items into river nodes.
   *
   * Grouping is a pure left-to-right function of the prefix, so replaying it on
   * a longer list can only ever append: the slot of an existing group is fixed
   * by the number of groups before it, and a growing run keeps its own slot. */
  function packRuns(items, collapse) {
    var nodes = [];
    var slot = 0;
    if (!collapse) {
      for (var q = 0; q < items.length; q++) nodes.push(makeRiverNode(items[q].id, [items[q]], "#" + q, slot++));
      return nodes;
    }
    var units = routineUnits(items);
    var current = null;
    for (var u = 0; u < units.length; u++) {
      var unit = units[u];
      var key = unitKey(unit);
      if (current && current.key === key) {
        for (var m = 0; m < unit.items.length; m++) current.members.push(unit.items[m]);
        current.count++;
        continue;
      }
      current = makeRiverNode(unit.items[0].id, unit.items.slice(), key, slot++);
      current.count = 1;
      nodes.push(current);
    }
    return nodes;
  }

  function makeRiverNode(id, members, key, slot) {
    var first = members[0];
    var src = first.source || {};
    var isGroupItem = !!first.group;
    return {
      id: "r:" + id,
      key: key,
      slot: slot,
      members: members,
      count: 1,
      type: isGroupItem ? "tool_call" : (src.type || "message"),
      actor: isGroupItem ? "codex" : (src.actor || "codex"),
      ts: src.ts || 0,
      tsLast: (members[members.length - 1].source || {}).ts || src.ts || 0,
      label: src.label || src.type || "",
      anchorId: src.id || id
    };
  }


  function rawIdsOf(riverNode) {
    var out = [];
    for (var i = 0; i < riverNode.members.length; i++) {
      var m = riverNode.members[i];
      if (m.source && m.source.id) out.push(m.source.id);
      if (m.group && m.group.result && m.group.result.id) out.push(m.group.result.id);
    }
    return out;
  }

  // A slot maps to a point of the snaked stream: bands run left to right, then
  // the next one starts on the right again (boustrophedon), so a long session
  // never becomes one endless line.
  function coordsFor(slot) {
    var band = Math.floor(slot / BAND_CAP);
    var col = slot % BAND_CAP;
    var right = (band % 2) === 0;
    return {
      x: (right ? col : (BAND_CAP - 1 - col)) * STEP_X,
      z: -band * BAND_DEPTH,
      band: band,
      col: col,
      right: right
    };
  }

  // Branches spread symmetrically and, crucially, by a function of the child
  // ordinal only: child #3 keeps its offset when child #4 appears.
  function branchOffset(k) {
    if (!k) return 0;
    var level = Math.ceil(k / 2);
    return (k % 2 === 1) ? level : -level;
  }

  function layout(items, collapse) {
    var nodes = packRuns(items, collapse);
    var byId = {};
    var byRaw = {};
    var i, j, k;
    for (i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      byId[n.id] = n;
      n.rawIds = rawIdsOf(n);
      for (j = 0; j < n.rawIds.length; j++) byRaw[n.rawIds[j]] = n;
      var c = coordsFor(n.slot);
      n.x = c.x;
      n.z = c.z;
      n.band = c.band;
      n.col = c.col;
      n.anchor = n.members[0];
      n.isGroup = n.count > 1;
      n.parentRawId = (n.members[0].source && n.members[0].source.parent) || null;
    }
    // branch ordinal among siblings, assigned in slot order (append-only)
    var seen = {};
    for (i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      node.parentRiver = node.parentRawId ? (byRaw[node.parentRawId] || null) : null;
      var ord = 0;
      if (node.parentRiver) {
        ord = seen[node.parentRiver.id] || 0;
        seen[node.parentRiver.id] = ord + 1;
      }
      node.ord = ord;
    }
    // y = parent y + branch offset (memoised, guard against odd data)
    function yOf(n) {
      if (n.y !== undefined) return n.y;
      if (n._walking) { n.y = 0; return 0; }
      n._walking = true;
      var py = n.parentRiver ? yOf(n.parentRiver) : 0;
      n.y = py + branchOffset(n.ord) * BRANCH_STEP;
      n._walking = false;
      return n.y;
    }
    for (i = 0; i < nodes.length; i++) yOf(nodes[i]);
    return { nodes: nodes, byId: byId, byRaw: byRaw };
  }

  var model = {
    palette: PALETTE,
    typeOrder: TYPE_ORDER,
    BAND_CAP: BAND_CAP,
    STEP_X: STEP_X,
    BAND_DEPTH: BAND_DEPTH,
    BRANCH_STEP: BRANCH_STEP,
    NODE_RADIUS: NODE_RADIUS,
    FRESH_MS: FRESH_MS,
    APPEAR_MS: APPEAR_MS,
    FLASH_MS: FLASH_MS,
    ERROR_FLASH_MS: ERROR_FLASH_MS,
    SPARK_POOL: SPARK_POOL,
    SPARK_SPEED: SPARK_SPEED,
    SPARK_MIN_MS: SPARK_MIN_MS,
    SPARK_MAX_MS: SPARK_MAX_MS,
    SPARK_STAGGER_MS: SPARK_STAGGER_MS,
    runKey: runKey,
    packRuns: packRuns,
    coordsFor: coordsFor,
    branchOffset: branchOffset,
    layout: layout,
    rawIdsOf: rawIdsOf,
    coolColor: coolColor,
    rgb: rgb,
    toHex: toHex,
    mixRgb: mixRgb,
    // spark duration in ms for an edge of a given length (long edge -> longer)
    sparkDuration: function (distance, speed) {
      var v = speed && speed > 0 ? speed : SPARK_SPEED;
      var ms = distance / v;
      if (ms < SPARK_MIN_MS) ms = SPARK_MIN_MS;
      if (ms > SPARK_MAX_MS) ms = SPARK_MAX_MS;
      return ms;
    }
  };

  // ---------------------------------------------------------------- //
  // scene: one instanced mesh for the cores, one points cloud for the
  // halos, one line-segments object for the edges, one points cloud for
  // the sparks.  No DOM per node anywhere.
  // ---------------------------------------------------------------- //
  var MAX_NODES = 6000;   // instance capacity; the real cap is applied by the app
  var MAX_LINKS = 12000;

  function glowTexture() {
    var size = 128;
    var canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    var ctx = canvas.getContext("2d");
    var g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    g.addColorStop(0.0, "rgba(255,255,255,1)");
    g.addColorStop(0.25, "rgba(255,255,255,0.55)");
    g.addColorStop(0.55, "rgba(255,255,255,0.16)");
    g.addColorStop(1.0, "rgba(255,255,255,0)");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, size, size);
    var tex = new THREE.CanvasTexture(canvas);
    tex.needsUpdate = true;
    return tex;
  }

  function create(host, opts) {
    opts = opts || {};
    var THREE_ = window.THREE;
    if (!THREE_) return null;

    var headless = !!opts.headless;
    var scene = new THREE_.Scene();
    scene.background = new THREE_.Color(0x04070c);

    var camera = new THREE_.PerspectiveCamera(55, 1, 1, 60000);
    camera.position.set(0, 0, 1600);

    var renderer = null;
    if (!headless) {
      try {
        renderer = new THREE_.WebGLRenderer({ antialias: !opts.perf, alpha: false });
        renderer.setClearColor(0x04070c, 1);
        if (host && renderer.domElement) host.appendChild(renderer.domElement);
      } catch (e) {
        renderer = null;
      }
    }

    // ---- core spheres (instanced) ---- //
    var coreGeo = new THREE_.SphereGeometry(1, 12, 10);
    var coreMat = new THREE_.MeshBasicMaterial({ vertexColors: true });
    var cores = new THREE_.InstancedMesh(coreGeo, coreMat, MAX_NODES);
    cores.instanceMatrix.setUsage(THREE_.DynamicDrawUsage);
    cores.count = 0;
    cores.frustumCulled = false;
    // allocate the colour buffer once (per-instance colour, one draw call)
    var white = new THREE_.Color(0xffffff);
    for (var ci = 0; ci < MAX_NODES; ci++) cores.setColorAt(ci, white);
    scene.add(cores);

    // ---- halos (one additive points cloud) ---- //
    var haloGeo = new THREE_.BufferGeometry();
    var haloPos = new Float32Array(MAX_NODES * 3);
    var haloCol = new Float32Array(MAX_NODES * 3);
    haloGeo.setAttribute("position", new THREE_.BufferAttribute(haloPos, 3));
    haloGeo.setAttribute("color", new THREE_.BufferAttribute(haloCol, 3));
    var haloMat = new THREE_.PointsMaterial({
      size: 44, sizeAttenuation: true, map: glowTexture(), vertexColors: true,
      blending: THREE_.AdditiveBlending, transparent: true, depthWrite: false, opacity: 0.9
    });
    var halos = new THREE_.Points(haloGeo, haloMat);
    halos.frustumCulled = false;
    scene.add(halos);

    // ---- links (one LineSegments, gradient parent -> child) ---- //
    var linkGeo = new THREE_.BufferGeometry();
    var linkPos = new Float32Array(MAX_LINKS * 6);
    var linkCol = new Float32Array(MAX_LINKS * 6);
    linkGeo.setAttribute("position", new THREE_.BufferAttribute(linkPos, 3));
    linkGeo.setAttribute("color", new THREE_.BufferAttribute(linkCol, 3));
    var linkMat = new THREE_.LineBasicMaterial({
      vertexColors: true, transparent: true, opacity: 0.5,
      blending: THREE_.AdditiveBlending, depthWrite: false
    });
    var linkLines = new THREE_.LineSegments(linkGeo, linkMat);
    linkLines.frustumCulled = false;
    scene.add(linkLines);

    // ---- sparks (one additive points pool) ---- //
    var SPARK_POINTS = SPARK_POOL * SPARK_TRAIL;
    var sparkGeo = new THREE_.BufferGeometry();
    var sparkPos = new Float32Array(SPARK_POINTS * 3);
    var sparkCol = new Float32Array(SPARK_POINTS * 3);
    sparkGeo.setAttribute("position", new THREE_.BufferAttribute(sparkPos, 3));
    sparkGeo.setAttribute("color", new THREE_.BufferAttribute(sparkCol, 3));
    var sparkMat = new THREE_.PointsMaterial({
      size: 26, sizeAttenuation: true, map: glowTexture(), vertexColors: true,
      blending: THREE_.AdditiveBlending, transparent: true, depthWrite: false, opacity: 1
    });
    var sparks = new THREE_.Points(sparkGeo, sparkMat);
    sparks.frustumCulled = false;
    scene.add(sparks);
    for (var si = 0; si < SPARK_POINTS; si++) {
      sparkPos[si * 3] = 1e7; sparkPos[si * 3 + 1] = 1e7; sparkPos[si * 3 + 2] = 1e7;
    }

    var sparkPool = [];
    for (var sp = 0; sp < SPARK_POOL; sp++) {
      sparkPool.push({ active: false, from: null, to: null, start: 0, dur: SPARK_MIN_MS });
    }

    // ------------------------------------------------------------ //
    // camera controls: orbit + wheel zoom + right-drag pan (own code,
    // the vendored three build ships no OrbitControls)
    // ------------------------------------------------------------ //
    var target = new THREE_.Vector3(0, 0, 0);
    var radius = 1600;
    var theta = 0;
    var phi = Math.PI / 2.4;
    var MIN_R = 40, MAX_R = 40000;
    var drag = null;
    var followTarget = null;   // node position the camera should drift to
    var userMoved = false;

    function applyCamera() {
      var sp = Math.sin(phi), cp = Math.cos(phi);
      camera.position.set(
        target.x + radius * sp * Math.sin(theta),
        target.y + radius * cp,
        target.z + radius * sp * Math.cos(theta)
      );
      camera.lookAt(target.x, target.y, target.z);
    }
    var controls = { target: target, get radius() { return radius; }, set radius(v) { radius = v; } };

    var un = null;
    if (host && host.addEventListener) {
      function onDown(ev) {
        var e = ev.touches ? ev.touches[0] : ev;
        drag = { x: e.clientX, y: e.clientY, button: (ev.button === 2 ? 2 : 0) };
        userMoved = true;
        followTarget = null;
      }
      function onMove(ev) {
        if (!drag) return;
        var e = ev.touches ? ev.touches[0] : ev;
        var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
        drag.x = e.clientX; drag.y = e.clientY;
        if (drag.button === 2) {
          var k = radius * 0.0016;
          target.x -= (dx * Math.cos(theta) - dy * Math.sin(theta)) * k;
          target.y += dy * k;
          target.z -= (-dx * Math.sin(theta) - dy * Math.cos(theta)) * k;
        } else {
          theta -= dx * 0.006;
          phi = Math.max(0.05, Math.min(Math.PI - 0.05, phi - dy * 0.006));
        }
        applyCamera();
      }
      function onUp() { drag = null; }
      function onWheel(ev) {
        var d = ev.deltaY || 0;
        radius = Math.max(MIN_R, Math.min(MAX_R, radius * (1 + (d > 0 ? 0.12 : -0.12))));
        userMoved = true;
        applyCamera();
        if (ev.preventDefault) ev.preventDefault();
      }
      host.addEventListener("mousedown", onDown);
      host.addEventListener("mousemove", onMove);
      host.addEventListener("mouseup", onUp);
      host.addEventListener("mouseleave", onUp);
      host.addEventListener("wheel", onWheel, { passive: false });
      if (host.addEventListener) {
        un = function () {
          host.removeEventListener("mousedown", onDown);
          host.removeEventListener("mousemove", onMove);
          host.removeEventListener("mouseup", onUp);
          host.removeEventListener("mouseleave", onUp);
          host.removeEventListener("wheel", onWheel);
        };
      }
    }

    // ------------------------------------------------------------ //
    // data: nodes + links, positions computed once per node and never
    // changed afterwards (a new event can only append at the tail)
    // ------------------------------------------------------------ //
    var state_linksVisible = true;
    var nodeList = [];      // river nodes, index == instance index
    var indexOfId = {};     // river id -> instance index
    var linkList = [];      // {from, to, fromIdx, toIdx, color, len}
    var sceneNodes = 0;
    var sceneLinks = 0;
    var oldestTs = 0;
    var newestTs = 0;
    var active = [];        // indices still animating (fresh / flashing)

    var WHITE_COLOR = new THREE_.Color(0xffffff);
    var hasMatrix = [];     // instance index -> its matrix was written at least once
    var tmpMatrix = new THREE_.Matrix4();
    var tmpQuat = new THREE_.Quaternion();
    var tmpVec = new THREE_.Vector3();
    var tmpScale = new THREE_.Vector3();
    var tmpColor = new THREE_.Color();

    // A node that is not being animated still has to be on screen: three's
    // InstancedMesh starts with an all-zero matrix, and a zero matrix draws
    // nothing at all.  One static write per node is enough, because a river
    // position never changes once the node exists.
    function writeSteady(i, now) {
      var n = nodeList[i];
      if (!n) return;
      if (!n.steady) n.steady = model.coolColor(n.type, 0, n.actor);
      tmpVec.set(n.x, n.y, n.z);
      tmpScale.set(NODE_RADIUS, NODE_RADIUS, NODE_RADIUS);
      tmpMatrix.compose(tmpVec, tmpQuat, tmpScale);
      cores.setMatrixAt(i, tmpMatrix);
      var col = tmpColor.set(n.steady);
      cores.setColorAt(i, col);
      haloPos[i * 3] = n.x; haloPos[i * 3 + 1] = n.y; haloPos[i * 3 + 2] = n.z;
      haloCol[i * 3] = col.r; haloCol[i * 3 + 1] = col.g; haloCol[i * 3 + 2] = col.b;
      hasMatrix[i] = true;
    }

    function ensureMatrices(now) {
      for (var i = 0; i < sceneNodes; i++) {
        if (!hasMatrix[i]) writeSteady(i, now);
      }
    }

    function setNodeAppearance(i, now) {
      var n = nodeList[i];
      var age = now - n.bornAt;
      // appearance: 0.6 -> 1.0 over APPEAR_MS, then a slow halo pulse while fresh
      var grow = age < APPEAR_MS ? (0.6 + 0.4 * (age / APPEAR_MS)) : 1;
      var pulse = 0;
      if (age < FRESH_MS) {
        var ph = (age - APPEAR_MS) / 700;
        if (ph > 0) pulse = 0.16 * Math.sin(ph) * (1 - age / FRESH_MS);
      }
      // arrival flash: bright right away, then cools to the steady colour;
      // errors hold the red flash much longer
      var flashMs = n.type === "error" ? ERROR_FLASH_MS : FLASH_MS;
      var flash = age < flashMs ? (1 - age / flashMs) : 0;
      var scale = NODE_RADIUS * (grow + pulse + flash * (n.type === "error" ? 0.5 : 0.28));
      tmpVec.set(n.x, n.y, n.z);
      tmpScale.set(scale, scale, scale);
      tmpMatrix.compose(tmpVec, tmpQuat, tmpScale);
      cores.setMatrixAt(i, tmpMatrix);

      hasMatrix[i] = true;
      var col = tmpColor.set(n.steady);
      if (flash > 0) {
        // flash towards white (cools back to the steady hue on its own)
        col.lerp(WHITE_COLOR, n.type === "error" ? 0.55 * flash : 0.42 * flash);
      }
      cores.setColorAt(i, col);

      haloPos[i * 3] = n.x; haloPos[i * 3 + 1] = n.y; haloPos[i * 3 + 2] = n.z;
      var hg = 1 + pulse * 3 + flash * 2.4;
      haloCol[i * 3] = col.r * hg; haloCol[i * 3 + 1] = col.g * hg; haloCol[i * 3 + 2] = col.b * hg;
    }

    // Colour of every node for a given "now": young nodes are saturated, old
    // ones slide into the deep violet.  This is a colour property, not a
    // per-frame effect, so it is recomputed on a slow timer.
    function recolour(now) {
      var span = (newestTs > oldestTs) ? (newestTs - oldestTs) : 0;
      for (var i = 0; i < sceneNodes; i++) {
        var n = nodeList[i];
        var frac = span > 0 ? ((now / 1000) - n.ts) / Math.max(1, span) : 0;
        if (frac < 0) frac = 0;
        n.steady = model.coolColor(n.type, frac * 0.9, n.actor);
      }
    }

    // Only fresh nodes are animated.  A cold load of a 4000-node session would
    // otherwise rewrite every instance for 9 seconds; the cap keeps the newest
    // FRESH_CAP and lets the rest sit at their steady colour immediately.
    function updateActive(now) {
      active = [];
      var from = Math.max(0, sceneNodes - FRESH_CAP);
      for (var i = from; i < sceneNodes; i++) {
        var n = nodeList[i];
        var flashMs = n.type === "error" ? ERROR_FLASH_MS : FLASH_MS;
        if (!n.seenAt || now - n.seenAt < APPEAR_MS + FRESH_MS || now - n.seenAt < flashMs + 50) {
          active.push(i);
        }
      }
    }

    // ------------------------------------------------------------ //
    // rebuild from the packed river model
    // ------------------------------------------------------------ //
    function setData(riverNodes, links, opts2) {
      opts2 = opts2 || {};
      var now = nowMs();
      var prevCount = nodeList.length;
      var added = [];

      // nodes: append-only.  A node that already exists keeps its index and
      // its computed coordinates, so nothing on screen moves.
      for (var i = 0; i < riverNodes.length; i++) {
        var rn = riverNodes[i];
        var idx = indexOfId[rn.id];
        if (idx === undefined) {
          if (nodeList.length >= MAX_NODES) break;
          idx = nodeList.length;
          indexOfId[rn.id] = idx;
          var n = {
            id: rn.id, index: idx, type: rn.type, actor: rn.actor,
            x: rn.x, y: rn.y, z: rn.z, slot: rn.slot, band: rn.band,
            ts: rn.ts, tsLast: rn.tsLast, count: rn.count,
            rn: rn, bornAt: now, seenAt: !opts2.silent ? now : 0,
            steady: rn.steady || model.coolColor(rn.type, 0, rn.actor)
          };
          nodeList.push(n);
          added.push(n);
        } else {
          nodeList[idx].count = rn.count;
          nodeList[idx].rn = rn;
        }
      }
      sceneNodes = nodeList.length;

      // links: connect the river nodes that correspond to the real parentage
      linkList = [];
      var seenLink = {};
      for (var l = 0; l < links.length; l++) {
        var fromIdx = indexOfId[links[l].from];
        var toIdx = indexOfId[links[l].to];
        if (fromIdx === undefined || toIdx === undefined || fromIdx === toIdx) continue;
        var key = fromIdx + ">" + toIdx;
        if (seenLink[key]) continue;
        seenLink[key] = true;
        var a = nodeList[fromIdx], b = nodeList[toIdx];
        var dist = Math.sqrt((a.x - b.x) * (a.x - b.x) + (a.y - b.y) * (a.y - b.y) + (a.z - b.z) * (a.z - b.z));
        linkList.push({
          from: fromIdx, to: toIdx, len: dist,
          colorFrom: a.steady, colorTo: b.steady
        });
      }
      sceneLinks = Math.min(linkList.length, MAX_LINKS);

      for (var t = 0; t < riverNodes.length; t++) {
        var tt = riverNodes[t].ts;
        if (!oldestTs || (tt && tt < oldestTs)) oldestTs = tt;
        if (tt > newestTs) newestTs = tt;
      }

      cores.count = sceneNodes;
      halos.geometry.setDrawRange(0, sceneNodes);
      halos.geometry.attributes.position.needsUpdate = true;
      halos.geometry.attributes.color.needsUpdate = true;

      recolour(now);
      var lpos = 0;
      for (var k = 0; k < sceneLinks; k++) {
        var lk = linkList[k];
        var p1 = nodeList[lk.from], p2 = nodeList[lk.to];
        linkPos[lpos * 3] = p1.x; linkPos[lpos * 3 + 1] = p1.y; linkPos[lpos * 3 + 2] = p1.z;
        linkPos[lpos * 3 + 3] = p2.x; linkPos[lpos * 3 + 4] = p2.y; linkPos[lpos * 3 + 5] = p2.z;
        lpos += 2;
      }
      linkLines.geometry.setDrawRange(0, sceneLinks * 2);
      linkGeo.attributes.position.needsUpdate = true;
      linkLines.visible = sceneLinks > 0 && !!state_linksVisible;

      var addedIndices = [];
      for (var a2 = 0; a2 < added.length; a2++) { addedIndices.push(added[a2].index); active.push(added[a2].index); }
      if (prevCount === 0) updateActive(now);
      else if (active.length > FRESH_CAP) active = active.slice(active.length - FRESH_CAP);

      // Data changed: there is now something to show, so the loop must come
      // back even if it had parked itself while the graph was still empty.
      pendingFrame = true;
      if (!document.hidden) start();

      return { added: added.length, indices: addedIndices, first: prevCount === 0 };
    }

    // ------------------------------------------------------------ //
    // sparks: a few dozen at most, staggered, and only between nodes
    // that are fresh and on screen
    // ------------------------------------------------------------ //
    var sparkQueue = [];
    var lastSparkAt = 0;

    function spawnSparks(indices) {
      if (!linkLines.visible) return 0;   // the links switch hides them too
      var now = nowMs();
      // one queued spark per node that just arrived on screen
      for (var i = 0; i < indices.length; i++) {
        var idx = indices[i];
        var n = nodeList[idx];
        if (!n) continue;
        var parent = null;
        for (var l = 0; l < linkList.length; l++) {
          if (linkList[l].to === idx) { parent = linkList[l]; break; }
        }
        if (!parent) continue;
        sparkQueue.push({ link: parent, at: now });
        if (sparkQueue.length > SPARK_POOL) sparkQueue.shift();
      }
    }

    function freeSpark(now) {
      for (var i = 0; i < sparkPool.length; i++) {
        if (!sparkPool[i].active) return sparkPool[i];
      }
      // pool exhausted: recycle the oldest one
      var oldest = sparkPool[0];
      for (var j = 1; j < sparkPool.length; j++) {
        if (sparkPool[j].start < oldest.start) oldest = sparkPool[j];
      }
      return oldest;
    }

    function launchSpark(item, now) {
      var link = item.link;
      var s = freeSpark(now);
      s.active = true;
      s.from = link.from;
      s.to = link.to;
      s.start = now;
      s.dur = model.sparkDuration(link.len, SPARK_SPEED);
      s.head = 0;
    }

    function updateSparks(now) {
      // drain the queue with a stagger so a burst leaves one after another
      var i;
      for (i = 0; i < sparkQueue.length; i++) {
        if (now - lastSparkAt >= SPARK_STAGGER_MS) {
          launchSpark(sparkQueue[i], now);
          lastSparkAt = now;
          sparkQueue.splice(i, 1);
          i--;
        } else break;
      }
      if (sparkQueue.length > SPARK_POOL * 4) sparkQueue = sparkQueue.slice(-SPARK_POOL * 2);

      var write = 0;
      for (i = 0; i < sparkPool.length; i++) {
        var s = sparkPool[i];
        if (!s.active) continue;
        var t = (now - s.start) / s.dur;
        if (t >= 1) {
          s.active = false;
          continue;
        }
        var a = nodeList[s.from], b = nodeList[s.to];
        if (!a || !b) { s.active = false; continue; }
        for (var k = 0; k < SPARK_TRAIL; k++) {
          var tt = t - k * 0.06;
          if (tt < 0) tt = 0;
          var px = a.x + (b.x - a.x) * tt;
          var py = a.y + (b.y - a.y) * tt;
          var pz = a.z + (b.z - a.z) * tt;
          var slot = (i * SPARK_TRAIL + k) * 3;
          sparkPos[slot] = px; sparkPos[slot + 1] = py; sparkPos[slot + 2] = pz;
          // the tail fades behind the head (additive -> scale means fade)
          var fade = (1 - k / SPARK_TRAIL) * (1 - t * 0.7);
          sparkCol[slot] = fade; sparkCol[slot + 1] = fade; sparkCol[slot + 2] = fade;
          write++;
        }
      }
      sparkGeo.attributes.position.needsUpdate = true;
      sparkGeo.attributes.color.needsUpdate = true;
    }

    function clearSparks() {
      sparkQueue = [];
      for (var i = 0; i < sparkPool.length; i++) sparkPool[i].active = false;
      for (var k = 0; k < SPARK_POINTS; k++) {
        sparkPos[k * 3] = 1e7; sparkPos[k * 3 + 1] = 1e7; sparkPos[k * 3 + 2] = 1e7;
      }
      sparkGeo.attributes.position.needsUpdate = true;
    }

    // ------------------------------------------------------------ //
    // camera helpers
    // ------------------------------------------------------------ //
    function bounds() {
      var box = { minX: 0, maxX: 0, minY: 0, maxY: 0, minZ: 0, maxZ: 0, count: sceneNodes };
      if (!sceneNodes) return box;
      var n = nodeList[0];
      box.minX = box.maxX = n.x; box.minY = box.maxY = n.y; box.minZ = box.maxZ = n.z;
      for (var i = 1; i < sceneNodes; i++) {
        var m = nodeList[i];
        if (m.x < box.minX) box.minX = m.x; if (m.x > box.maxX) box.maxX = m.x;
        if (m.y < box.minY) box.minY = m.y; if (m.y > box.maxY) box.maxY = m.y;
        if (m.z < box.minZ) box.minZ = m.z; if (m.z > box.maxZ) box.maxZ = m.z;
      }
      return box;
    }

    function fit(animate) {
      var box = bounds();
      if (!box.count) return;
      var cx = (box.minX + box.maxX) / 2;
      var cy = (box.minY + box.maxY) / 2;
      var cz = (box.minZ + box.maxZ) / 2;
      var w = Math.max(1, box.maxX - box.minX);
      var h = Math.max(1, box.maxY - box.minY);
      var d = Math.max(1, box.maxZ - box.minZ);
      var span = Math.max(w, h * 1.6, d * 1.6);
      var r = Math.max(180, span / (2 * Math.tan(Math.PI / 8)));
      followTarget = null;
      pendingFrame = true;
      if (!document.hidden) start();
      animateTo(cx, cy, cz, Math.min(r, MAX_R), animate);
    }

    var camAnim = null;
    function animateTo(x, y, z, r, animate) {
      if (!animate) {
        target.set(x, y, z); radius = r; applyCamera(); return;
      }
      camAnim = { t0: nowMs(), dur: 600, fromT: { x: target.x, y: target.y, z: target.z, r: radius }, toT: { x: x, y: y, z: z, r: r } };
    }

    function tickCamera(now) {
      if (!camAnim) return;
      var t = (now - camAnim.t0) / camAnim.dur;
      if (t >= 1) { t = 1; }
      var e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      target.set(
        camAnim.fromT.x + (camAnim.toT.x - camAnim.fromT.x) * e,
        camAnim.fromT.y + (camAnim.toT.y - camAnim.fromT.y) * e,
        camAnim.fromT.z + (camAnim.toT.z - camAnim.fromT.z) * e
      );
      radius = camAnim.fromT.r + (camAnim.toT.r - camAnim.fromT.r) * e;
      applyCamera();
      if (t >= 1) camAnim = null;
    }

    // "follow the newest": the camera moves, the nodes never do
    function followLatest(animate) {
      if (!sceneNodes) return;
      var n = nodeList[sceneNodes - 1];
      animateTo(n.x, n.y, n.z, Math.max(220, radius * 0.92), animate);
    }

    function focusNode(riverId, animate) {
      var idx = indexOfId[riverId];
      if (idx === undefined) return false;
      var n = nodeList[idx];
      animateTo(n.x, n.y, n.z, Math.max(150, Math.min(radius, 420)), animate !== false);
      return true;
    }

    function setPixelRatio(v) {
      if (!renderer) return;
      try { renderer.setPixelRatio(v); } catch (e) {}
    }

    function resize(w, h) {
      pendingFrame = true;
      if (!renderer) return;
      camera.aspect = (w || 1) / (h || 1);
      camera.updateProjectionMatrix();
      renderer.setSize(w, h, false);
    }

    // ------------------------------------------------------------ //
    // a step (turn) finishing: a short crest of light rolls along
    // that step's chain, then everything returns to calm
    // ------------------------------------------------------------ //
    var crests = [];
    var CREST_MS_PER_NODE = 55;
    var CREST_MAX_MS = 1500;

    function setLinksVisible(on) {
      state_linksVisible = !!on;
      linkLines.visible = !!on && sceneLinks > 0;
      pendingFrame = true;
      if (!document.hidden) start();
      return linkLines.visible;
    }

    function linksVisible() { return !!linkLines.visible; }

    // how many sparks are alive right now (diagnostics)
    function sparkStats() {
      var live = 0;
      for (var i = 0; i < sparkPool.length; i++) if (sparkPool[i].active) live++;
      return live;
    }

    function crest(indices) {
      if (!indices || !indices.length) return 0;
      var dur = Math.min(CREST_MAX_MS, Math.max(320, indices.length * CREST_MS_PER_NODE));
      crests.push({ indices: indices.slice(0, 400), start: nowMs(), dur: dur });
      if (crests.length > 4) crests.shift();
      pendingFrame = true;
      if (!document.hidden) start();
      return indices.length;
    }

    // ------------------------------------------------------------ //
    // render loop: runs only while the mode is visible and the tab is
    // shown; pauses when the layout has nothing left to animate
    // ------------------------------------------------------------ //
    var running = false;
    var rafId = null;
    var lastNow = 0;
    var lastRecolour = 0;
    var frameMs = 0;
    var drawCalls = 0;
    var triangles = 0;
    var animBudget = 0;      // ms the last animation pass took
    var loopDirty = true;
    var pendingFrame = true; // scene changed since the last drawn frame
    var frames = 0;          // frames actually drawn
    var nonEmptyFrames = 0;  // frames drawn while the scene had nodes
    var lastFrameAt = 0;
    var idle = false;         // nothing animating: the loop is on a slow tick

    function nowMs() {
      return (window.performance && window.performance.now) ? window.performance.now() : Date.now();
    }

    function renderOnce(now) {
      if (typeof now !== "number") now = nowMs();
      var t0 = nowMs();
      // A node that was never written would be invisible (zero matrix), so the
      // very first frame after a data change fills in every missing instance.
      ensureMatrices(now);
      // animation budget: only fresh nodes get per-frame work
      var i;
      for (i = 0; i < active.length; i++) {
        var idx = active[i];
        if (idx < sceneNodes) setNodeAppearance(idx, now);
      }
      // a crest rolls over its chain (cheap: a handful of nodes)
      for (i = 0; i < crests.length; i++) {
        var cr = crests[i];
        if (now - cr.start > cr.dur) continue;
        for (var k = 0; k < cr.indices.length; k++) {
          if (cr.indices[k] < sceneNodes) setNodeAppearance(cr.indices[k], now);
        }
      }
      if (now - lastRecolour > RECOLOUR_MS) { lastRecolour = now; recolour(now); }
      cores.instanceMatrix.needsUpdate = true;
      if (cores.instanceColor) cores.instanceColor.needsUpdate = true;
      halos.geometry.attributes.position.needsUpdate = true;
      halos.geometry.attributes.color.needsUpdate = true;
      linkGeo.attributes.position.needsUpdate = true;
      linkGeo.attributes.color.needsUpdate = true;
      sparkGeo.attributes.position.needsUpdate = true;
      sparkGeo.attributes.color.needsUpdate = true;
      animBudget = nowMs() - t0;
      pendingFrame = false;

      if (renderer) {
        renderer.render(scene, camera);
        try {
          var info = renderer.info.render;
          drawCalls = info.calls || 0;
          triangles = info.triangles || 0;
        } catch (e) {}
        frames++;
        if (sceneNodes > 0) nonEmptyFrames++;
        lastFrameAt = nowMs();
      }
      return !!renderer;
    }

    function pruneActive(now) {
      var fresh = [];
      for (var i = 0; i < active.length; i++) {
        var idx = active[i];
        if (idx >= sceneNodes) continue;
        var n = nodeList[idx];
        var flashMs = n.type === "error" ? ERROR_FLASH_MS : FLASH_MS;
        if (now - n.bornAt < Math.max(APPEAR_MS + FRESH_MS, flashMs + 100)) fresh.push(idx);
      }
      active = fresh;
      var live = [];
      for (var c = 0; c < crests.length; c++) if (now - crests[c].start <= crests[c].dur) live.push(crests[c]);
      crests = live;
      var liveSparks = 0;
      for (var s = 0; s < sparkPool.length; s++) if (sparkPool[s].active) liveSparks++;
      return { fresh: active.length, sparks: liveSparks, crests: crests.length };
    }

    function isPaused() {
      return document.hidden;
    }

    // Is there anything that really needs a 60 Hz frame right now?
    function hasWork(now) {
      if (loopDirty || pendingFrame || camAnim) return true;
      if (active.length) return true;
      for (var c = 0; c < crests.length; c++) if (now - crests[c].start <= crests[c].dur) return true;
      for (var s = 0; s < sparkPool.length; s++) if (sparkPool[s].active) return true;
      return false;
    }

    function loop() {
      rafId = requestAnimationFrame(loop);
      if (isPaused()) return;
      var now = nowMs();
      var dt = lastNow ? (now - lastNow) : 16;
      lastNow = now;
      var work = hasWork(now);
      // Nothing is animating: drop to a slow tick instead of stopping.  The
      // canvas stays correct, cooling keeps running and the mode stays "live",
      // but no frame budget is burnt on an idle scene.
      if (!work) {
        idle = true;
        if (now - lastFrameAt < IDLE_MS) return;
      } else {
        idle = false;
      }
      frameMs = frameMs ? (frameMs * 0.8 + dt * 0.2) : dt;
      tickCamera(now);
      updateSparks(now);
      renderOnce(now);
      pruneActive(now);
      loopDirty = false;
    }

    function start() {
      pendingFrame = true;
      if (running) return;
      running = true;
      lastNow = 0;
      loopDirty = true;
      if (rafId === null) rafId = requestAnimationFrame(loop);
    }

    // `stop()` is allowed to park the loop, but never on a canvas that does not
    // match the data: if a frame is still owed it is drawn right here, so
    // leaving the tab keeps a correct picture instead of a black one.  A hidden
    // tab draws nothing (it is frozen on purpose) and dispose() opts out.
    function stop(drawFinal) {
      running = false;
      if (rafId !== null) { cancelAnimationFrame(rafId); rafId = null; }
      if (drawFinal !== false && pendingFrame && !document.hidden) renderOnce(nowMs());
    }

    // Force exactly one frame.  Used by the diagnostics surface so a check can
    // prove from outside that the scene really paints.
    function renderNow() {
      var drew = renderOnce(nowMs());
      pendingFrame = false;
      return drew;
    }

    function wake(ms) {
      loopDirty = true;
      start();
      if (ms) {
        setTimeout(function () { loopDirty = true; start(); }, ms);
      }
      return true;
    }

    // ------------------------------------------------------------ //
    // picking: project the node positions and take the nearest one
    // (no DOM hit-testing, no per-node elements)
    // ------------------------------------------------------------ //
    var projVec = new THREE_.Vector3();
    function pick(clientX, clientY, w, h) {
      if (!sceneNodes) return null;
      var best = null, bestD = 24 * 24;
      for (var i = 0; i < sceneNodes; i++) {
        var n = nodeList[i];
        projVec.set(n.x, n.y, n.z);
        projVec.project(camera);
        var sx = (projVec.x * 0.5 + 0.5) * w;
        var sy = (-projVec.y * 0.5 + 0.5) * h;
        var d = (sx - clientX) * (sx - clientX) + (sy - clientY) * (sy - clientY);
        if (d < bestD) { bestD = d; best = n; }
      }
      return best ? { id: best.id, index: best.index, screen: true } : null;
    }

    function stats() {
      return {
        nodes: sceneNodes, links: sceneLinks,
        drawCalls: drawCalls, triangles: triangles,
        frameMs: Math.round(frameMs * 10) / 10,
        fps: frameMs > 0 ? Math.round(1000 / frameMs) : 0,
        running: running, animBudget: Math.round(animBudget * 100) / 100,
        // proof that the canvas is not black: frames really drawn, and whether
        // any of them was drawn with the graph populated
        frames: frames, nonEmptyFrames: nonEmptyFrames,
        drawn: frames > 0, painted: nonEmptyFrames > 0,
        pendingFrame: pendingFrame,
        idle: idle,
        idleMs: IDLE_MS,
        lastFrameAgo: lastFrameAt ? Math.round(nowMs() - lastFrameAt) : null,
        sparkLimit: SPARK_POOL, bandCap: BAND_CAP, maxNodes: MAX_NODES
      };
    }

    function positionOf(riverId) {
      var idx = indexOfId[riverId];
      if (idx === undefined) return null;
      var n = nodeList[idx];
      return { x: n.x, y: n.y, z: n.z, slot: n.slot, band: n.band, index: idx };
    }

    function positions() {
      var out = {};
      for (var i = 0; i < sceneNodes; i++) {
        var n = nodeList[i];
        out[n.id] = { x: n.x, y: n.y, z: n.z, slot: n.slot, band: n.band };
      }
      return out;
    }

    function dispose() {
      stop(false);
      clearSparks();
      if (un) { try { un(); } catch (e) {} un = null; }
      try {
        if (renderer && renderer.domElement && renderer.domElement.parentNode) {
          renderer.domElement.parentNode.removeChild(renderer.domElement);
        }
      } catch (e) {}
      try { cores.geometry.dispose(); coreMat.dispose(); } catch (e) {}
      try { haloGeo.dispose(); haloMat.dispose(); } catch (e) {}
      try { linkGeo.dispose(); linkMat.dispose(); } catch (e) {}
      try { sparkGeo.dispose(); sparkMat.dispose(); } catch (e) {}
      if (renderer && renderer.dispose) { try { renderer.dispose(); } catch (e) {} }
      renderer = null;
      nodeList = []; indexOfId = {}; linkList = []; sceneNodes = 0; sceneLinks = 0;
    }

    // ------------------------------------------------------------ //
    // public surface
    // ------------------------------------------------------------ //
    applyCamera();

    var api = {
      model: model,
      palette: PALETTE,
      scene: scene,
      camera: camera,
      renderer: function () { return renderer; },
      controls: controls,
      setData: setData,
      spawnSparks: spawnSparks,
      clearSparks: clearSparks,
      setLinksVisible: setLinksVisible,
      linksVisible: linksVisible,
      sparkStats: sparkStats,
      crest: crest,
      fit: fit,
      followLatest: followLatest,
      focusNode: focusNode,
      pick: pick,
      resize: resize,
      setPixelRatio: setPixelRatio,
      start: start,
      stop: stop,
      wake: wake,
      renderOnce: renderNow,
      ensureMatrices: ensureMatrices,
      writtenInstances: function () {
        var n = 0;
        for (var i = 0; i < sceneNodes; i++) if (hasMatrix[i]) n++;
        return n;
      },
      running: function () { return running && !document.hidden; },
      frames: function () { return frames; },
      dragActive: function () { return !!drag; },
      userMoved: function () { return userMoved; },
      recolour: recolour,
      stats: stats,
      positionOf: positionOf,
      positions: positions,
      bounds: bounds,
      dispose: dispose,
      nodes: function () { return nodeList; },
      links: function () { return linkList; }
    };
    return api;
  }

  // ---------------------------------------------------------------- //
  // module surface (the scene factory + the pure layout model)
  // ---------------------------------------------------------------- //
  return {
    palette: model.palette,
    typeOrder: model.typeOrder,
    constants: {
      BAND_CAP: BAND_CAP, STEP_X: STEP_X, BAND_DEPTH: BAND_DEPTH,
      BRANCH_STEP: BRANCH_STEP, NODE_RADIUS: NODE_RADIUS,
      SPARK_POOL: SPARK_POOL, SPARK_STAGGER_MS: SPARK_STAGGER_MS,
      FRESH_MS: FRESH_MS, APPEAR_MS: APPEAR_MS
    },
    layout: model.layout,
    packRuns: model.packRuns,
    coordsFor: model.coordsFor,
    branchOffset: model.branchOffset,
    runKey: model.runKey,
    coolColor: model.coolColor,
    rawIdsOf: model.rawIdsOf,
    sparkDuration: model.sparkDuration,
    glowTexture: glowTexture,
    create: create
  };
})();
