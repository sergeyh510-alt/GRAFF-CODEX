/* GRAFF test fixture: the "shader builds" stand for the river module.
 *
 * Loads the REAL static/vendor/three.min.js (r137) and the REAL
 * static/js/river.js into a VM, supplies a mock WebGL context that behaves like
 * a driver compiler, and checks the LINK CONTRACT of every program three builds:
 * a shader body may not read a varying the active #defines never declared.
 * That is the check the browser performs when it prints
 *   THREE.WebGLProgram: Shader Error 0 - VALIDATE_STATUS false
 *   WebGL: INVALID_OPERATION: useProgram: program not valid
 * and turns the canvas black while every counter still reads "healthy".
 *
 * Consumed by RiverShaderTests in tests/test_graff.py; keep in sync with
 * static/js/river.js.
 */
"use strict";
/* River SHADER stand: the real static/js/river.js under the real vendored three
 * (r137), with a mock WebGL context that behaves like a driver's compiler where
 * it matters: it captures every shaderSource, and at linkProgram time it runs a
 * defines-aware preprocessor over both stages and fails the program if any
 * watch-listed varying/attribute is USED but never DECLARED.
 *
 * That is exactly the class of bug that produced, in a real browser,
 *   THREE.WebGLProgram: Shader Error 0 - VALIDATE_STATUS false
 *   WebGL: INVALID_OPERATION: useProgram: program not valid
 * while every counter (nodes, drawCalls, triangles, painted) stayed "healthy".
 */
const path = require("path");
const fs = require("fs");
const vm = require("vm");

const WATCH = ["vColor", "vNormal", "vUv", "vUv2", "vViewPosition", "vWorldPosition",
  "vClipPosition", "vFragDepth", "vIsPerspective", "vShadowCoord", "vTangent",
  "vBitangent", "vEyePosition", "vFogDepth", "vHighPrecisionZW", "instanceColor",
  "instanceMatrix"];
const ATTRS = ["position", "normal", "uv", "color", "other", "corner", "instanceColor",
  "instanceMatrix"];

function evalCond(expr, defines) {
  let s = expr.replace(/defined\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)/g, (m, n) => (defines[n] !== undefined ? "1" : "0"));
  s = s.replace(/\b([A-Za-z_][A-Za-z0-9_]*)\b/g, (m) => (defines[m] !== undefined && /^-?\d+$/.test(defines[m]) ? defines[m] : "0"));
  s = s.replace(/(-?\d+)\s*(>=|<=|==|!=|>|<)\s*(-?\d+)/g, (m, a, op, b) => {
    const x = +a, y = +b;
    const r = op === ">=" ? x >= y : op === "<=" ? x <= y : op === "==" ? x === y : op === "!=" ? x !== y : op === ">" ? x > y : x < y;
    return r ? "1" : "0";
  });
  s = s.replace(/!/g, " ! ").replace(/&&/g, " && ").replace(/\|\|/g, " || ");
  try { return !!new Function("return (" + s + ");")(); } catch (e) { return true; }
}

function preprocess(src) {
  const lines = src.split("\n");
  const out = [];
  const defines = Object.create(null);
  const stack = [];
  let active = true;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    const mIf = /^#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$/.exec(line);
    if (mIf) {
      const kind = mIf[1], rest = (mIf[2] || "").trim();
      if (kind === "if" || kind === "ifdef" || kind === "ifndef") {
        const cond = kind === "ifdef" ? defines[rest] !== undefined
          : kind === "ifndef" ? defines[rest] === undefined : evalCond(rest, defines);
        stack.push({ parentActive: active, taken: cond });
        active = active && cond;
      } else if (kind === "elif") {
        const top = stack[stack.length - 1];
        if (top) { const cond = !top.taken && evalCond(rest, defines); top.taken = top.taken || cond; active = top.parentActive && cond; }
      } else if (kind === "else") {
        const top = stack[stack.length - 1];
        if (top) { const cond = !top.taken; top.taken = true; active = top.parentActive && cond; }
      } else { const top = stack.pop(); active = top ? top.parentActive : true; }
      continue;
    }
    const mDef = /^#\s*define\s+([A-Za-z_][A-Za-z0-9_]*)\s*(.*)$/.exec(line);
    if (mDef) { if (active) defines[mDef[1]] = mDef[2].trim(); continue; }
    if (/^#/.test(line)) continue;
    if (active) out.push(lines[i]);
  }
  return { code: out.join("\n"), defines };
}

