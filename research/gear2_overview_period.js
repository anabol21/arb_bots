/* Period control: load every tick in a UTC window from the local overview server.
 * file:// — button stays disabled; http(s) — GET /api/ticks and Plotly.react.
 */
(function () {
  "use strict";

  var FILE_NOTE =
    "полный ряд тиков нужен локальный сервер: `./venv/bin/python research/gear2_overview_server.py`";

  function $(id) {
    return document.getElementById(id);
  }

  function isHttp() {
    return location.protocol === "http:" || location.protocol === "https:";
  }

  function readConfig() {
    var el = $("gear2-overview-config");
    var cfg = {};
    if (el && el.textContent) {
      try {
        cfg = JSON.parse(el.textContent);
      } catch (e) {
        cfg = {};
      }
    }
    var h1 = document.querySelector("h1");
    if (!cfg.coin && h1) cfg.coin = h1.textContent.trim();
    cfg.maxPoints = cfg.maxPoints || 300000;
    return cfg;
  }

  function pad(n) {
    return n < 10 ? "0" + n : String(n);
  }

  function toDatetimeLocal(iso) {
    if (!iso) return "";
    var s = String(iso).replace("Z", "").replace("+00:00", "");
    if (s.length >= 19) return s.slice(0, 19);
    if (s.length >= 16) return s.slice(0, 16);
    return s;
  }

  function fromDatetimeLocal(val) {
    if (!val) return "";
    var s = String(val).trim();
    if (!s) return "";
    if (/Z|[+-]\d\d:\d\d$/.test(s)) return s;
    if (s.length === 16) s += ":00";
    return s + "Z";
  }

  function parseUtcMs(iso) {
    var s = fromDatetimeLocal(iso);
    var t = Date.parse(s);
    return isNaN(t) ? null : t;
  }

  function msToLocalValue(ms) {
    var d = new Date(ms);
    return (
      d.getUTCFullYear() +
      "-" +
      pad(d.getUTCMonth() + 1) +
      "-" +
      pad(d.getUTCDate()) +
      "T" +
      pad(d.getUTCHours()) +
      ":" +
      pad(d.getUTCMinutes()) +
      ":" +
      pad(d.getUTCSeconds())
    );
  }

  function setStatus(text, isErr) {
    var el = $("period-status");
    if (!el) return;
    el.textContent = text || "";
    el.className = isErr ? "note period-err" : "meta";
  }

  function init() {
    var panel = $("period-panel");
    if (!panel) return;
    var cfg = readConfig();
    var startIn = $("period-start");
    var endIn = $("period-end");
    var loadBtn = $("period-load");
    var fileNote = $("period-file-note");
    var httpOk = isHttp();

    if (startIn && cfg.defaultStart && !startIn.value) {
      startIn.value = toDatetimeLocal(cfg.defaultStart);
    }
    if (endIn && cfg.defaultEnd && !endIn.value) {
      endIn.value = toDatetimeLocal(cfg.defaultEnd);
    }
    if (startIn && cfg.calendarStart) startIn.min = toDatetimeLocal(cfg.calendarStart);
    if (endIn && cfg.calendarStart) endIn.min = toDatetimeLocal(cfg.calendarStart);
    if (startIn && cfg.calendarEnd) startIn.max = toDatetimeLocal(cfg.calendarEnd);
    if (endIn && cfg.calendarEnd) endIn.max = toDatetimeLocal(cfg.calendarEnd);

    if (!httpOk) {
      if (fileNote) {
        fileNote.hidden = false;
        fileNote.textContent = FILE_NOTE;
      }
      if (loadBtn) {
        loadBtn.disabled = true;
        loadBtn.title = FILE_NOTE;
      }
      panel.querySelectorAll(".period-preset").forEach(function (b) {
        b.disabled = true;
      });
      setStatus("Открыто как file:// — полный ряд недоступен без сервера.", true);
      return;
    }
    if (fileNote) fileNote.hidden = true;
    if (loadBtn) loadBtn.disabled = false;

    panel.querySelectorAll(".period-preset").forEach(function (b) {
      b.addEventListener("click", function () {
        var ms = parseInt(b.getAttribute("data-ms"), 10);
        if (!ms) return;
        var endMs = parseUtcMs(endIn && endIn.value);
        if (endMs == null && cfg.defaultEnd) endMs = parseUtcMs(cfg.defaultEnd);
        if (endMs == null) return;
        var startMs = endMs - ms;
        var cal0 = cfg.calendarStart ? parseUtcMs(cfg.calendarStart) : null;
        if (cal0 != null && startMs < cal0) startMs = cal0;
        if (startIn) startIn.value = msToLocalValue(startMs);
        if (endIn) endIn.value = msToLocalValue(endMs);
      });
    });

    function loadTicks() {
      var coin = cfg.coin;
      var start = fromDatetimeLocal(startIn && startIn.value);
      var end = fromDatetimeLocal(endIn && endIn.value);
      if (!coin || !start || !end) {
        setStatus("Задайте монету и интервал UTC.", true);
        return;
      }
      if (loadBtn) loadBtn.disabled = true;
      setStatus("Загрузка всех тиков…");
      var url =
        "/api/ticks?coin=" +
        encodeURIComponent(coin) +
        "&start=" +
        encodeURIComponent(start) +
        "&end=" +
        encodeURIComponent(end);
      fetch(url)
        .then(function (res) {
          return res.json().then(function (body) {
            return { okHttp: res.ok, body: body };
          });
        })
        .then(function (pack) {
          var body = pack.body || {};
          if (!body.ok) {
            setStatus(body.error || "Сервер отказал в полном ряде.", true);
            return;
          }
          var fig = body.figure;
          var div = $("period-plot");
          if (!div || typeof Plotly === "undefined") {
            setStatus("Plotly не загрузился — график окна недоступен.", true);
            return;
          }
          if (!fig || !fig.data) {
            setStatus("Пустой ответ без фигуры.", true);
            return;
          }
          return Plotly.react(div, fig.data, fig.layout || {}, {
            responsive: true,
          }).then(function () {
            var dropped = (body.n_parquet || 0) - (body.n_plot || 0);
            var extra =
              dropped > 0
                ? " отфильтровано L1 (пустой/неположительный стакан): " + dropped + "."
                : " совпадает с parquet после фильтра времени/монеты (L1 ничего не отбросил).";
            setStatus(
              "все тики окна: " +
                body.n_plot +
                "  ·  строк parquet: " +
                body.n_parquet +
                "  ·  файлов: " +
                (body.n_files || "?") +
                "." +
                extra +
                " Без прореживания."
            );
          });
        })
        .catch(function (err) {
          setStatus(
            "Не удалось запросить /api/ticks (" +
              (err && err.message ? err.message : err) +
              "). " +
              FILE_NOTE,
            true
          );
        })
        .then(function () {
          if (loadBtn) loadBtn.disabled = false;
        });
    }

    if (loadBtn) loadBtn.addEventListener("click", loadTicks);

    fetch("/api/meta")
      .then(function (r) {
        return r.json();
      })
      .then(function (meta) {
        if (!meta || !meta.ok) return;
        if (meta.max_points) cfg.maxPoints = meta.max_points;
        if (startIn && meta.default_start) startIn.value = toDatetimeLocal(meta.default_start);
        if (endIn && meta.default_end) endIn.value = toDatetimeLocal(meta.default_end);
      })
      .catch(function () {})
      .then(function () {
        loadTicks();
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
