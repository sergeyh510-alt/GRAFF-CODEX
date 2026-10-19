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
    /* "cold" is not grey: an old node keeps its own hue, dimmed and pulled a
       little towards noble blue-violet, so a long session stays colourful
       instead of collapsing into a grey mat. */
    cold: "#5a3fa8"
  };
  var TYPE_ORDER = ["reason", "message", "tool_call", "tool_result", "file_edit", "error"];

  // how far an old node drifts from its own colour into the deep violet
  var COOL_MIX = 0.82;

  /* A NULL-PROTOTYPE id map.  An id such as "constructor" or "toString" must
     read back as "absent", never as an inherited Object.prototype member: with a
     plain {} such a key passes a `=== undefined` guard, the loop then indexes
     nodeList by a function and the undefined reaches rowOf() -- the exact
     "Cannot read properties of undefined (reading 'endOfStream')" crash this
     whole path exists to prevent. */
  function newIdMap() { return Object.create(null); }

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
  var FRESH_MS = 2600;      // younger than this -> appearance + halo pulse
  var FRESH_CAP = 240;      // at most this many nodes animated per frame
  var APPEAR_MS = 200;      // appearance: scale 0.6 -> 1.0
  var FLASH_MS = 700;       // arrival flash decay
  var ERROR_FLASH_MS = 2400;// errors hold their red flash much longer
  var SPARK_POOL = 24;      // live sparks, never thousands
  var SPARK_TRAIL = 5;      // trail samples per spark
  var SPARK_STAGGER_MS = 45;// a batch leaves one after another, not in one volley
  var SPARK_STAGGER_MAX = 6; // ... but never more than this many in ONE frame
  var SPARK_SPEED = 0.9;    // world units per millisecond
  var SPARK_MIN_MS = 300;
  var SPARK_MAX_MS = 600;
  var RECOLOUR_MS = 1200;   // cooling is batched: a few times a second at most
  var IDLE_MS = 2500;       // quiet this long -> the loop parks on the last frame
  var IDLE_TICK_MS = 1200;  // ... but while lingering, tick at most this often
  var UPLOAD_RING = 256;    // ring of recent upload bursts for the rate metric
  var LINK_PX = 3;          // link thickness in pixels (WebGL ignores linewidth)
  var HALO_GROW = 2.35;     // halo radius as a multiple of the core radius
  var HALO_OPACITY = 0.8;   // ... and how bright that second pass is
  var MAX_DPR = 1.25;       // a big scene must never burn fill rate on 4x DPR

  // ---------------------------------------------------------------- //
  // node colour: a saturated core, weighted by the type's own loudness.
  // Everything here is a table lookup - the frame loop may not allocate,
  // so it never builds a colour string and never mixes two rgb objects.
  // ---------------------------------------------------------------- //
  var TYPE_KEYS = ["reason", "message", "tool_call", "tool_result", "file_edit", "error", "user"];
  var TYPE_GAIN = [1.22, 1.0, 1.32, 1.3, 1.3, 1.7, 1.08];     // errors shout loudest
  /* How far each type recedes into the cold hue at the FAR end of the session.
     Never 1: grey is exactly the failure this fixes, so every type keeps a
     strong part of its own signal colour.  Messages (near white) recede a bit
     more, errors the least, so red still reads as "error" at any age. */
  var TYPE_COLD = [0.45, 0.58, 0.44, 0.42, 0.44, 0.3, 0.5];
  var COLD_MIN = 0.0;       // the far end: the table's own cold mix applies in full
  var COLD_DECAY = 1.0;     // linear in the session position (no early grey-out)
  var HALO_BASE = 0.42;     // halo brightness of a settled node
  var HALO_FRESH = 1.5;     // ... and of a node that has just arrived
  var HALO_CORE_SEG_W = 5;  // the halo is a soft blob, so a coarse sphere is plenty
  var HALO_CORE_SEG_H = 3;
  var RECOLOUR_BATCH = 600; // nodes whose colour is refreshed per recolour tick
  var UPLOAD_WINDOW_MS = 1000;  // rolling window of the re-uploads/second figure
  var SESSION_COOL_SPAN = 0.30;      // how much of the table the whole session may use

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
  function coordsFor(slot, depthAbs) {
    var band = Math.floor(slot / BAND_CAP);
    var col = slot % BAND_CAP;
    var right = (band % 2) === 0;
    // Two nodes of one depth never share a lane: the ordinal of the slot within
    // its (band, depth) pair shifts y, so a long chain fans into a shallow comb
    // and the links BETWEEN the lanes are plainly visible without any physics.
    var place = (slot * 7 + (depthAbs || 0)) * 5;
    var ordinal = right ? place % 17 : (place * 5 + 3) % 17;
    return {
      x: (right ? col : (BAND_CAP - 1 - col)) * STEP_X,
      y: (ordinal - 8) * 26,
      z: -band * BAND_DEPTH,
      band: band,
      col: col,
      right: right,
      ordinal: ordinal
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
    var byId = newIdMap();
    var byRaw = newIdMap();
    var i, j, k;
    for (i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      byId[n.id] = n;
      n.rawIds = rawIdsOf(n);
      for (j = 0; j < n.rawIds.length; j++) byRaw[n.rawIds[j]] = n;
      n.anchor = n.members[0];
      n.isGroup = n.count > 1;
      n.parentRawId = (n.members[0].source && n.members[0].source.parent) || null;
    }
    // A node's position is a pure function of (slot, depth), so the position of
    // a node is fixed the moment it arrives and NEVER changes, whatever arrives
    // later.  That is the invariant the whole view rests on.
    for (i = 0; i < nodes.length; i++) {
      var m = nodes[i];
      m.parentRiver = m.parentRawId ? (byRaw[m.parentRawId] || null) : null;
      var depth = 0;
      var walker = m, guard = 0;
      while (walker && walker.parentRiver && guard++ < 4000) { walker = walker.parentRiver; depth++; }
      var c = coordsFor(m.slot, depth);
      m.depth = depth;
      m.x = c.x;
      m.y = c.y;
      m.z = c.z;
      m.band = c.band;
      m.col = c.col;
      m.ordinal = c.ordinal;
    }
    // Sibling ordinal (kept for callers of the model; no longer drives y)
    var seen = newIdMap();
    for (i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var ord = 0;
      if (node.parentRiver) {
        ord = seen[node.parentRiver.id] || 0;
        seen[node.parentRiver.id] = ord + 1;
      }
      node.ord = ord;
    }
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

  // Dirty flags: an instanced buffer is re-uploaded ONLY when something it
  // holds actually changed.  Re-uploading everything every frame was the
  // single biggest cost of this scene.
  var DIRTY_MATRIX = 1, DIRTY_COLOR = 2, DIRTY_LINK = 4, DIRTY_LINKCOLOR = 8, DIRTY_SPARK = 16;


  // A soft radial falloff, white so the instance colour tints it.  It is the
  // map of the halo pass; it is created once and shared.
  function glowTexture() {
    var size = 128;
    var canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    var ctx = canvas.getContext("2d");
    var g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    g.addColorStop(0.0, "rgba(255,255,255,1)");
    g.addColorStop(0.18, "rgba(255,255,255,0.78)");
    g.addColorStop(0.45, "rgba(255,255,255,0.26)");
    g.addColorStop(1.0, "rgba(255,255,255,0)");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, size, size);
    var tex = new THREE.CanvasTexture(canvas);
    tex.needsUpdate = true;
    return tex;
  }

  // ---------------------------------------------------------------- //
  // Nodes: one unlit sphere geometry per surface, one shared texture.  The
  // scene must not depend on lights, so both materials are MeshBasicMaterial
  // (no normal matrix, no lighting chunk).
  // ---------------------------------------------------------------- //
  var CORE_SEG_W = 6, CORE_SEG_H = 4;   // 36 triangles per core, 6x fewer than 12x10
  // The halo is a soft blob with no lighting, so it can afford to be coarser
  // than the core.  A 5x3 sphere has 30 triangles instead of 36 and, because it
  // is drawn additively, the fewer overlapping shells the cleaner the bloom.
  function coreGeometry(THREE_) { return new THREE_.SphereGeometry(1, CORE_SEG_W, CORE_SEG_H); }

  function coreMaterial(THREE_, dpr) {
    var mat = new THREE_.MeshBasicMaterial({ vertexColors: true, toneMapped: false });
    mat.onBeforeCompile = function (shader) {
      // The core is a tiny sphere; a flat disc reads the same and costs
      // nothing.  Instance scaling happens in the vertex stage, after this.
      shader.vertexShader = "uniform float uShrink;\n" + shader.vertexShader.replace(
        "#include <begin_vertex>", "#include <begin_vertex>\n\ttransformed *= uShrink;");
      shader.uniforms.uShrink = { value: 0.62 };
    };
    return mat;
  }

  // The halo: the same sphere drawn a second time, bigger and additive.  The
  // vertex stage grows the instance; the fragment stage makes a HOT core bloom
  // harder than a settled one, by gating on the luminance of the instance
  // colour three already passes in as vColor (USE_INSTANCING_COLOR).  That gate
  // is per-instance and costs nothing: no extra attribute, no extra upload, no
  // per-node object.  It is also why the whole thing reads on a dark background:
  // an earlier version folded the "halo" factor into the core rgb and clamped it
  // to white, so the second pass could never appear at all.
  function haloMaterial(THREE_, dpr) {
    var mat = new THREE_.MeshBasicMaterial({
      color: 0xffffff, transparent: true, opacity: HALO_OPACITY, depthWrite: false,
      blending: THREE_.AdditiveBlending, toneMapped: false
    });
    mat.onBeforeCompile = function (shader) {
      shader.vertexShader = "uniform float uGrow;\n" + shader.vertexShader.replace(
        "#include <begin_vertex>", "#include <begin_vertex>\n\ttransformed *= uGrow;");
      shader.uniforms.uGrow = { value: HALO_GROW };
      // GUARDED on purpose.  vColor only EXISTS when three emitted USE_COLOR /
      // USE_COLOR_ALPHA; an unguarded reference is an undeclared identifier and
      // fails the whole program ("Shader Error 0 - VALIDATE_STATUS false"),
      // which is what left the canvas black while every counter looked sane.
      shader.fragmentShader = shader.fragmentShader.replace(
        "#include <color_fragment>",
        "#include <color_fragment>\n#if defined( USE_COLOR_ALPHA ) || defined( USE_COLOR )\n" +
        "\tfloat g = clamp(dot(vColor.rgb, vec3(0.6, 0.3, 0.1)) * 1.2, 0.45, 1.25);\n" +
        "\tdiffuseColor.a *= g;\n#endif");
    };
    return mat;
  }

  // ---------------------------------------------------------------- //
  // Links: a camera-facing quad per edge, two triangles, one shared
  // vertex-coloured geometry, therefore ONE draw call whatever the edge
  // count.  WebGL ignores linewidth, which is exactly why the old line
  // version was almost invisible.
  // ---------------------------------------------------------------- //
  var LINK_VERTS = 6;    // two triangles per edge

  function linkMaterial(THREE_, dpr) {
    // A custom shader: the ribbon is built in pixels of the screen, so it always
    // faces the camera and its thickness is a screen width, not a world size.
    // Every triangle pair shares this one program -> one draw call.
    //
    // The quad SPANS BOTH ENDPOINTS (corner.y mixes A -> B).  An earlier version
    // laid the rib on endpoint A only, which collapsed every edge to a zero-area
    // sliver: that is why no link was visible in the real renderer.
    return new THREE_.ShaderMaterial({
      uniforms: {
        uPx: { value: LINK_PX * (1 + (dpr - 1) * 0.25) },
        uOpacity: { value: 0.85 },
        resolution: { value: new THREE_.Vector2(1280, 720) }
      },
      vertexShader: [
        "#define LINK_STEM 1",
        // declared here on purpose: three only emits this for USE_COLOR, and a
        // ShaderMaterial with vertexColors:false never defines it
        "attribute vec3 color;",
        "attribute vec3 other;",
        "attribute vec2 corner;",
        "uniform float uPx;",
        "uniform vec2 resolution;",
        "varying vec3 vColor;",
        "void main() {",
        "  vec4 cA = projectionMatrix * modelViewMatrix * vec4(position, 1.0);",
        "  vec4 cB = projectionMatrix * modelViewMatrix * vec4(other, 1.0);",
        "  float wA = max(abs(cA.w), 1e-5);",
        "  float wB = max(abs(cB.w), 1e-5);",
        "  vec2 sA = cA.xy / wA;",
        "  vec2 sB = cB.xy / wB;",
        "  vec2 dirPx = (sB - sA) * resolution;",
        "  float lenPx = length(dirPx);",
        "  vec2 dPx = lenPx > 1e-4 ? dirPx / lenPx : vec2(1.0, 0.0);",
        "  vec2 nPx = vec2(-dPx.y, dPx.x);",
        "  vec2 mid = mix(sA, sB, corner.y);",
        "  vec2 midPx = mid * resolution;",
        "  float w = max(uPx, 1.0);",
        "  vec2 off = nPx * corner.x * w * 0.5;",
        "  vec2 ndc = (midPx + off) / max(resolution, vec2(1.0));",
        "  gl_Position = vec4(ndc, mix(cA.z, cB.z, corner.y), 1.0);",
        "  vColor = color;",
        "}"
      ].join("\n"),
      fragmentShader: [
        "uniform float uOpacity;",
        "varying vec3 vColor;",
        "void main() {",
        "  gl_FragColor = vec4(vColor, uOpacity);",
        "}"
      ].join("\n"),
      vertexColors: false, transparent: true, depthWrite: false,
      depthTest: true, blending: THREE_.AdditiveBlending, toneMapped: false
    });
  }

  function create(host, opts) {
    opts = opts || {};
    var THREE_ = window.THREE;
    if (!THREE_) return null;

    // A malformed record (a dangling edge, a node without an id) is COUNTED and
    // reported instead of throwing: one bad row may never blank the canvas.
    var warnHandler = (typeof opts.onWarn === "function") ? opts.onWarn : null;
    var warnings = 0, lastWarning = "";
    function warn(msg) {
      warnings++;
      lastWarning = msg;
      if (warnHandler) { try { warnHandler(msg); } catch (e) {} }
    }

    // ---- scene-wide handles, assigned further down ------------------------- //
    var linkLines = null;   // the ribbon mesh (one draw call for every edge)
    var linkGeo = null, linkPos = null, linkOther = null, linkCol = null;
    var linkDpr = 1;        // pixel ratio the ribbon width was calibrated for
    var writtenNodes = 0, writtenLinks = 0;
    var dangling = 0;       // links whose endpoint is not in the scene

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
        // Keep three's program checking ON (it is the only thing that turns a
        // silent "shader did not compile" into a programLog we can report).
        if (renderer.debug) renderer.debug.checkShaderErrors = true;
        // Preflight: an untouched GL canvas shows whatever the driver left in
        // it (usually black/transparent).  Clearing once right here means the
        // very first visible state is the scene background, never a blank slate.
        try { renderer.clear(); } catch (e) {}
        if (host && renderer.domElement) host.appendChild(renderer.domElement);
      } catch (e) {
        renderer = null;
      }
    }

    // ---- node cores: one instanced sphere with instanced colour ---------- //
    // The scene has no lights at all (every material is unlit), so nothing
    // depends on a normal matrix and nothing has to be recomputed per frame.
    var dpr = opts.perf === false ? Math.min(window.devicePixelRatio || 1, MAX_DPR) : 1;
    linkDpr = dpr;
    var coreGeo = coreGeometry(THREE_);
    var coreMat = coreMaterial(THREE_, dpr);
    var cores = new THREE_.InstancedMesh(coreGeo, coreMat, MAX_NODES);
    cores.instanceMatrix.setUsage(THREE_.DynamicDrawUsage);
    cores.count = 0;
    cores.frustumCulled = false;
    var white = new THREE_.Color(0xffffff);
    for (var ci = 0; ci < MAX_NODES; ci++) cores.setColorAt(ci, white);
    scene.add(cores);

    // ---- halos: the SAME instances, the SAME geometry and the SAME matrix
    // buffer, drawn a second time bigger and additive.  One extra draw call
    // for the whole scene - never one object (or sprite) per node. ---------- //
    var haloMat = haloMaterial(THREE_, dpr);
    var halos = new THREE_.InstancedMesh(coreGeo, haloMat, MAX_NODES);
    halos.instanceMatrix = cores.instanceMatrix;   // shared: uploaded once
    // Share the colour buffer too: the halo then glows in the NODE'S OWN hue
    // (an error blooms red, a message white) and, just as important, three
    // defines USE_INSTANCING_COLOR for this material, which is the define that
    // makes "varying vec3 vColor" exist for the fragment stage above.
    halos.instanceColor = cores.instanceColor;
    halos.count = 0;
    halos.frustumCulled = false;
    halos.renderOrder = 2;
    scene.add(halos);

    // ---- links: a camera-facing ribbon per edge, two triangles each, in one
    // shared vertex-coloured geometry -> ONE draw call at any edge count. --- //
    var linkCap = MAX_LINKS * LINK_VERTS;
    linkGeo = new THREE_.BufferGeometry();
    linkPos = new Float32Array(linkCap * 3);      // the near endpoint
    linkOther = new Float32Array(linkCap * 3);    // the far endpoint
    linkCol = new Float32Array(linkCap * 3);      // gradient parent -> child
    // (side, end): the quad spans endpoint A -> endpoint B, so corner.y mixes
    // the two projected endpoints and the ribbon is the whole edge.
    var linkCorner = new Float32Array(linkCap * 2);
    linkGeo.setAttribute("position", new THREE_.BufferAttribute(linkPos, 3));
    linkGeo.setAttribute("other", new THREE_.BufferAttribute(linkOther, 3));
    linkGeo.setAttribute("color", new THREE_.BufferAttribute(linkCol, 3));
    linkGeo.setAttribute("corner", new THREE_.BufferAttribute(linkCorner, 2));
    (function () {
      var c = [-1, 0, 1, 0, 1, 1, -1, 0, 1, 1, -1, 1];   // the two triangles
      for (var e = 0; e < MAX_LINKS; e++) {
        for (var v = 0; v < LINK_VERTS; v++) {
          linkCorner[(e * LINK_VERTS + v) * 2] = c[v * 2];
          linkCorner[(e * LINK_VERTS + v) * 2 + 1] = c[v * 2 + 1];
        }
      }
    })();
    var linkMat = linkMaterial(THREE_, dpr);
    linkLines = new THREE_.Mesh(linkGeo, linkMat);
    linkLines.frustumCulled = false;
    linkLines.renderOrder = 1;
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
        wake();          // a parked loop would otherwise ignore the user
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
        wake();
      }
      function onUp() { drag = null; }
      function onWheel(ev) {
        var d = ev.deltaY || 0;
        radius = Math.max(MIN_R, Math.min(MAX_R, radius * (1 + (d > 0 ? 0.12 : -0.12))));
        userMoved = true;
        applyCamera();
        wake();
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
    var indexOfId = newIdMap();     // river id -> instance index
    var linkList = [];      // {from, to, fromIdx, toIdx, color, len}
    var sceneNodes = 0;
    var sceneLinks = 0;
    var oldestTs = 0;
    var newestTs = 0;
    var active = [];        // indices still animating (fresh / flashing)

    // ---- per-node colour: allocation-free ---------------------------------- //
    // The colour of a node only depends on (type, actor, age-in-session), so a
    // 128-bucket table per type is computed ONCE.  Every node then holds a
    // bucket plus an appearance factor, and the render loop is pure arithmetic:
    // no new THREE.Color, no rgb object, no hex string, ever.
    var COLOUR_BUCKETS = 128;
    var colourTable = null;      // built once in create(); the row per type
    var typeSlot = null;         // type key -> row in the table
    var rowReason = 0, rowError = 0, rowStart = 0, rowEnd = 0, rowUser = 0;

    // Idempotent: the rows are pure functions of the palette, so one build
    // serves every scene of the page lifetime.
    function buildColourTable() {
      if (colourTable) return;
      colourTable = [];
      typeSlot = Object.create(null);
      rowReason = 0;
      for (var t = 0; t < TYPE_KEYS.length; t++) {
        var key = TYPE_KEYS[t];
        typeSlot[key] = t;
        var base = rgb(key === "user" ? PALETTE.user : (PALETTE[key] || PALETTE.message));
        var cold = rgb(PALETTE.cold);
        var gain = TYPE_GAIN[t], coldKeep = TYPE_COLD[t];
        if (key === "error") cold = { r: 0x6a, g: 0x10, b: 0x33 };
        var arr = new Float32Array(COLOUR_BUCKETS * 3);
        for (var b = 0; b < COLOUR_BUCKETS; b++) {
          var f = b / (COLOUR_BUCKETS - 1);
          var k = COLD_MIN + (1 - COLD_MIN) * Math.pow(f, COLD_DECAY);
          k *= coldKeep;
          arr[b * 3] = Math.min(255, (base.r + (cold.r - base.r) * k) * gain) / 255;
          arr[b * 3 + 1] = Math.min(255, (base.g + (cold.g - base.g) * k) * gain) / 255;
          arr[b * 3 + 2] = Math.min(255, (base.b + (cold.b - base.b) * k) * gain) / 255;
        }
        colourTable.push(arr);
      }
      rowError = typeSlot.error;
      rowStart = -2;
      rowEnd = -3;
      rowUser = typeSlot.user;
      // The stream's own ends are not a type but a role: they get their own
      // rows so the palette is honoured without a per-node string.
      colourTable.push(rowFor("start", PALETTE.start));
      rowStart = colourTable.length - 1;
      colourTable.push(rowFor("end", PALETTE.end));
      rowEnd = colourTable.length - 1;
      // the group rows: the same hues, pulled towards the cold violet so a
      // folded run reads as "quieter", not as "grey"
      var coldRgb = rgb(PALETTE.cold);
      mutedBase = colourTable.length;
      for (var g = 0; g < TYPE_KEYS.length; g++) {
        var gsrc = colourTable[g];
        var garr = new Float32Array(COLOUR_BUCKETS * 3);
        for (var gb = 0; gb < COLOUR_BUCKETS * 3; gb += 3) {
          garr[gb] = gsrc[gb] * 0.62 + coldRgb.r / 255 * 0.38;
          garr[gb + 1] = gsrc[gb + 1] * 0.62 + coldRgb.g / 255 * 0.38;
          garr[gb + 2] = gsrc[gb + 2] * 0.62 + coldRgb.b / 255 * 0.38;
        }
        colourTable.push(garr);
      }
    }

    // A row for the "start of the stream" / "end of the stream" roles: they
    // never cool, they are a landmark, not an event in the middle of a run.
    function rowFor(role, hex) {
      var base = rgb(hex);
      var arr = new Float32Array(COLOUR_BUCKETS * 3);
      for (var b = 0; b < COLOUR_BUCKETS; b++) {
        arr[b * 3] = Math.min(255, base.r * 1.2) / 255;
        arr[b * 3 + 1] = Math.min(255, base.g * 1.2) / 255;
        arr[b * 3 + 2] = Math.min(255, base.b * 1.2) / 255;
      }
      return arr;
    }

    function bucketOf(frac) {
      var b = (frac * (COLOUR_BUCKETS - 1)) | 0;
      if (b < 0) b = 0; else if (b > COLOUR_BUCKETS - 1) b = COLOUR_BUCKETS - 1;
      return b;
    }

    // A group node (a folded "xN" run) is a *quieter* version of its own kind,
    // never a grey one: the same hue, mixed 25% towards the deep violet.  One
    // row per type, built once, so the frame loop still only indexes a table.
    var mutedBase = -1;        // first row of the muted (group) twin table

    // The row a node reads: the first node of the stream is the green landmark,
    // the last one the yellow one; everything else reads its own type.
    function rowOf(n) {
      // Defensive: a node this pass cannot resolve is not allowed to throw.
      // Without this guard an undefined here is exactly the crash that reported
      // "Cannot read properties of undefined (reading 'endOfStream')".
      if (!n || typeof n !== "object") return typeSlot.tool_call === undefined ? 0 : typeSlot.tool_call;
      if (n.endOfStream) return rowEnd;
      if (n.startOfStream) return rowStart;
      var row = typeSlot[n.type];
      if (row === undefined) row = typeSlot[n.actor === "user" ? "user" : "tool_call"];
      // a folded "xN" run is the muted but still colourful twin of its type
      if (n.count > 1 && mutedBase >= 0) return mutedBase + row;
      return row;
    }

    // Built here, after the declarations: the frame loop only ever indexes the
    // table, so colour work is arithmetic instead of strings and allocation.
    buildColourTable();

    // ---- per-node appearance: bucket + gain, no allocation ----------------- //

    // Which buffers the current pass by writeNode() is allowed to re-upload.
    // Steady/prepare writes set DIRTY_MATRIX | DIRTY_COLOR; the idle &&!fresh
    // pass only sets DIRTY_MATRIX, so an already-settled instance colour buffer
    // is never pushed to the GPU again.
    // No extra scene objects: the whole picture is the instanced cores, the
    // additive halo pass and the ribbon mesh.  Everything else (stems, end
    // markers) used to be separate objects and only added draw calls.

    var passDirty = DIRTY_MATRIX | DIRTY_COLOR;   // what writeNode() may dirty
    // The span of instances a pass actually touched.  three uploads only
    // [offset, count) of a buffer, so an animated frame pushes the few hundred
    // instances that moved instead of the whole 6000-instance capacity.
    var dirtyMin = -1, dirtyMax = -1;
    function touch(i) {
      if (dirtyMin < 0 || i < dirtyMin) dirtyMin = i;
      if (i > dirtyMax) dirtyMax = i;
    }


    var hasMatrix = [];     // instance index -> its matrix was written at least once
    var tmpMatrix = new THREE_.Matrix4();
    var tmpQuat = new THREE_.Quaternion();
    var tmpVec = new THREE_.Vector3();
    var tmpScale = new THREE_.Vector3();
    var tmpColor = new THREE_.Color();
    var renderDirty = 0;    // which instanced buffers must be re-uploaded

    // One appearance pass for a single node, used by BOTH the steady write and
    // every animated frame, so a node never flickers between two code paths.
    function writeNode(i, now, animated) {
      var n = nodeList[i];
      if (!n) return;
      var row = colourTable[rowOf(n)];
      var b3 = n.bucket * 3;
      var cr = row[b3], cg = row[b3 + 1], cb = row[b3 + 2];
      var scale = NODE_RADIUS;
      var halo = HALO_BASE;
      if (animated) {
        var age = now - n.bornAt;
        var grow = age < APPEAR_MS ? 0.6 + 0.4 * (age / APPEAR_MS) : 1;
        var flashMs = n.type === "error" ? ERROR_FLASH_MS : FLASH_MS;
        var flash = age < flashMs ? (1 - age / flashMs) : 0;
        var pulse = 0;
        if (age < FRESH_MS) {
          var ph = (age - APPEAR_MS) / 700;
          if (ph > 0) pulse = 0.16 * Math.sin(ph) * (1 - age / FRESH_MS);
        }
        scale = NODE_RADIUS * (grow + pulse + flash * (n.type === "error" ? 0.5 : 0.28));
        halo = HALO_BASE + pulse * 2.2 + flash * (n.type === "error" ? 1.6 : 1.2);
        if (flash > 0) {
          // a short white-hot arrival spark, fading back into the node's hue
          var w = n.type === "error" ? 0.55 * flash : 0.42 * flash;
          cr += (1 - cr) * w; cg += (1 - cg) * w; cb += (1 - cb) * w;
        }
      }
      tmpVec.set(n.x, n.y, n.z);
      tmpScale.set(scale, scale, scale);
      tmpMatrix.compose(tmpVec, tmpQuat, tmpScale);
      cores.setMatrixAt(i, tmpMatrix);
      cores.setColorAt(i, tmpColor.setRGB(cr, cg, cb));
      // the halo pass shares this very matrix and this very colour buffer: the
      // same sphere, drawn a second time bigger and additive for one extra draw
      // call.  (A hot white core therefore also makes the brightest halo, which
      // is exactly the "a fresh event glows" reading we want.)
      hasMatrix[i] = true;
      touch(i);
      renderDirty |= passDirty;
    }

    // The "cold" write: position + colour, used the first time a node is drawn.
    // It runs once per node, during setData, never inside the frame loop.
    function writeSteady(i, now) {
      ensureBucket(i);
      writtenNodes++;
      renderDirty |= DIRTY_MATRIX | DIRTY_COLOR;
      writeNode(i, now, false);
    }

    function ensureMatrices(now) {
      for (var i = 0; i < sceneNodes; i++) {
        if (!hasMatrix[i]) writeSteady(i, now);
      }
    }

    function ensureBucket(i) {
      var n = nodeList[i];
      if (!n) return;
      n.bucket = ageFrac(n);
    }

    // Everything is animated through the same pass, so an animated node and a
    // settled one can never disagree about their colour.
    function setNodeAppearance(i, now) {
      writeNode(i, now, true);
    }

    // Colour of every node for a given "now": young nodes are saturated, old
    // ones slide into the deep violet.  This is a colour property, not a
    // per-frame effect, so it is recomputed on a slow timer and the scene is
    // re-uploaded once - not every frame.
    // Batched: at most a few hundred colours are recomputed in one tick.  The
    // cursor walks the whole scene over several seconds, so a cold load cools
    // gradually and a frame never pays for the whole graph.
    var recolourCursor = 0;
    function recolour(now) {
      var budget = RECOLOUR_BATCH;
      var changed = 0;
      while (budget > 0 && sceneNodes > 0) {
        if (recolourCursor >= sceneNodes) recolourCursor = 0;
        var n = nodeList[recolourCursor];
        var next = ageFrac(n);
        if (next !== n.bucket) { n.bucket = next; writeNode(recolourCursor, now, false); changed++; }
        recolourCursor++;
        budget--;
      }
      return changed;
    }

    /* How far along the SESSION a node sits: 0 = the very first event, 1 = the
       newest.  It is a pure function of the timestamps, so it is fixed the
       moment the event arrives and it NEVER reads the wall clock.  Measured
       against the wall clock an old session - the usual case - was uniformly
       "cold" and every node came out the same grey; anchored to the session it
       is colourful however long ago it happened. */
    function sessionFrac(ts) {
      var span = newestTs - oldestTs;
      if (span <= 0) return 0.42;                   // a single event: near the middle
      var frac = (ts - oldestTs) / span;
      if (frac < 0) return 0;
      if (frac > 1) return 1;
      return frac;
    }

    /* The cooling AGE, read against the session and anchored at the NEWEST
       event: 0 = just happened (its own type colour, at full strength),
       1 = the oldest event of the session (the deep blue-violet).  So an ember
       cools exactly as time passes, and the reference point is the freshest
       event - never the wall clock.  The span is capped so even the oldest node
       keeps most of its own hue instead of collapsing into grey. */
    function coolAge(ts) {
      return (1 - sessionFrac(ts)) * (1 - SESSION_COOL_SPAN);
    }

    // The colour bucket of a node, recomputed only when a NEW event extends the
    // session window and shifts everything slightly towards cool.
    function ageFrac(n) { return ageFracOf(n.ts); }

    // The same bucket for a bare timestamp, so the append path (which holds a
    // river node, not a scene node, and therefore has no n.bucket field yet)
    // computes exactly what the recolour pass will compute later.
    function ageFracOf(ts) {
      return bucketOf(coolAge(ts));
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
      try {
        return setDataInner(riverNodes, links, opts2);
      } catch (e) {
        // The scene must survive a record nobody anticipated.  The previous,
        // correct frame stays on screen; the failure is named in the journal.
        warn("русло: сборка сцены пропущена: " + (e && e.message ? e.message : String(e)));
        return { added: 0, indices: [], first: false, error: String(e && e.message ? e.message : e) };
      }
    }

    function setDataInner(riverNodes, links, opts2) {
      opts2 = opts2 || {};
      var now = nowMs();
      var prevCount = nodeList.length;
      var added = [];

      // nodes: append-only.  A node that already exists keeps its index and
      // its computed coordinates, so nothing on screen moves.
      if (!riverNodes || !riverNodes.length) riverNodes = [];
      if (!links || !links.length) links = [];
      var skippedNodes = 0;
      for (var i = 0; i < riverNodes.length; i++) {
        var rn = riverNodes[i];
        if (!rn || rn.id === undefined || rn.id === null) { skippedNodes++; continue; }
        var idx = indexOfId[rn.id];
        if (idx === undefined) {
          if (nodeList.length >= MAX_NODES) break;
          idx = nodeList.length;
          indexOfId[rn.id] = idx;
          var n = {
            id: rn.id, index: idx, type: rn.type, actor: rn.actor,
            x: rn.x || 0, y: rn.y || 0, z: rn.z || 0, slot: rn.slot || 0, band: rn.band || 0,
            ts: rn.ts, tsLast: rn.tsLast, count: rn.count,
            rn: rn, bornAt: now, seenAt: !opts2.silent ? now : 0,
            bucket: ageFracOf(rn.ts)
          };
          nodeList.push(n);
          added.push(n);
        } else {
          nodeList[idx].count = rn.count;
          nodeList[idx].rn = rn;
        }
      }
      sceneNodes = nodeList.length;

      // links: connect the river nodes that correspond to the real parentage.
      // A ribbon needs both endpoints at both of its vertices, so the far
      // endpoint and the corner are stamped into the shared geometry once here
      // and are never touched again.
      linkList = [];
      var seenLink = newIdMap();
      dangling = 0;
      for (var l = 0; l < links.length; l++) {
        var lk0 = links[l];
        // A dangling edge (a rolled-up group, a service node, or an event that
        // the cap pushed out) has no scene node: it is COUNTED and skipped.
        // `undefined` is what an absent id looks like; a non-numeric index is
        // treated the same way.  This is the guard for the crash that reported
        // "Cannot read properties of undefined".
        var fromIdx = (lk0 && typeof lk0.from === "string") ? indexOfId[lk0.from] : undefined;
        var toIdx = (lk0 && typeof lk0.to === "string") ? indexOfId[lk0.to] : undefined;
        if (fromIdx === undefined || toIdx === undefined) { dangling++; continue; }
        if (fromIdx === toIdx) continue;
        var key = fromIdx + ">" + toIdx;
        if (seenLink[key]) continue;
        seenLink[key] = true;
        var a = nodeList[fromIdx], b = nodeList[toIdx];
        if (!a || !b) { dangling++; continue; }
        var dist = Math.sqrt((a.x - b.x) * (a.x - b.x) + (a.y - b.y) * (a.y - b.y) + (a.z - b.z) * (a.z - b.z));
        // Trim the end radii so the ribbon starts at the rim of the cause and
        // stops at the rim of the effect instead of hiding inside the spheres.
        var trim = dist > 1e-3 ? Math.min(0.25, (NODE_RADIUS * 0.62) / dist) : 0;
        // The parent is normally the lower index in this append-only model;
        // "a" and "z" are kept ordered so a data set whose edges point the
        // other way still costs nothing extra.
        var a = fromIdx <= toIdx ? fromIdx : toIdx;
        var z = fromIdx <= toIdx ? toIdx : fromIdx;
        linkList.push({ from: fromIdx, to: toIdx, a: a, z: z, len: dist, a0: trim, a1: 1 - trim });
      }
      sceneLinks = Math.min(linkList.length, MAX_LINKS);
      // Report the data problems that used to be invisible: a broken record is
      // now a line in the journal and a number in the diagnostics panel.
      if (dangling > 0) warn("русло: пропущено висячих связей — " + dangling);
      if (skippedNodes > 0) warn("русло: пропущено записей без id — " + skippedNodes);

      for (var t = 0; t < riverNodes.length; t++) {
        var tt = riverNodes[t].ts;
        if (!oldestTs || (tt && tt < oldestTs)) oldestTs = tt;
        if (tt > newestTs) newestTs = tt;
      }

      cores.count = sceneNodes;
      halos.count = sceneNodes;
      linkLines.visible = sceneLinks > 0 && !!state_linksVisible;

      // The link geometry is rebuilt only when the edge set changed, not on
      // every data update: a ribbon never needs its endpoints refreshed
      // because a river node never moves.
      if (sceneNodes !== writtenNodes) rebuildNodeColours(now);
      if (sceneLinks !== writtenLinks) rebuildLinks();

      var addedIndices = [];
      for (var a2 = 0; a2 < added.length; a2++) { addedIndices.push(added[a2].index); active.push(added[a2].index); }
      if (prevCount === 0) updateActive(now);
      else if (active.length > FRESH_CAP) active = active.slice(active.length - FRESH_CAP);

      // Data changed: there is now something to show, so the loop must come
      // back even if it had parked itself while the graph was still empty.
      coolSettled = false; coolQuiet = 0;
      pendingFrame = true;
      if (!document.hidden) start();

      return { added: added.length, indices: addedIndices, first: prevCount === 0 };
    }

    // Fill the ribbon geometry of every visible edge.  Called only when the
    // edge set itself changed: a river node never moves, so the endpoints of an
    // already drawn link never change either.
    function rebuildLinks() {
      var n = sceneLinks;
      for (var k = 0; k < n; k++) {
        var lk = linkList[k];
        var p1 = nodeList[lk.from], p2 = nodeList[lk.to];
        var ca = colourTable[rowOf(p1)], cb = colourTable[rowOf(p2)];
        var ba = p1.bucket * 3, bb = p2.bucket * 3;
        var t0 = lk.a0 || 0, t1 = lk.a1 === undefined ? 1 : lk.a1;
        var ax = p1.x + (p2.x - p1.x) * t0;
        var ay = p1.y + (p2.y - p1.y) * t0;
        var az = p1.z + (p2.z - p1.z) * t0;
        var bx = p2.x - (p2.x - p1.x) * (1 - t1);
        var by = p2.y - (p2.y - p1.y) * (1 - t1);
        var bz = p2.z - (p2.z - p1.z) * (1 - t1);
        for (var v = 0; v < LINK_VERTS; v++) {
          var o = (k * LINK_VERTS + v);
          // endpoint A lives in `position`; endpoint B in `other`, so the quad
          // spans the WHOLE edge in the vertex shader
          linkPos[o * 3] = ax; linkPos[o * 3 + 1] = ay; linkPos[o * 3 + 2] = az;
          linkOther[o * 3] = bx; linkOther[o * 3 + 1] = by; linkOther[o * 3 + 2] = bz;
          // the rib at the far end belongs to the child's colour: the ribbon
          // then reads as a gradient from cause to effect
          var near = (v === 1 || v === 3 || v === 4) ? 1 : 0;
          linkCol[o * 3] = near ? ca[ba] : cb[bb];
          linkCol[o * 3 + 1] = near ? ca[ba + 1] : cb[bb + 1];
          linkCol[o * 3 + 2] = near ? ca[ba + 2] : cb[bb + 2];
        }
      }
      linkGeo.setDrawRange(0, n * LINK_VERTS);
      renderDirty |= DIRTY_LINK | DIRTY_LINKCOLOR;
      writtenLinks = n;
      uploadedLinkGeometry++;
    }

    // One write per node for the whole scene: the instance matrices, the
    // instance colours and the links.  This is the "cold" pass a fresh session
    // pays once; after it, only the nodes that really change are touched.
    function rebuildNodeColours(now) {
      var i;
      for (i = 0; i < sceneNodes; i++) {
        var n = nodeList[i];
        // Every node keeps a strong part of its OWN hue: the cold cap is far from
        // grey on purpose, so a long session is a colourful gradient, not a mat.
        n.bucket = ageFrac(n);
        n.startOfStream = (i === 0);
        n.endOfStream = (i === sceneNodes - 1);
      }
      // the landmarks (green start / yellow end) are rows that never cool, so
      // the bucket is irrelevant for them; the freshest plain node is a bucket 0
      for (i = 0; i < sceneNodes; i++) writeNode(i, now, false);
      writtenNodes = sceneNodes;
    }

    // ------------------------------------------------------------ //
    // self-check: render one frame and read the pixels BACK.  Counters and
    // draw calls can all look healthy while the canvas is a grey smear, so the
    // only honest proof of "the picture is right" is the picture itself.
    // ------------------------------------------------------------ //
    var CHECK_W = 160, CHECK_H = 96;
    var checkTarget = null, checkPixels = null;
    var checkBuf = new Uint8Array(CHECK_W * CHECK_H * 4);
    var windingScene = null, windingTarget = null, windingBuf = new Uint8Array(64 * 16 * 4);
    var linkWindingOk = null;     // null = not tested yet, true/false = measured
    var lastCheck = null;         // the most recent selfCheck() result

    function ensureCheckTargets() {
      if (checkTarget || !renderer) return;
      checkTarget = new THREE_.WebGLRenderTarget(CHECK_W, CHECK_H, { depthBuffer: false, stencilBuffer: false });
      checkPixels = new Uint8Array(CHECK_W * CHECK_H * 4);
    }

    /* One straight test edge, drawn with the REAL ribbon material, into a small
       render target.  Its only job is to answer a question a stub cannot: does
       this driver actually rasterise the pixel-width quad?  If the answer is
       no, the app says so instead of quietly showing nothing. */
    function windingTest() {
      if (linkWindingOk !== null) return linkWindingOk;
      if (!renderer) return null;
      try {
        if (!windingScene) {
          // a fat horizontal quad, in its OWN scene: one draw call, and the big
          // scene is not touched at all
          windingScene = new THREE_.Scene();
          var g = new THREE_.BufferGeometry();
          var w = 400, h = 40;              // a fat quad in world units
          var pos = [0, 0, 0,  w, 0, 0,  w, h, 0,  0, 0, 0,  w, h, 0,  0, h, 0];
          var oth = [w, 0, 0,  w, 0, 0,  w, h, 0,  w, 0, 0,  w, h, 0,  w, h, 0];
          var col = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1];
          var cor = [-1, 0, 1, 0, 1, 1, -1, 0, 1, 1, -1, 1];
          g.setAttribute("position", new THREE_.BufferAttribute(new Float32Array(pos), 3));
          g.setAttribute("other", new THREE_.BufferAttribute(new Float32Array(oth), 3));
          g.setAttribute("color", new THREE_.BufferAttribute(new Float32Array(col), 3));
          g.setAttribute("corner", new THREE_.BufferAttribute(new Float32Array(cor), 2));
          g.setDrawRange(0, 6);
          var quad = new THREE_.Mesh(g, linkMat);
          quad.frustumCulled = false;
          windingScene.add(quad);
        }
        if (!windingTarget) windingTarget = new THREE_.WebGLRenderTarget(64, 16, { depthBuffer: false, stencilBuffer: false });
        var cam = new THREE_.OrthographicCamera(-1, 1, 1, -1, 0.1, 10);
        cam.position.set(0, 0, 5);
        cam.lookAt(0, 0, 0);
        var prevAspect = camera.aspect;      // the ribbon reads `resolution`
        camera.aspect = 4;
        camera.updateProjectionMatrix();
        if (linkMat.uniforms && linkMat.uniforms.resolution) linkMat.uniforms.resolution.value.set(64, 16);
        renderer.setRenderTarget(windingTarget);
        renderer.setClearColor(0x000000, 1);
        renderer.clear();
        renderer.render(windingScene, cam);
        renderer.readRenderTargetPixels(windingTarget, 0, 0, 64, 16, windingBuf);
        renderer.setRenderTarget(null);
        renderer.setClearColor(0x04070c, 1);
        camera.aspect = prevAspect;
        camera.updateProjectionMatrix();
        if (linkMat.uniforms && linkMat.uniforms.resolution) {
          linkMat.uniforms.resolution.value.set(
            (renderer.domElement && renderer.domElement.clientWidth) || 1280,
            (renderer.domElement && renderer.domElement.clientHeight) || 720);
        }
        var lit = 0;
        for (var i = 0; i < windingBuf.length; i += 4) {
          if (windingBuf[i] + windingBuf[i + 1] + windingBuf[i + 2] > 24) lit++;
        }
        linkWindingOk = lit > 0;
      } catch (e) {
        linkWindingOk = null;
      }
      return linkWindingOk;
    }

    /* Renders one frame into an offscreen target and reads the pixels back.
       Returns the numbers that expose a "healthy counters, blank canvas" bug:
       non-black pixels, distinct coarse hues, whether in-between (anti-aliased)
       pixel values exist -> that is the signature of a LINE, not a blob -> and
       how many scene nodes projected into the frame. */
    function selfCheck() {
      var out = {
        supported: false, width: CHECK_W, height: CHECK_H,
        nonBlack: 0, hues: 0, linePixels: 0, nodesInFrame: 0,
        winding: linkWindingOk, dangling: dangling,
        // `reason` is the whole point of this method: "supported: false" must
        // never be a bare zero, it must NAME what is wrong (a program that did
        // not compile, or a drawn frame that came back empty).
        reason: "", shaderErrors: shaderErrors, shaderLog: shaderLog
      };
      if (!renderer) { out.reason = "нет WebGL-рендерера"; return out; }
      try {
        ensureCheckTargets();
        // size the check exactly like the visible canvas, so the projection and
        // the ribbon math are the ones the user is actually looking at
        var cw = (renderer.domElement && renderer.domElement.clientWidth) || 0;
        var ch = (renderer.domElement && renderer.domElement.clientHeight) || 0;
        if (cw > 8 && ch > 8) {
          var ar = cw / ch;
          if (ar >= 1) { CHECK_H = 96; CHECK_W = Math.round(96 * ar); }
          else { CHECK_W = 160; CHECK_H = Math.round(160 / ar); }
        }
        if (checkTarget.width !== CHECK_W || checkTarget.height !== CHECK_H) {
          checkTarget.setSize(CHECK_W, CHECK_H);
          checkPixels = new Uint8Array(CHECK_W * CHECK_H * 4);
        }
        var prevPixelRatio = null, prevSize = null;
        try {
          if (renderer.getPixelRatio) prevPixelRatio = renderer.getPixelRatio();
          if (renderer.getSize) { prevSize = renderer.getSize(new THREE_.Vector2()); }
        } catch (e) {}
        // read PIXELS, not CSS pixels: at DPR 1 the readback is 1:1 with the buffer
        try { renderer.setPixelRatio(1); } catch (e) {}
        try { renderer.setSize(CHECK_W, CHECK_H, false); } catch (e) {}
        var prevAspect = camera.aspect;
        camera.aspect = CHECK_W / CHECK_H;
        camera.updateProjectionMatrix();
        if (linkMat.uniforms && linkMat.uniforms.resolution) {
          linkMat.uniforms.resolution.value.set(CHECK_W, CHECK_H);
        }
        renderer.setRenderTarget(checkTarget);
        renderer.clear();
        renderer.render(scene, camera);
        renderer.readRenderTargetPixels(checkTarget, 0, 0, CHECK_W, CHECK_H, checkPixels);
        renderer.setRenderTarget(null);
        // put the real canvas back exactly as it was, and repaint it: a check
        // must never leave the user staring at a black canvas
        try { renderer.setPixelRatio(prevPixelRatio || 1); } catch (e) {}
        if (prevSize) { try { renderer.setSize(prevSize.x, prevSize.y, false); } catch (e) {} }
        camera.aspect = prevAspect;
        camera.updateProjectionMatrix();
        if (linkMat.uniforms && linkMat.uniforms.resolution) {
          linkMat.uniforms.resolution.value.set(prevSize ? prevSize.x : CHECK_W, prevSize ? prevSize.y : CHECK_H);
        }
        renderer.setRenderTarget(null);
        renderer.render(scene, camera);

        // ---- the analysis: count and bucket, no allocation per pixel -------- //
        var nonBlack = 0, linePixels = 0;
        var seen = {};
        var hues = 0;
        for (var i = 0; i < checkPixels.length; i += 4) {
          var r = checkPixels[i], g = checkPixels[i + 1], b = checkPixels[i + 2];
          var lum = r + g + b;
          if (lum <= 24) continue;                     // background
          nonBlack++;
          // a coarse hue bucket: 5 bits per channel is plenty to tell the
          // palette apart and cheap enough to run over ~15k pixels
          var key = ((r >> 5) << 10) | ((g >> 5) << 5) | (b >> 5);
          if (!seen[key]) { seen[key] = 1; hues++; }
          // "in-between" brightness on at least one channel is the signature of
          // a thin, anti-aliased edge; a filled sphere jumps several steps
          var mx = Math.max(r, g, b), mn = Math.min(r, g, b);
          if (mx < 250 && mn > 0 && lum < 700 && (mx - mn) < 160) linePixels++;
        }
        out.nonBlack = nonBlack;
        out.hues = hues;
        out.linePixels = linePixels;
        out.width = CHECK_W; out.height = CHECK_H;
        out.winding = linkWindingOk;
        out.dangling = dangling;

        // A program that failed to link means NOTHING on this canvas is real:
        // say so instead of returning the zeroes as if the check had passed.
        var bad = inspectPrograms();
        out.shaderErrors = bad;
        out.shaderLog = shaderLog;
        if (bad > 0) {
          out.supported = false;
          out.reason = "шейдер не собрался (" + bad + " программа): " + (shaderLog || "нет лога драйвера");
          lastCheck = out;
          return out;
        }
        // The scene has nodes but the frame came back black: that is a failure
        // of the picture, not a pass, and it has to be reported as one.
        if (sceneNodes > 0 && nonBlack === 0) {
          out.supported = false;
          out.reason = "кадр пуст: " + sceneNodes + " узлов в сцене, но ни одного непустого пикселя";
          lastCheck = out;
          return out;
        }
        out.supported = true;
        out.reason = "";

        // nodes in frame: the same projection the picker uses
        var inFrame = 0;
        for (var k = 0; k < sceneNodes; k++) {
          var n = nodeList[k];
          tmpVec.set(n.x, n.y, n.z).project(camera);
          if (tmpVec.z > -1 && tmpVec.z < 1 &&
              tmpVec.x > -1.1 && tmpVec.x < 1.1 && tmpVec.y > -1.1 && tmpVec.y < 1.1) inFrame++;
        }
        out.nodesInFrame = inFrame;
        lastCheck = out;
      } catch (e) {
        out.error = (e && e.message) ? String(e.message) : String(e);
      }
      return out;
    }

    // ------------------------------------------------------------ //
    // sparks: a few dozen at most, staggered, and only between nodes
    // that are fresh and on screen
    // ------------------------------------------------------------ //
    // A FIFO ring, preallocated once: a burst of new events must never make the
    // frame loop allocate or splice an array.
    var sparkQueue = new Array(SPARK_POOL * 4);
    var sparkQHead = 0, sparkQCount = 0, sparkBursts = 0, sparkPointsWritten = 0;
    var lastSparkAt = 0;

    function queuePush(item) {
      if (sparkQCount >= sparkQueue.length) {
        // the ring is full: drop the oldest, a burst must not grow it
        sparkQHead = (sparkQHead + 1) % sparkQueue.length;
        sparkQCount--;
      }
      sparkQueue[(sparkQHead + sparkQCount) % sparkQueue.length] = item;
      sparkQCount++;
    }

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
        if (sparkQCount >= SPARK_POOL) break;   // the pool is what matters
        queuePush({ link: parent, at: now });
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
      // Drain the ring with a stagger so a burst leaves one after another.
      // The drain is itself capped: a volley of new events must never cost
      // more than a handful of sparks inside one frame.
      var launched = 0;
      while (sparkQCount && launched < SPARK_STAGGER_MAX && now - lastSparkAt >= SPARK_STAGGER_MS) {
        launchSpark(sparkQueue[sparkQHead], now);
        sparkQueue[sparkQHead] = null;
        sparkQHead = (sparkQHead + 1) % sparkQueue.length;
        sparkQCount--;
        lastSparkAt = now;
        launched++;
        sparkBursts++;
      }

      var write = 0, liveActive = 0;
      for (var i = 0; i < sparkPool.length; i++) {
        var s = sparkPool[i];
        if (!s.active) continue;
        liveActive++;
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
      sparkPointsWritten = write;
      // the sparks are the ONE buffer that legitimately moves every frame it
      // is alive; when nothing is alive the flag is simply not set
      if (liveActive) renderDirty |= DIRTY_SPARK;
      return liveActive;
    }

    function clearSparks() {
      sparkQHead = 0; sparkQCount = 0;
      for (var i = 0; i < sparkPool.length; i++) sparkPool[i].active = false;
      for (var k = 0; k < SPARK_POINTS; k++) {
        sparkPos[k * 3] = 1e7; sparkPos[k * 3 + 1] = 1e7; sparkPos[k * 3 + 2] = 1e7;
      }
      renderDirty |= DIRTY_SPARK;
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
      wake();
      animateTo(cx, cy, cz, Math.min(r, MAX_R), animate);
    }

    var camAnim = null;
    function animateTo(x, y, z, r, animate) {
      if (!animate) {
        target.set(x, y, z); radius = r; applyCamera(); return;
      }
      camAnim = { t0: nowMs(), dur: 600, fromT: { x: target.x, y: target.y, z: target.z, r: radius }, toT: { x: x, y: y, z: z, r: r } };
      wake();
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
      var d = Math.min(v || 1, MAX_DPR);
      try { renderer.setPixelRatio(d); } catch (e) {}
      if (linkMat.uniforms && linkMat.uniforms.uPx) {
        linkMat.uniforms.uPx.value = LINK_PX * (1 + (d - 1) * 0.25);
      }
      wake();
    }

    // Thickness of the causal ribbon, in pixels.  WebGL ignores linewidth, so
    // this really is a width the user can change: the ribbon is geometry.
    function linkWidth(px) {
      if (px === undefined) return linkMat.uniforms.uPx.value;
      var v = Math.max(1, Math.min(12, Number(px) || LINK_PX));
      try { localStorage.setItem("graff.river.linkPx", String(v)); } catch (e) {}
      linkMat.uniforms.uPx.value = v * (1 + (linkDpr - 1) * 0.25);
      wake();
      return v;
    }

    function resize(w, h) {
      wake();
      if (!renderer) return;
      camera.aspect = (w || 1) / (h || 1);
      camera.updateProjectionMatrix();
      renderer.setSize(w, h, false);
      if (linkMat.uniforms && linkMat.uniforms.resolution) {
        linkMat.uniforms.resolution.value.set(w || 1, h || 1);
      }
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
      wake();
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
      wake();
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
    var frameMs = 0, frameMsMax = 0, frameMsSum = 0, frameCount = 0;
    var drawCalls = 0;
    var triangles = 0;
    var animBudget = 0;      // ms the last animation pass took
    var loopDirty = true;
    var pendingFrame = true; // scene changed since the last drawn frame
    var frames = 0;          // frames actually drawn
    var nonEmptyFrames = 0;  // frames drawn while the scene had nodes
    var lastFrameAt = 0;
    var idle = false;         // nothing is animating right now
    var idleSince = 0;        // when the scene last went quiet
    var idleGap = 0;          // ms we have been quiet
    var coolSettled = false;  // every colour has reached its final bucket
    var coolQuiet = 0;        // consecutive recolour ticks that changed nothing
    // Re-uploads/second, measured over a rolling window.  The ring is
    // preallocated: measuring the scene must not itself allocate.
    var ringAt = new Float64Array(UPLOAD_RING);
    var ringN = new Float64Array(UPLOAD_RING);
    var ringHead = 0, ringCount = 0;
    var uploadedMatrix = 0, uploadedColour = 0, uploadsSparks = 0, uploadsLinks = 0, uploadsLinkColor = 0;
    var uploadedMatrixElems = 0, uploadedColourElems = 0;   // instance slots uploaded
    var uploadedLinkGeometry = 0;
    var uploadsPerSecond = 0;
    var firstColourUpload = true;   // the colour buffer needs one full push

    // One entry per upload burst, so the rate reads "bursts per second".
    function noteUpload(n, now) {
      ringAt[ringHead] = now; ringN[ringHead] = n;
      ringHead = (ringHead + 1) % UPLOAD_RING;
      if (ringCount < UPLOAD_RING) ringCount++;
      uploadRate(now);
    }

    // Read on demand, so the figure decays to zero by itself once the scene
    // has parked - it must never report a stale "bursts per second".
    function uploadRate(now) {
      var fresh = 0;
      for (var i = 0; i < ringCount; i++) {
        if (now - ringAt[i] <= UPLOAD_WINDOW_MS) fresh += ringN[i];
      }
      uploadsPerSecond = fresh;
      return fresh;
    }

    function nowMs() {
      return (window.performance && window.performance.now) ? window.performance.now() : Date.now();
    }

    /* Walk renderer.info.programs (three keeps the live programs there) and
       collect the ones that did NOT link.  Reading `diagnostics.runnable` is
       how three itself expresses "this program is unusable"; the programLog and
       the per-stage logs carry the real driver message, so that is what we
       report.  Called on the first frames and by selfCheck(); it never
       allocates while the scene is healthy. */
    var shaderErrors = 0;      // distinct broken programs seen
    var shaderLog = "";        // the driver's own message, for the journal
    var shaderChecked = false; // at least one inspection ran
    function inspectPrograms() {
      shaderChecked = true;
      var list = null;
      try { list = renderer && renderer.info && renderer.info.programs; } catch (e) { list = null; }
      if (!list || !list.length) return 0;
      var bad = 0, log = "";
      for (var i = 0; i < list.length; i++) {
        var pr = list[i];
        var diag = pr && pr.diagnostics;
        // No diagnostics object means three could not even build the program.
        var ok = diag ? diag.runnable !== false : true;
        if (ok) continue;
        bad++;
        if (!log) {
          log = (diag && diag.programLog ? diag.programLog : "") ;
          if (diag && diag.vertexShader && diag.vertexShader.log) log += " | vertex: " + diag.vertexShader.log;
          if (diag && diag.fragmentShader && diag.fragmentShader.log) log += " | fragment: " + diag.fragmentShader.log;
          if (!log) log = "программа не собралась (нет diagnostics)";
        }
      }
      if (bad && bad !== shaderErrors) {
        shaderErrors = bad;
        shaderLog = log;
        warn("русло: шейдер не собрался — " + bad + " программа(ы): " + log);
      }
      return bad;
    }

    function renderOnce(now) {
      if (typeof now !== "number") now = nowMs();
      var t0 = nowMs();
      // A node that was never written would be invisible (zero matrix), so the
      // very first frame after a data change fills in every missing instance.
      ensureMatrices(now);
      pendingFrame = false;

      // ---- animated pass: only the nodes that are actually moving -------- //
      passDirty = DIRTY_MATRIX | DIRTY_COLOR;
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
      // ---- cooling pass: batched on a timer, never per frame.  It is the only
      // thing left to do once every node has arrived, and it stops for good as
      // soon as the whole palette has settled. ------------------------------ //
      if (now - lastRecolour > RECOLOUR_MS) {
        lastRecolour = now;
        passDirty = DIRTY_MATRIX | DIRTY_COLOR;
        var changed = recolour(now);
        if (changed) coolQuiet = 0;
        else if (++coolQuiet >= 3) coolSettled = true;   // nothing will change
      }
      passDirty = DIRTY_MATRIX | DIRTY_COLOR;
      flushDirty();
      animBudget = nowMs() - t0;
      pendingFrame = false;

      if (renderer) {
        if (linkWindingOk === null && sceneLinks > 0) windingTest();
        renderer.render(scene, camera);
        try {
          var info = renderer.info.render;
          drawCalls = info.calls || 0;
          triangles = info.triangles || 0;
        } catch (e) {}
        // Right after a draw three has built (or reused) every program, so this
        // is the moment a compile failure can be caught.  A healthy scene pays
        // one array walk once.
        if (!shaderChecked) inspectPrograms();
        var ms = nowMs() - t0;                  // CPU cost of the JS pass
        if (ms > frameMsMax) frameMsMax = ms;   // absolute worst pass seen
        frameMsSum += ms;
        frameCount++;
        frames++;
        if (sceneNodes > 0) nonEmptyFrames++;
        lastFrameAt = nowMs();
      }
      return !!renderer;
    }

    // Push exactly the buffers that changed since the last frame.  An idle or
    // merely rotating frame therefore uploads NOTHING at all: the instances
    // already live on the GPU and the camera is a uniform, not a buffer.
    function flushDirty() {
      var d = renderDirty;
      renderDirty = 0;
      if (!d) return;
      var iMin = dirtyMin, iMax = dirtyMax;
      dirtyMin = -1; dirtyMax = -1;
      noteUpload(1, nowMs());
      var span = (iMin >= 0 && iMax >= iMin) ? (iMax - iMin + 1) : 0;
      // The FIRST push of a buffer must cover the whole buffer: a partial range
      // only updates a buffer that already holds valid data, and an instance
      // whose slot was never pushed would draw from a fresh (zero) buffer.
      var ranged = span > 0 && span < sceneNodes && !firstColourUpload;
      if (d & DIRTY_COLOR && firstColourUpload) firstColourUpload = false;
      if (d & DIRTY_MATRIX) {
        if (cores.instanceMatrix) {
          cores.instanceMatrix.updateRange = ranged
            ? { offset: iMin * 16, count: span * 16 } : { offset: 0, count: -1 };
          cores.instanceMatrix.needsUpdate = true;
          uploadedMatrix++;
          uploadedMatrixElems += (ranged ? span : sceneNodes);
        }
      }
      if (d & DIRTY_COLOR) {
        if (cores.instanceColor) {
          cores.instanceColor.updateRange = ranged
            ? { offset: iMin * 3, count: span * 3 } : { offset: 0, count: -1 };
          cores.instanceColor.needsUpdate = true;
          uploadedColour++;
          uploadedColourElems += (ranged ? span : sceneNodes);
        }
      }
      if (d & DIRTY_LINK) { if (linkGeo.attributes.position) linkGeo.attributes.position.needsUpdate = true; uploadsLinks++; }
      if (d & DIRTY_LINKCOLOR) { if (linkGeo.attributes.color) linkGeo.attributes.color.needsUpdate = true; uploadsLinkColor++; }
      if (d & DIRTY_SPARK) {
        if (sparkGeo.attributes.position) sparkGeo.attributes.position.needsUpdate = true;
        if (sparkGeo.attributes.color) sparkGeo.attributes.color.needsUpdate = true;
        uploadsSparks++;
      }
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
      if (camAnim || pendingFrame) return true;
      if (active.length) return true;
      // cooling counts as work while it still changes something, and stops
      // entirely once the palette has settled
      if (!coolSettled && sceneNodes && now - lastRecolour > RECOLOUR_MS) return true;
      for (var c = 0; c < crests.length; c++) if (now - crests[c].start <= crests[c].dur) return true;
      for (var s = 0; s < sparkPool.length; s++) if (sparkPool[s].active) return true;
      return false;
    }

    // Once the scene is quiet the loop parks itself: it stops asking for
    // animation frames altogether and leaves the last correct frame on screen.
    // The mode stays live because every producer (new data, a spark, a crest,
    // the camera, a resize) wakes it again.
    function park() {
      // NOTE: `running` stays true on purpose.  A parked river is still a live
      // mode - it resumes on the next wake() - it has merely stopped asking for
      // animation frames.  Only stop() (leaving the mode) sets running = false.
      if (rafId !== null) { cancelAnimationFrame(rafId); rafId = null; }
      if (idleSince) idleGap = nowMs() - idleSince;
    }

    function loop() {
      rafId = requestAnimationFrame(loop);
      if (isPaused()) { idle = true; return; }
      var now = nowMs();
      var dt = lastNow ? (now - lastNow) : 16;
      lastNow = now;
      if (hasWork(now)) {
        // "frameMs" is the wall-clock gap between frames: that, not the CPU
        // cost of the JS pass, is what a user feels as smoothness.
        frameMs = frameMs ? (frameMs * 0.8 + dt * 0.2) : dt;
        idle = false; idleSince = 0; idleGap = 0;
        tickCamera(now);
        updateSparks(now);
        renderOnce(now);
        pruneActive(now);
        return;
      }
      // Nothing to animate.  A live session usually keeps arriving, so linger
      // for a short grace window on a slow tick; after that the loop parks for
      // real and stops asking for frames.  The last correct frame stays on
      // screen, and every producer wakes the loop again.
      idle = true;
      if (!idleSince) { idleSince = now; return; }
      if (now - idleSince < IDLE_MS) {
        if (now - lastFrameAt >= IDLE_TICK_MS) {
          tickCamera(now);
          updateSparks(now);
          renderOnce(now);
          pruneActive(now);
        }
        return;
      }
      idleGap = now - idleSince;
      // a parked loop may never leave a stale frame behind
      if (pendingFrame) renderOnce(now);
      park();
    }

    function start() {
      pendingFrame = true;
      idle = false; idleSince = 0; idleGap = 0;
      running = true;
      // A parked loop has running === true and rafId === null; that is exactly
      // the state start() must revive, so the gate is the pending frame, not
      // the `running` flag.
      if (rafId !== null) return;
      lastNow = 0;
      loopDirty = true;
      rafId = requestAnimationFrame(loop);
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

    function renderNow() {
      var drew = renderOnce(nowMs());
      pendingFrame = false;
      return drew;
    }

    function wake(ms) {
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
    // Picking allocates nothing either: it reuses the frame-loop scratch.
    function pick(clientX, clientY, w, h) {
      if (!sceneNodes) return null;
      var best = null, bestD = 24 * 24;
      for (var i = 0; i < sceneNodes; i++) {
        var n = nodeList[i];
        tmpVec.set(n.x, n.y, n.z).project(camera);
        var sx = (tmpVec.x * 0.5 + 0.5) * w;
        var sy = (-tmpVec.y * 0.5 + 0.5) * h;
        var d = (sx - clientX) * (sx - clientX) + (sy - clientY) * (sy - clientY);
        if (d < bestD) { bestD = d; best = n; }
      }
      return best ? { id: best.id, index: best.index, screen: true } : null;
    }

    function stats() {
      var r = function (v) { return Math.round(v * 100) / 100; };
      return {
        nodes: sceneNodes, links: sceneLinks,
        drawCalls: drawCalls, triangles: triangles,
        frameMs: Math.round(frameMs * 10) / 10,
        frameMsMax: Math.round(frameMsMax * 10) / 10,
        frameMsAvg: frameCount ? Math.round((frameMsSum / frameCount) * 100) / 100 : 0,
        fps: frameMs > 0 ? Math.round(1000 / frameMs) : 0,
        running: running, animBudget: r(animBudget),
        // proof that the canvas is not black: frames really drawn, and whether
        // any of them was drawn with the graph populated
        frames: frames, nonEmptyFrames: nonEmptyFrames,
        drawn: frames > 0, painted: nonEmptyFrames > 0,
        pendingFrame: pendingFrame,
        // A parked loop is still a LIVE mode: `running` stays true and it wakes
        // on the next update.  `scheduled` is the honest "is a frame actually
        // queued right now" figure the perf check reads.
        scheduled: rafId !== null,
        idle: idle,
        idleMs: IDLE_MS,
        idleGap: Math.round(idleGap), coolSettled: coolSettled,
        lastFrameAgo: lastFrameAt ? Math.round(nowMs() - lastFrameAt) : null,
        // the effect of the whole exercise, in numbers:
        uploadsPerSecond: uploadRate(nowMs()),
        uploadMatrix: uploadedMatrix, uploadColour: uploadedColour,
        uploadSparks: uploadsSparks, uploadLinks: uploadsLinks,
        uploadLinkGeometry: uploadedLinkGeometry,
        uploadMatrixElems: uploadedMatrixElems, uploadColourElems: uploadedColourElems,
        writtenNodes: writtenNodes, writtenLinks: writtenLinks,
        sparkLimit: SPARK_POOL, bandCap: BAND_CAP, maxNodes: MAX_NODES,
        sparkStaggerMax: SPARK_STAGGER_MAX, recolourBatch: RECOLOUR_BATCH,
        linkPx: LINK_PX,
        haloCount: halos ? halos.count : 0,
        haloGrow: HALO_GROW,
        haloOpacity: haloMat.opacity,
        coreSegments: CORE_SEG_W + "x" + CORE_SEG_H,
        sparkPointsWritten: sparkPointsWritten,
        // The honest view of the canvas.  `checkDone` false means selfCheck()
        // has never run; the app shows a dash instead of inventing a number.
        danglingLinks: dangling,
        warnings: warnings,
        lastWarning: lastWarning,
        linkWinding: linkWindingOk,
        checkDone: !!lastCheck,
        checkSupported: !!(lastCheck && lastCheck.supported),
        checkNonBlack: lastCheck ? lastCheck.nonBlack : 0,
        checkHues: lastCheck ? lastCheck.hues : 0,
        checkLines: lastCheck ? lastCheck.linePixels : 0,
        checkNodes: lastCheck ? lastCheck.nodesInFrame : 0,
        checkSize: lastCheck ? (lastCheck.width + "x" + lastCheck.height) : "",
        checkError: lastCheck && lastCheck.error ? lastCheck.error : null,
        // The honest verdict of the pixel check: `checkSupported` is false not
        // only when the check could not run, but also when the program did not
        // compile or the frame came back empty - and then `checkReason` says
        // why.  That is what stops a black canvas from reading as "healthy".
        checkReason: lastCheck && lastCheck.reason ? lastCheck.reason : "",
        shaderErrors: shaderErrors,
        shaderLog: shaderLog,
        shaderChecked: shaderChecked
      };
    }

    // A test/DI surface for the park-and-paint invariant: how many frames were
    // drawn and skipped while the loop was parked, plus the counters above.
    function perfCounters() {
      return {
        frames: frames, uploadsPerSecond: uploadRate(nowMs()),
        uploadMatrix: uploadedMatrix, uploadColour: uploadedColour,
        uploadSparks: uploadsSparks, uploadLinks: uploadsLinks,
        uploadLinkGeometry: uploadedLinkGeometry,
        uploadMatrixElems: uploadedMatrixElems, uploadColourElems: uploadedColourElems,
        sparkBursts: sparkBursts, writtenNodes: writtenNodes, writtenLinks: writtenLinks,
        running: running, scheduled: rafId !== null,
        idleGap: Math.round(idleGap)
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
      try { halos.geometry.dispose(); haloMat.dispose(); } catch (e) {}
      try { linkGeo.dispose(); linkMat.dispose(); } catch (e) {}
      try { sparkGeo.dispose(); sparkMat.dispose(); } catch (e) {}
      if (renderer && renderer.dispose) { try { renderer.dispose(); } catch (e) {} }
      renderer = null;
      nodeList = []; indexOfId = newIdMap(); linkList = []; sceneNodes = 0; sceneLinks = 0;
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
      selfCheck: selfCheck,
      windingTest: windingTest,
      setWarnHandler: function (fn) { warnHandler = fn; },
      perfCounters: perfCounters,
      colourTable: function () { return colourTable; },
      linkWidth: linkWidth,
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
