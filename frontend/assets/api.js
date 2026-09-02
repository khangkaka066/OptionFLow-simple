const daySnapshotCache = new Map();
const intradaySnapshotCache = new Map();

export async function fetchDaySnapshot(ticker, dayId) {
  const key = ticker + "::" + dayId;
  if (!daySnapshotCache.has(key)) {
    daySnapshotCache.set(key, fetch("/api/snapshot?id=" + encodeURIComponent(dayId) + "&ticker=" + encodeURIComponent(ticker) + "&ts=" + Date.now())
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
    intradaySnapshotCache.set(key, fetch("/api/intraday?date=" + encodeURIComponent(tradingDate) + "&ticker=" + encodeURIComponent(ticker) + "&ts=" + Date.now())
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
