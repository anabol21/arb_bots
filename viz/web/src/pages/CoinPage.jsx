import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import Plotly from "plotly.js-dist-min";
import createPlotlyComponent from "react-plotly.js/factory";
import {
  fetchCoins,
  fetchMeta,
  fetchOverviewCoin,
  fetchTicks,
  fromInputValue,
  toInputValue,
} from "../api.js";

const Plot = createPlotlyComponent(Plotly);

function defaultWindow(meta) {
  if (!meta?.span_end) {
    return { start: "2026-08-18T00:00:00Z", end: "2026-08-18T00:15:00Z" };
  }
  const end = meta.span_end;
  const endMs = Date.parse(end);
  const startMs = endMs - 15 * 60 * 1000;
  const start = new Date(startMs).toISOString().replace(/\.\d{3}Z$/, "Z");
  return { start, end };
}

function neighbors(coin, coinsPayload) {
  if (!coinsPayload) return { prev: null, next: null, group: null };
  const crypto = coinsPayload.crypto || [];
  const other = coinsPayload.other || [];
  let list = crypto.includes(coin) ? crypto : other.includes(coin) ? other : null;
  if (!list) return { prev: null, next: null, group: null };
  const i = list.indexOf(coin);
  return {
    prev: i > 0 ? list[i - 1] : null,
    next: i >= 0 && i < list.length - 1 ? list[i + 1] : null,
    group: crypto.includes(coin) ? "крипто" : "не крипто",
  };
}

function spreadTraces(data) {
  if (!data?.n && !data?.n_line && !data?.t?.length) return [];
  return [
    {
      x: data.t,
      y: data.spread_long,
      type: "scattergl",
      mode: "lines",
      name: "spread_long",
      line: { width: 1, color: "#1d4ed8" },
      connectgaps: false,
    },
    {
      x: data.t,
      y: data.spread_short,
      type: "scattergl",
      mode: "lines",
      name: "spread_short",
      line: { width: 1, color: "#b45309" },
      connectgaps: false,
    },
  ];
}

function midTrace(data) {
  if (!data?.t?.length) return [];
  return [
    {
      x: data.t,
      y: data.bybit_mid,
      type: "scattergl",
      mode: "lines",
      name: "bybit mid",
      line: { width: 1, color: "#57534e" },
      connectgaps: false,
    },
  ];
}

function chartLayout(title, yTitle, height = 420) {
  return {
    height,
    margin: { t: 40, r: 24, b: 40, l: 52 },
    paper_bgcolor: "#fffdf8",
    plot_bgcolor: "#fffdf8",
    title: { text: title, font: { size: 14 } },
    legend: { orientation: "h", y: 1.14 },
    xaxis: { title: "UTC", showgrid: true, gridcolor: "#e7e0d4" },
    yaxis: {
      title: yTitle,
      showgrid: true,
      gridcolor: "#e7e0d4",
      zeroline: true,
    },
    hovermode: "x unified",
  };
}

function StatsCards({ ov }) {
  if (!ov || !ov.n_all) return null;
  const fmt = (pcts, k) =>
    pcts && pcts[k] != null ? Number(pcts[k]).toFixed(4) : "—";
  return (
    <div className="stats-grid">
      <div className="stat">
        <span className="k">все тики</span>
        <span className="v">{ov.n_all}</span>
      </div>
      <div className="stat">
        <span className="k">на линии</span>
        <span className="v">{ov.n_line}</span>
      </div>
      <div className="stat">
        <span className="k">суток с тиками</span>
        <span className="v">{ov.days_with_ticks}</span>
      </div>
      <div className="stat">
        <span className="k">spread_long p50 / p95 / p99</span>
        <span className="v">
          {fmt(ov.pct_long, "50")} / {fmt(ov.pct_long, "95")} /{" "}
          {fmt(ov.pct_long, "99")}
        </span>
      </div>
      <div className="stat">
        <span className="k">spread_short p50 / p95 / p99</span>
        <span className="v">
          {fmt(ov.pct_short, "50")} / {fmt(ov.pct_short, "95")} /{" "}
          {fmt(ov.pct_short, "99")}
        </span>
      </div>
    </div>
  );
}