function declared(code, kind) {
  const set = Object.create(null);
  const re = new RegExp("^\\s*" + kind + "\\s+(?:highp\\s+|mediump\\s+|lowp\\s+)?(?:vec[234]|float|mat[234]|int)\\s+([A-Za-z_][A-Za-z0-9_]*)", "gm");
  let m;
  while ((m = re.exec(code))) set[m[1]] = true;
  return set;
}
function usedIn(code, names) {
  const used = Object.create(null);
  for (const n of names) {
    if (new RegExp("(^|[^A-Za-z0-9_.])" + n + "([^A-Za-z0-9_]|$)").test(code)) used[n] = true;
  }
  return used;
}

const STAGE_DEBUG = [];
function checkStage(src, isVert) {
  const pp = preprocess(src);
  const decl = Object.create(null);
  const v = declared(pp.code, "varying"), a = declared(pp.code, "attribute");
  for (const k in v) decl[k] = true;
  for (const k in a) decl[k] = true;
  const problems = [];
  const usedV = usedIn(pp.code, WATCH);
  for (const n in usedV) {
    // an instance attribute is bound by three, not declared in the shader body
    if (n === "instanceColor" || n === "instanceMatrix") {
      if (pp.defines["USE_INSTANCING_COLOR"] === undefined && n === "instanceColor") {
        problems.push("uses '" + n + "' with no USE_INSTANCING_COLOR define");
      }
      continue;
    }
    if (!decl[n]) problems.push("uses '" + n + "' but never declares it (no active define)");
  }
  // Attributes are a VERTEX-side concept only (a "vertexColors" name in a
  // fragment shader is just a varying alias).  A USED attribute the vertex
  // stage never declared is exactly the GLSL error a driver reports.
  if (isVert) {
    // Only the RIVER's own attributes are watched by name: "position"/"normal"/
    // "uv" are supplied by three's prefix and "color" is ALSO a function
    // parameter name in <common> (average(vec3 color)), so a bare word search
    // would be a false positive.  The precise rule for colour instead: if the
    // body multiplies vColor by `color`, then an "attribute ... color" must be
    // declared (that is exactly the USE_COLOR contract).
    for (const n of ["other", "corner"]) {
      if (usedIn(pp.code, [n])[n] && !decl[n]) problems.push("uses attribute '" + n + "' with no declaration");
    }
    if (/vColor\s*\*=\s*color\s*;/.test(pp.code) && !decl["color"]) {
      problems.push("uses attribute 'color' (vColor *= color) with no declaration");
    }
  }
  STAGE_DEBUG.push({ stage: isVert ? "VERT" : "FRAG", uGrow: /uGrow/.test(pp.code),
    uShrink: /uShrink/.test(pp.code), haloFrag: /diffuseColor\.a \*=/.test(pp.code),
    vcolorDecl: !!decl["vColor"],
    name: (/SHADER_NAME\s+(\w+)/.exec(pp.code) || [null, "?"])[1],
    declKeys: Object.keys(decl), usedV: Object.keys(usedIn(pp.code, WATCH)),
    colorLines: pp.code.split("\n").filter(l => /attribute\s+vec[34]\s+color/.test(l)),
    defines: Object.keys(pp.defines).filter(k => k.indexOf("USE_") === 0) });
  return { stage: isVert ? "VERT" : "FRAG", problems, defines: Object.keys(pp.defines).filter(k => k.indexOf("USE_") === 0 || k === "FLAT_SHADED"), code: pp.code };
}

