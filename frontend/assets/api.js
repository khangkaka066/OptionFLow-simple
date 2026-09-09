import { apiUrl } from "./config.js";

const daySnapshotCache = new Map();
const intradaySnapshotCache = new Map();

export async function fetchDaySnapshot(ticker, dayId) {
  const key = ticker + "::" + dayId;
  if (!daySnapshotCache.has(key)) {
    daySnapshotCache.set(key, fetch(apiUrl("/api/snapshot?id=" + encodeURIComponent(dayId) + "&ticker=" + encodeURIComponent(ticker) + "&ts=" + Date.now()))
      .then(res => res.json())
      .then(payload => {
        if (payload.error) throw new Error(payload.error);
        return payload;
      })
      .catch(err => {
        daySnapshotCache.delete(key);
        throw err;
      }));
  }
  return daySnapshotCache.get(key);
}

export async function fetchIntradaySnapshot(ticker, tradingDate, force = false) {
  const key = ticker + "::intraday::" + tradingDate;
  if (force) intradaySnapshotCache.delete(key);
  if (!intradaySnapshotCache.has(key)) {
    intradaySnapshotCache.set(key, fetch(apiUrl("/api/intraday?date=" + encodeURIComponent(tradingDate) + "&ticker=" + encodeURIComponent(ticker) + "&ts=" + Date.now()))
      .then(res => res.json())
      .then(payload => {
        if (payload.error) throw new Error(payload.error);
        return payload;
      })
      .catch(err => {
        intradaySnapshotCache.delete(key);
        throw err;
      }));
  }
  return intradaySnapshotCache.get(key);
}


export async function fetchGreekSurface({ticker, date, greek, mode, range, force = false}) {
  const params = new URLSearchParams();
  params.set("ticker", ticker || "QQQ");
  if (date) params.set("date", date);
  params.set("greek", greek || "gex");
  params.set("mode", mode || "net");
  if (range) params.set("range", range);
  if (force) params.set("refresh", "1");
  params.set("ts", String(Date.now()));
  const res = await fetch(apiUrl("/api/greek-surface?" + params.toString()));
  const payload = await res.json();
  if (payload.error) throw new Error(payload.error);
  return payload;
}
