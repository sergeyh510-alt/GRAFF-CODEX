/* GRAFF loader.
 *
 * Loads vis-network FIRST, sequentially: the bundled local copy (if present),
 * then a CDN as a fallback.  app.js is started only after the library has
 * actually loaded, so the page never races the <script> tags.  Reasonable
 * diagnostic is written into #missing-vis when every source fails, so the user
 * can tell a 404 from a bad MIME type from a blocked CDN.
 */
(function () {
  "use strict";

  // Bump to invalidate a previously cached (possibly broken) copy.
  var VERSION = "10.1.2";
  var SOURCES = [
    { name: "локальная копия", url: "/static/vendor/vis-network.min.js?v=" + VERSION },
    { name: "CDN cdnjs", url: "https://cdnjs.cloudflare.com/ajax/libs/vis-network/" + VERSION + "/standalone/umd/vis-network.min.js" },
    { name: "CDN unpkg", url: "https://unpkg.com/vis-network/standalone/umd/vis-network.min.js" }
  ];

  var log = [];

  function shortUrl(url) {
    try {
      return new URL(url, location.href).pathname + (new URL(url, location.href).search || "");
    } catch (e) {
      return url;
    }
  }

  function record(name, url, detail) {
    log.push("• " + name + " — " + shortUrl(url) + " → " + detail);
  }

  function libReady() {
    return !!(window.vis && window.vis.Network);
  }

  function startApp() {
    // The river mode ships as its own file next to app.js.  It is a plain
    // module (window.GRAFFRiver) with no dependencies of its own, so loading it
    // before app.js is enough; a failure here must not stop the other two
    // views, which is why the error is only recorded.
    var extra = document.createElement("script");
    extra.src = "/static/js/river.js?v=" + VERSION;
    extra.async = false;
    extra.onerror = function () {
      record("river.js", extra.src, "ошибка загрузки (режим «Русло» будет недоступен)");
    };
    document.body.appendChild(extra);

    var s = document.createElement("script");
    s.src = "/static/js/app.js";
    s.async = false;
    document.body.appendChild(s);
  }

  function showFailure() {
    var box = document.getElementById("missing-vis");
    var pre = document.getElementById("missing-vis-log");
    if (pre) {
      pre.textContent = log.join("\n") + "\n\nДиагностика: проверьте, что сервер отдаёт " +
        "vis-network.min.js со статусом 200 и content-type «text/javascript» " +
        "(а не text/plain), и что локальный файл не закеширован как 404 " +
        "(в адресе есть ?v=" + VERSION + ", это должно помочь).";
    }
    if (box) box.hidden = false;
    var retry = document.getElementById("missing-vis-retry");
    if (retry) {
      retry.addEventListener("click", function () {
        if (box) box.hidden = true;
        log = [];
        tryLoad(0);
      });
    }
  }

  function tryLoad(index) {
    if (libReady()) {
      record("готово", SOURCES[Math.min(index, SOURCES.length - 1)].url, "window.vis.Network доступен");
      startApp();
      return;
    }
    if (index >= SOURCES.length) {
      showFailure();
      return;
    }
    var source = SOURCES[index];
    var script = document.createElement("script");
    script.src = source.url;
    script.async = false;
    script.onload = function () {
      if (libReady()) {
        record(source.name, source.url, "200, vis.Network доступен");
        startApp();
      } else {
        record(source.name, source.url, "200, но window.vis.Network не появился");
        tryLoad(index + 1);
      }
    };
    script.onerror = function () {
      record(source.name, source.url, "ошибка загрузки (404/MIME/CSP/сеть)");
      tryLoad(index + 1);
    };
    document.head.appendChild(script);
  }

  tryLoad(0);
})();