// ---------------------------------------------------------------- mock GL --- //
const SHADER_SRC = new Map();
const PROGRAMS = [];
const LINKED = [];
function makeGL() {
  const noop = () => {};
  const gl = {
    canvas: null, drawingBufferWidth: 1280, drawingBufferHeight: 720,
    getParameter(p) {
      if (p === 3386 || p === 35660 || p === 34076) return new Int32Array([16384, 16384]);
      if (p === 2978) return new Int32Array([0, 0, 1280, 720]);
      if (p === 7938) return "WebGL 2.0 (mock)";
      if (p === 35724) return "WebGL GLSL ES 3.00 (mock)";
      if (p === 34921) return 16;
      if (p === 35661) return 32;
      return 4096;
    },
    getExtension(n) {
      if (n === "WEBGL_debug_renderer_info") return { UNMASKED_RENDERER_WEBGL: 1 };
      if (n === "ANGLE_instanced_arrays") return { drawElementsInstancedANGLE() {}, drawArraysInstancedANGLE() {}, vertexAttribDivisorANGLE() {} };
      if (n === "OES_vertex_array_object") return { createVertexArrayOES: () => ({}), bindVertexArrayOES() {}, deleteVertexArrayOES() {} };
      if (n === "EXT_blend_minmax") return { MIN_EXT: 32775, MAX_EXT: 32776 };
      return null;
    },
    getShaderPrecisionFormat: () => ({ precision: 23, rangeMin: 127, rangeMax: 127 }),
    getShaderParameter: () => true,
    getShaderInfoLog: (sh) => (sh && sh.__log) || "",
    getProgramParameter(prog, pname) {
      if (pname === 35714 || pname === 35715) return !!(prog && prog.__ok);
      return true;
    },
    getProgramInfoLog: (prog) => (prog && prog.__log) || "",
    getShaderSource: (sh) => SHADER_SRC.get(sh) || "",
    getActiveUniform: () => ({ name: "u", type: 5124, size: 1 }),
    getActiveAttrib: () => ({ name: "a", type: 5126, size: 1 }),
    getUniformLocation: () => ({}), getAttribLocation: () => 0, getError: () => 0,
    isContextLost: () => false, checkFramebufferStatus: () => 36053,
    getSupportedExtensions: () => [], getContextAttributes: () => ({}), isEnabled: () => false,
    getInternalformatParameter: () => new Int32Array([4]), fenceSync: () => ({}), clientWaitSync: () => 37146,
    createShader: (type) => ({ __type: type }), createProgram: () => ({}), createBuffer: () => ({}),
    createTexture: () => ({}), createFramebuffer: () => ({}), createRenderbuffer: () => ({}),
    createVertexArray: () => ({}), createSampler: () => ({}), createQuery: () => ({}),
    shaderSource(sh, src) { SHADER_SRC.set(sh, src); },
    attachShader(prog, sh) { (prog.__shaders = prog.__shaders || []).push(sh); },
    linkProgram(prog) {
      const stages = (prog.__shaders || []).map(sh => checkStage(SHADER_SRC.get(sh) || "", sh.__type === 35633));
      const problems = [];
      for (const st of stages) for (const p of st.problems) problems.push(st.stage + ": " + p);
      // link contract: a varying the vertex stage writes must exist in the fragment stage
      const vs = stages.find(s => s.stage === "VERT"), fs = stages.find(s => s.stage === "FRAG");
      if (vs && fs) {
        const names = Object.create(null);
        for (const n of WATCH) names[n] = true;
        const written = {};
        for (const n of WATCH) if (new RegExp("(^|[^A-Za-z0-9_.])" + n + "\\s*=", "m").test(vs.code)) written[n] = true;
        const fdecl = declared(fs.code, "varying");
        for (const n in written) if (!fdecl[n] && !/^attribute/m.test(n)) problems.push("LINK: vertex writes '" + n + "' but the fragment stage has no such varying");
      }
      prog.__ok = problems.length === 0;
      prog.__log = problems.join(" ; ");
      prog.__problems = problems;
      PROGRAMS.push({ ok: prog.__ok, problems });
      if (prog.__ok) LINKED.push(prog);
    },
    readPixels(x, y, w, h, fmt, type, out) { if (out && out.fill) out.fill(0); }
  };
  ["uniform1i","uniform1f","uniform1fv","uniform2fv","uniform3fv","uniform4fv","uniform2f","uniform3f","uniform4f",
   "uniformMatrix2fv","uniformMatrix3fv","uniformMatrix4fv","uniformBlockBinding","bindBufferBase","bindBufferRange",
   "enable","disable","clear","clearColor","clearDepth","clearStencil","colorMask","depthMask","depthFunc","stencilFunc",
   "stencilOp","stencilMask","blendFunc","blendFuncSeparate","blendEquation","blendEquationSeparate","viewport","scissor",
   "enableVertexAttribArray","disableVertexAttribArray","vertexAttribPointer","vertexAttribDivisor","vertexAttribIPointer",
   "bindBuffer","bufferStorage","bindFramebuffer","bindRenderbuffer","framebufferTexture2D","framebufferRenderbuffer",
   "renderbufferStorage","renderbufferStorageMultisample","bindVertexArray","deleteVertexArray","deleteBuffer",
   "deleteTexture","deleteProgram","deleteShader","deleteFramebuffer","deleteRenderbuffer","deleteSampler","deleteQuery",
   "beginQuery","endQuery","deleteSync","cullFace","frontFace","lineWidth","polygonOffset","bindAttribLocation",
   "compileShader","validateProgram","useProgram","detachShader","flush","finish",
   "invalidateFramebuffer","readBuffer","blitFramebuffer","copyTexImage2D","copyTexSubImage2D","texImage3D",
   "texSubImage2D","texStorage2D","texStorage3D","pixelStorei","activeTexture","bindTexture","bindSampler",
   "generateMipmap","texParameteri","texParameterf","compressedTexImage2D","depthRange","sampleCoverage",
   "stencilFuncSeparate","stencilOpSeparate","stencilMaskSeparate","hint","blendColor","uniform3ui","uniform4ui","uniform1ui",
   "drawArrays","drawElements","drawArraysInstanced","drawElementsInstanced","bufferData","bufferSubData","texImage2D"
   ].forEach(n => { gl[n] = noop; });
  return gl;
}

