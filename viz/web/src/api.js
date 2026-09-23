async function apiGet(path) {
  const res = await fetch(path);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg =
      (data && (data.detail?.error || data.detail || data.error)) ||
      res.statusText ||
      "request failed";
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    err.status = res.status;
    err.payload = data;
    throw err;
  }
  return data;
}

export function fetchMeta() {
  return apiGet("/api/meta");
}

export function fetchCoins(refresh = false) {
  const q = refresh ? "?refresh=1" : "";
  return apiGet(`/api/coins${q}`);
}

export function fetchCoverage(start, end) {
  const sp = new URLSearchParams();
  if (start) sp.set("start", start);
  if (end) sp.set("end", end);
  const q = sp.toString();
  return apiGet(`/api/coverage${q ? `?${q}` : ""}`);
}

export function fetchTicks(coin, start, end) {
  const sp = new URLSearchParams({ coin, start, end });
  return apiGet(`/api/ticks?${sp}`);
}

export function fetchOverviewSummary() {
  return apiGet("/api/overview/summary");
}

export function fetchOverviewCoin(coin, start, end, { compute = false, refresh = false } = {}) {
  const sp = new URLSearchParams({ coin });
  if (start) sp.set("start", start);
  if (end) sp.set("end", end);
  if (compute) sp.set("compute", "1");
  if (refresh) sp.set("refresh", "1");
  return apiGet(`/api/overview/coin?${sp}`);
}

export function toInputValue(iso) {
  if (!iso) return "";
  return String(iso).replace("Z", "").slice(0, 16);
}

export function fromInputValue(local) {
  if (!local) return "";
  const s = String(local).trim();
  if (s.endsWith("Z")) return s;
  if (s.length === 16) return `${s}:00Z`;
  if (s.length === 19) return `${s}Z`;
  return `${s}Z`;
}
