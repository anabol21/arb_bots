import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  fetchCoins,
  fetchCoverage,
  fetchMeta,
  fetchOverviewSummary,
} from "../api.js";

function CoinGrid({ coins }) {
  return (
    <div className="coin-grid">
      {coins.map((c) => (
        <Link key={c} className="coin-chip" to={`/coin/${encodeURIComponent(c)}`}>
          {c}
        </Link>
      ))}
    </div>
  );
}

function StatsTable({ rows, title, id }) {
  if (!rows?.length) return null;
  return (
    <>
      <div className="section-head" id={id}>
        <h2>{title}</h2>
        <span className="count-pill">{rows.length}</span>
      </div>
      <p className="meta">Соседние страницы листают только этот список.</p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>монета</th>
              <th>класс</th>
              <th>все тики</th>
              <th>на линии</th>
              <th>суток&gt;0</th>
              <th>сутки без тиков</th>
              <th>long p50/p95/p99</th>
              <th>short p50/p95/p99</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.coin}>
                <td>
                  <Link to={`/coin/${encodeURIComponent(r.coin)}`}>{r.coin}</Link>
                </td>
                <td>{r.klass}</td>
                <td>{r.n_all}</td>
                <td>{r.n_line}</td>
                <td>{r.days_with_ticks}</td>
                <td>{r.days_missing || "—"}</td>
                <td>
                  <code>{r.long_pct}</code>
                </td>
                <td>
                  <code>{r.short_pct}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

export default function Home() {
  const [meta, setMeta] = useState(null);
  const [coins, setCoins] = useState(null);
  const [coverage, setCoverage] = useState(null);
  const [summary, setSummary] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [m, c, cov, sum] = await Promise.all([
          fetchMeta(),
          fetchCoins(),
          fetchCoverage(),
          fetchOverviewSummary().catch(() => null),
        ]);
        if (cancelled) return;
        setMeta(m);
        setCoins(c);
        setCoverage(cov);
        setSummary(sum);
      } catch (e) {
        if (!cancelled) setErr(String(e.message || e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const cryptoRows = summary?.rows?.filter((r) => r.klass === "крипто") || [];
  const otherRows = summary?.rows?.filter((r) => r.klass !== "крипто") || [];
  const hasTable = summary?.ok && summary?.rows?.length > 0;

  return (
    <div className="wrap">
      <h1>Обзор спредов по монетам</h1>
      <p className="lede">
        Локальный кэш lean ticks (SoT — backup). На странице монеты: прореженный
        обзор всего покрытия + все тики выбранного окна (без silent downsample).
      </p>
      {loading && <p className="meta">Загрузка…</p>}
      {err && <p className="error">{err}</p>}
      {meta && (
        <p className="meta">
          Файлов в кэше: <strong>{meta.n_files}</strong>
          {meta.span_start && (
            <>
              {" "}
              · покрытие {meta.span_start} → {meta.span_end}
            </>
          )}
          {" "}
          · max тиков на all-ticks окно: {meta.max_points}
        </p>
      )}
      {coverage && coverage.n_holes > 0 && (
        <div className="legend-topn">
          <div>
            <strong>Дыры календаря (5м слоты):</strong> {coverage.n_holes} run
            {coverage.holes?.[0] && (
              <p className="note" style={{ marginTop: "0.35rem" }}>
                Первые:{" "}
                {coverage.holes.slice(0, 3).map((h) => (
                  <span key={h.start}>
                    {h.start}→{h.end} ({h.duration_min} мин);{" "}
                  </span>
                ))}
              </p>
            )}
          </div>
        </div>
      )}
      {hasTable ? (
        <>
          <p className="meta">
            Сводка overview: {summary.start} → {summary.end} · монет=
            {summary.n_coins} · собрано {summary.built_at} ({summary.elapsed_s} с).
            p50/p95/p99 — по <strong>всем</strong> тикам; линия ≤{summary.max_line}{" "}
            точек.
          </p>
          <StatsTable rows={cryptoRows} title="Крипто" id="crypto" />
          <StatsTable rows={otherRows} title="Не крипто" id="non-crypto" />
        </>
      ) : (
        <>
          <div className="legend-topn">
            <div>
              <strong>Таблица статов ещё не собрана.</strong>
              <p className="note" style={{ marginTop: "0.35rem" }}>
                Запустите{" "}
                <code>./venv/bin/python -m viz.build_overview</code> (один проход
                по кэшу, долго). Пока доступны чипы монет:
              </p>
              {summary?.error && <p className="note">{summary.error}</p>}
            </div>
          </div>
          {coins && (
            <>
              <div className="section-head" id="crypto">
                <h2>Крипто</h2>
                <span className="count-pill">{coins.n_crypto}</span>
              </div>
              <CoinGrid coins={coins.crypto} />
              <div className="section-head" id="non-crypto">
                <h2>Не крипто</h2>
                <span className="count-pill">{coins.n_other}</span>
              </div>
              <CoinGrid coins={coins.other} />
            </>
          )}
        </>
      )}
      <p className="note">
        Сервис на Mac. Sync: <code>python -m viz.sync_from_backup</code>. Не
        деплоить на VPS рядом со сборщиком.
      </p>
    </div>
  );
}