// ---------------------------------------------------------------- boot ----- //
const CTX2D = new Proxy({}, { get(t, p) {
  if (p in t) return t[p];
  if (p === "createRadialGradient" || p === "createLinearGradient") return () => ({ addColorStop() {} });
  if (p === "measureText") return () => ({ width: 10 });
  const f = () => undefined; t[p] = f; return f; }, set(t, p, v) { t[p] = v; return true; } });
const CANVAS = { tagName: "CANVAS", width: 1280, height: 720, clientWidth: 1280, clientHeight: 720, style: {},
  addEventListener() {}, removeEventListener() {}, remove() {}, setAttribute() {}, getAttribute: () => "1280",
  parentNode: null,
  getBoundingClientRect: () => ({ left: 0, top: 0, right: 1280, bottom: 720, width: 1280, height: 720 }),
  getContext(kind) { if (kind === "2d") return CTX2D; const gl = makeGL(); gl.canvas = this; return gl; } };
global.window = global; global.self = global; global.devicePixelRatio = 1;
Object.defineProperty(global, "navigator", { value: { userAgent: "node-river-shaders" }, configurable: true, writable: true });
let __now = 1000000;
global.performance = { now: () => __now };
global.Element = function () {};
global.Element.prototype = { appendChild: c => c, removeChild: c => c, insertBefore: c => c,
  addEventListener() {}, removeEventListener() {}, setAttribute() {}, getAttribute: () => null, style: {},
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 1280, height: 720 }) };
global.HTMLElement = global.Element; global.HTMLCanvasElement = global.Element; global.HTMLDivElement = global.Element;
global.Node = global.Element; global.CanvasRenderingContext2D = function () {};
let __tid = 1, __queue = [];
global.requestAnimationFrame = cb => { const id = __tid++; __queue.push({ id, cb }); return id; };
global.cancelAnimationFrame = id => { __queue = __queue.filter(e => e.id !== id); };
function pump(dt) { const q = __queue; __queue = []; __now += (dt == null ? 16 : dt); q.forEach(e => { try { e.cb(__now); } catch (err) {} }); }
function drain(n, dt) { for (let i = 0; i < n; i++) pump(dt); }
const HOST = { clientWidth: 1280, clientHeight: 720, style: {},
  addEventListener() {}, removeEventListener() {}, appendChild() {}, removeChild() {},
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 1280, height: 720 }) };
global.document = { hidden: false, visibilityState: "visible",
  createElement: t => (t === "canvas" ? CANVAS : { style: {}, addEventListener() {}, removeEventListener() {} }),
  createElementNS: (ns, t) => (t === "canvas" ? CANVAS : { style: {}, addEventListener() {}, removeEventListener() {} }),
  getElementById: () => ({ style: {}, addEventListener() {}, removeEventListener() {}, setAttribute() {}, getAttribute: () => null }),
  querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, removeEventListener() {} };
global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };

const threeCtx = { console, navigator: global.navigator, performance: global.performance,
  requestAnimationFrame: global.requestAnimationFrame, cancelAnimationFrame: global.cancelAnimationFrame,
  document: global.document, setTimeout, clearTimeout };
threeCtx.self = threeCtx; threeCtx.window = threeCtx; threeCtx.globalThis = threeCtx;
vm.createContext(threeCtx);
vm.runInContext(fs.readFileSync(path.resolve("static/vendor/three.min.js"), "utf8"), threeCtx);
const THREE = threeCtx.THREE;
global.THREE = THREE; global.window.THREE = THREE;