export default function CoinPage() {
  const { coin: raw } = useParams();
  const coin = String(raw || "").toUpperCase();
  const navigate = useNavigate();
  const [meta, setMeta] = useState(null);
  const [coins, setCoins] = useState(null);
  const [startIn, setStartIn] = useState("");
  const [endIn, setEndIn] = useState("");
  const [data, setData] = useState(null);
  const [overview, setOverview] = useState(null);
  const [err, setErr] = useState("");
  const [ovErr, setOvErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [ovLoading, setOvLoading] = useState(false);

  const nav = useMemo(() => neighbors(coin, coins), [coin, coins]);

  useEffect(() => {
    fetchMeta()
      .then((m) => {
        setMeta(m);
        const w = defaultWindow(m);
        setStartIn(toInputValue(w.start));
        setEndIn(toInputValue(w.end));
      })
      .catch((e) => setErr(String(e.message || e)));
    fetchCoins()
      .then(setCoins)
      .catch(() => {});
  }, []);

  useEffect(() => {
    let cancelled = false;
    setOverview(null);
    setOvErr("");
    setOvLoading(true);
    fetchOverviewCoin(coin)
      .then((ov) => {
        if (!cancelled) setOverview(ov);
      })
      .catch((e) => {
        if (!cancelled) setOvErr(String(e.message || e));
      })
      .finally(() => {
        if (!cancelled) setOvLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [coin]);

  async function buildOverviewLine() {
    setOvErr("");
    setOvLoading(true);
    try {
      const ov = await fetchOverviewCoin(coin, null, null, { compute: true });
      setOverview(ov);
    } catch (e) {
      setOvErr(String(e.message || e));
    } finally {
      setOvLoading(false);
    }
  }

  async function load() {
    setErr("");
    setLoading(true);
    setData(null);
    try {
      const start = fromInputValue(startIn);
      const end = fromInputValue(endIn);
      const payload = await fetchTicks(coin, start, end);
      setData(payload);
    } catch (e) {
      const detail = e.payload?.detail;
      if (detail && typeof detail === "object" && detail.error) {
        setErr(
          `${detail.error}${
            detail.n != null ? ` (n=${detail.n}, max=${detail.max_points})` : ""
          }`
        );
      } else {
        setErr(String(e.message || e));
      }
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (meta && startIn && endIn) {
      load();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meta, coin]);

  useEffect(() => {
    const onKey = (e) => {
      if (e.target && ["INPUT", "TEXTAREA"].includes(e.target.tagName)) return;
      if (e.key === "ArrowLeft" && nav.prev) {
        navigate(`/coin/${encodeURIComponent(nav.prev)}`);
      }
      if (e.key === "ArrowRight" && nav.next) {
        navigate(`/coin/${encodeURIComponent(nav.next)}`);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [nav, navigate]);

  return (
    <div className="wrap">
      <nav className="coin-nav">
        <Link className="nav-btn nav-home" to="/">
          ← К списку
        </Link>
        {nav.prev ? (
          <Link className="nav-btn" to={`/coin/${encodeURIComponent(nav.prev)}`}>
            ← {nav.prev}
          </Link>
        ) : (
          <span className="nav-disabled">←</span>
        )}
        <span className="here">
          <strong>{coin}</strong>
          {nav.group && <span className="meta"> · {nav.group}</span>}
        </span>
        {nav.next ? (
          <Link className="nav-btn" to={`/coin/${encodeURIComponent(nav.next)}`}>
            {nav.next} →
          </Link>
        ) : (
          <span className="nav-disabled">→</span>
        )}
        <span className="kbd">← → листают только внутри списка</span>
      </nav>
      <h1>{coin}</h1>

      <h2>Обзор всего покрытия (прореженно)</h2>
      <p className="note">
        Статы (p50/p95/p99) берутся из готового summary мгновенно. Линию по
        всем файлам не считаем автоматически (это часы) — только из кэша или по
        кнопке ниже.
      </p>
      {ovLoading && <p className="meta">Загрузка overview…</p>}
      {ovErr && <p className="error">{ovErr}</p>}
      {overview && (
        <>
          <p className="meta">
            {overview.start} → {overview.end} · source={overview.source || "—"} ·{" "}
            {overview.line_note}
          </p>
          <StatsCards ov={overview} />
          {(!overview.n_line || overview.n_line === 0) && (
            <div className="panel">
              <button
                type="button"
                onClick={buildOverviewLine}
                disabled={ovLoading}
              >
                {ovLoading
                  ? "Строю линию (долго)…"
                  : "Построить линию overview (медленный скан)"}
              </button>
              <p className="meta" style={{ marginTop: "0.45rem" }}>
                Один раз на монету; результат кэшируется в coin_pages.
              </p>
            </div>
          )}
          {overview.n_line > 0 && (
            <>
              <div className="chart-wrap">
                <Plot
                  data={spreadTraces(overview)}
                  layout={chartLayout(`${coin} — overview спред`, "spread pp")}
                  config={{ responsive: true, displayModeBar: true }}
                  style={{ width: "100%", height: "420px" }}
                  useResizeHandler
                />
              </div>
              <div className="chart-wrap">
                <Plot
                  data={midTrace(overview)}
                  layout={chartLayout(`${coin} — overview Bybit mid`, "price")}
                  config={{ responsive: true, displayModeBar: true }}
                  style={{ width: "100%", height: "360px" }}
                  useResizeHandler
                />
              </div>
            </>
          )}
        </>
      )}

      <h2>Все тики выбранного окна</h2>
      <p className="note">
        Без прореживания; окно &gt;300k тиков API отклонит явно.
      </p>
      <div className="panel">
        <label>
          START (UTC)
          <input
            type="datetime-local"
            value={startIn}
            onChange={(e) => setStartIn(e.target.value)}
          />
        </label>
        <label>
          END (UTC)
          <input
            type="datetime-local"
            value={endIn}
            onChange={(e) => setEndIn(e.target.value)}
          />
        </label>
        <button type="button" onClick={load} disabled={loading}>
          {loading ? "Загрузка…" : "Показать"}
        </button>
        {data && (
          <p className="meta" style={{ marginTop: "0.6rem" }}>
            n={data.n} · файлов={data.n_files} · {data.start} → {data.end}
          </p>
        )}
      </div>
      {err && <p className="error">{err}</p>}
      {data && data.n > 0 && (
        <>
          <div className="chart-wrap">
            <Plot
              data={spreadTraces({ ...data, n_line: data.n })}
              layout={chartLayout(`${coin} — все тики · спред`, "spread pp", 480)}
              config={{ responsive: true, displayModeBar: true }}
              style={{ width: "100%", height: "480px" }}
              useResizeHandler
            />
          </div>
          <div className="chart-wrap">
            <Plot
              data={midTrace(data)}
              layout={chartLayout(`${coin} — все тики · Bybit mid`, "price", 360)}
              config={{ responsive: true, displayModeBar: true }}
              style={{ width: "100%", height: "360px" }}
              useResizeHandler
            />
          </div>
        </>
      )}
      {data && data.n === 0 && !err && (
        <p className="meta">Нет тиков {coin} в выбранном окне.</p>
      )}
    </div>
  );
}