const ctx = { window: global, document: global.document, console, THREE,
  requestAnimationFrame: global.requestAnimationFrame, cancelAnimationFrame: global.cancelAnimationFrame,
  performance: global.performance, setTimeout, clearTimeout };
vm.createContext(ctx); ctx.window = ctx; ctx.self = ctx; ctx.document = global.document; ctx.THREE = THREE;
vm.runInContext(fs.readFileSync(path.resolve(process.env.RIVER_JS || "static/js/river.js"), "utf8"), ctx);
const R = ctx.GRAFFRiver;

function chain(n) {
  const out = []; let prev = null;
  const T = ["reason", "message", "tool_call", "tool_result", "file_edit", "error"];
  for (let i = 0; i < n; i++) { const id = "n" + i;
    out.push({ id: id, source: { id: id, ts: 1000 + i, type: T[i % 6], actor: "codex", parent: prev,
      label: "e" + i, meta: { name: "tool" + i, call_id: "c" + i } } }); prev = id; }
  return out;
}
function feed(river, items) {
  const m = R.layout(items, true);
  const ls = m.nodes.slice(1).map((n, i) => ({ from: m.nodes[i].id, to: n.id }));
  m.nodes.forEach(n => { n.ts = n.ts || 0; });
  river.setData(m.nodes, ls, {});
  return m;
}

const warnings = [];
const river = R.create(HOST, { onWarn: function (m) { warnings.push(m); } });
feed(river, chain(400));
drain(3, 16);
const st = river.stats();
const check = river.selfCheck();

const badPrograms = PROGRAMS.filter(p => !p.ok);
fs.writeFileSync(".work/stage_debug.json", JSON.stringify(STAGE_DEBUG, null, 1), "utf8");
// dump the shader sources of each broken program for inspection
const dbg = [];
(function () {
  const seen = new Set();
  const arr = Array.from(SHADER_SRC.entries());
  for (const prog of PROGRAMS) {
    if (prog.ok) continue;
    for (let i = 0; i < arr.length; i++) {
      const src = arr[i][1];
      const isV = /gl_Position/.test(src);
      const tag = (isV ? "V" : "F") + ":" + /SHADER_NAME\s+(\w+)/.exec(src)[1];
      if (seen.has(tag)) continue;
      seen.add(tag);
      fs.writeFileSync(".work/badsh_" + tag.replace(/[:]/g, "_") + ".txt", src, "utf8");
      dbg.push(tag + " len=" + src.length);
    }
  }
})();
const report = {
  programs: PROGRAMS.length, linked: LINKED.length, broken: badPrograms.length,
  brokenDetail: badPrograms.map(p => p.problems),
  shaderStageCount: SHADER_SRC.size,
  nodes: st.nodes, links: st.links, drawCalls: st.drawCalls, triangles: st.triangles,
  painted: st.painted,
  shaderErrors: st.shaderErrors, shaderChecked: st.shaderChecked, shaderLog: st.shaderLog,
  check: { supported: check.supported, reason: check.reason, nonBlack: check.nonBlack,
    hues: check.hues, linePixels: check.linePixels, nodesInFrame: check.nodesInFrame,
    shaderErrors: check.shaderErrors },
  warnings: warnings.slice(0, 4),
  halo: (function () {
    // the halo is the vertex pass that grew the instance with uGrow; its
    // fragment partner must declare vColor (it reads dot(vColor.rgb, ...))
    const hv = STAGE_DEBUG.filter(s => s.uGrow && s.stage === "VERT");
    const hf = STAGE_DEBUG.filter(s => s.haloFrag && s.stage === "FRAG");
    return { vertexPresent: hv.length > 0, fragmentPresent: hf.length > 0,
      vertexDeclaresVColor: hv.some(s => s.vcolorDecl),
      fragmentDeclaresVColor: hf.some(s => s.vcolorDecl) };
  })(),
  programsDetail: STAGE_DEBUG.map(s => ({ stage: s.stage, name: s.name,
    defines: s.defines, vcolor: s.vcolorDecl })),
  dumped: dbg
};
fs.writeFileSync(path.resolve(process.env.OUTJSON || ".work/river_shader_report.json"), JSON.stringify(report, null, 2), "utf8");
console.log(JSON.stringify(report, null, 2));
process.exit(report.broken ? 1 : 0);
