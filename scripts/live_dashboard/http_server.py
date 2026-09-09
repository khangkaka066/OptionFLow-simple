from __future__ import annotations

import gzip
import json
import mimetypes
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# Below this size gzip's own overhead isn't worth the CPU time.
_GZIP_MIN_BYTES = 512

from .cache import ResponseCache
from .greek_surface_service import GreekSurfaceService
from .intraday_service import IntradayService
from .snapshot_service import SnapshotService
from .web_assets import read_index_html, resolve_asset_path


class DashboardRequestHandler(BaseHTTPRequestHandler):
    state = None
    ticker: str = "QQQ"
    window: float = 14.0
    snapshot_service: SnapshotService | None = None
    intraday_service: IntradayService | None = None
    greek_surface_service: GreekSurfaceService | None = None
    apply_secondary_basis = None
    response_cache = ResponseCache(max_entries=64)
    history_cache_ttl_seconds: float = 120.0
    snapshot_cache_ttl_seconds: float = 120.0
    intraday_cache_ttl_seconds: float = 45.0
    cors_allowed_origin: str = "*"

    def log_message(self, fmt: str, *args) -> None:
        return

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send(read_index_html(), "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/assets/"):
            self.send_static_asset(parsed.path)
            return
        if parsed.path == "/api/state":
            self.send_json_payload(self.state.snapshot())
            return
        if parsed.path == "/api/history":
            started = time.perf_counter()
            ticker = parse_qs(parsed.query).get("ticker", [self.ticker])[0] or self.ticker
            cache_key = ("history", ticker.upper())
            cached = self.response_cache.get(cache_key, self.history_cache_ttl_seconds)
            cache_status = "hit" if cached is not None else "miss"
            if cached is None:
                cached = self.json_bytes({"snapshots": self.snapshot_service.list_trading_days(ticker)})
                self.response_cache.set(cache_key, cached)
            self.send_bytes(cached, "application/json")
            self.log_api_timing("history", started=started, ticker=ticker, cache=cache_status, size=len(cached))
            return
        if parsed.path == "/api/snapshot":
            started = time.perf_counter()
            query = parse_qs(parsed.query)
            ticker = query.get("ticker", [self.ticker])[0] or self.ticker
            snapshot_id = query.get("id", [""])[0]
            refresh = query.get("refresh", ["0"])[0] == "1"
            cache_key = (
                ticker.upper(),
                snapshot_id,
                float(self.window),
                self.secondary_basis_cache_key(),
            )
            cached = None if refresh else self.response_cache.get(cache_key, self.snapshot_cache_ttl_seconds)
            cache_status = "refresh" if refresh else ("hit" if cached is not None else "miss")
            try:
                if cached is None:
                    payload = self.snapshot_service.load_snapshot_state(snapshot_id, ticker, self.window, refresh=refresh)
                    if self.apply_secondary_basis:
                        self.apply_secondary_basis(payload, ticker)
                    cached = self.json_bytes(payload)
                    self.response_cache.set(cache_key, cached)
                self.send_bytes(cached, "application/json")
                self.log_api_timing(
                    "snapshot",
                    started=started,
                    ticker=ticker,
                    snapshot_id=snapshot_id,
                    cache=cache_status,
                    size=len(cached),
                )
            except Exception as exc:
                self.response_cache.delete(cache_key)
                error = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_bytes(error, "application/json", HTTPStatus.BAD_REQUEST)
                self.log_api_timing(
                    "snapshot",
                    started=started,
                    ticker=ticker,
                    snapshot_id=snapshot_id,
                    cache="error",
                    size=len(error),
                )
            return
        if parsed.path == "/api/stream":
            query = parse_qs(parsed.query)
            panels = {item.strip() for item in (query.get("panels", ["greek_surface"])[0] or "").split(",") if item.strip()}
            if "greek_surface" not in panels:
                self.send_bytes(b"event: heartbeat\ndata: {}\n\n", "text/event-stream")
                return
            ticker = query.get("ticker", query.get("tickers", [self.ticker]))[0] or self.ticker
            trading_date = query.get("date", [""])[0] or None
            greek = query.get("greek", ["gex"])[0] or "gex"
            mode = query.get("mode", ["net"])[0] or "net"
            strike_range = self.float_query(query, "range")
            dte_max_raw = self.float_query(query, "dte_max")
            dte_max = int(dte_max_raw) if dte_max_raw is not None else None
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_cors_headers()
            self.end_headers()
            last_payload_key = None
            try:
                while True:
                    try:
                        payload = self.greek_surface_service.load_surface(
                            ticker=ticker,
                            trading_date=trading_date,
                            greek=greek,
                            mode=mode,
                            strike_range=strike_range,
                            dte_max=dte_max,
                        )
                    except (FileNotFoundError, ValueError) as exc:
                        self.write_sse("error", {"error": str(exc)})
                        self.write_sse("heartbeat", {"ts": time.time()})
                        time.sleep(30)
                        continue
                    payload_key = (
                        payload.get("ticker"),
                        payload.get("snapshot_utc"),
                        payload.get("greek"),
                        payload.get("mode"),
                        payload.get("rawMaxAbs"),
                        len(payload.get("strikes") or []),
                        len(payload.get("expiries") or []),
                    )
                    if payload_key != last_payload_key:
                        self.write_sse("greek_surface", payload)
                        last_payload_key = payload_key
                    self.write_sse("heartbeat", {"ts": time.time()})
                    time.sleep(30)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        if parsed.path == "/api/greek-surface":
            started = time.perf_counter()
            query = parse_qs(parsed.query)
            ticker = query.get("ticker", [self.ticker])[0] or self.ticker
            trading_date = query.get("date", [""])[0] or None
            greek = query.get("greek", ["gex"])[0] or "gex"
            mode = query.get("mode", ["net"])[0] or "net"
            refresh = query.get("refresh", ["0"])[0] == "1"
            strike_range = self.float_query(query, "range")
            dte_max_raw = self.float_query(query, "dte_max")
            dte_max = int(dte_max_raw) if dte_max_raw is not None else None
            try:
                payload = self.greek_surface_service.load_surface(
                    ticker=ticker,
                    trading_date=trading_date,
                    greek=greek,
                    mode=mode,
                    strike_range=strike_range,
                    dte_max=dte_max,
                    refresh=refresh,
                )
                body = self.json_bytes(payload)
                self.send_bytes(body, "application/json")
                self.log_api_timing(
                    "greek_surface",
                    started=started,
                    ticker=ticker,
                    trading_date=trading_date,
                    greek=greek,
                    mode=mode,
                    size=len(body),
                )
            except Exception as exc:
                error = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_bytes(error, "application/json", HTTPStatus.BAD_REQUEST)
                self.log_api_timing(
                    "greek_surface",
                    started=started,
                    ticker=ticker,
                    trading_date=trading_date,
                    greek=greek,
                    mode=mode,
                    size=len(error),
                )
            return
        if parsed.path == "/api/intraday":
            started = time.perf_counter()
            query = parse_qs(parsed.query)
            ticker = query.get("ticker", [self.ticker])[0] or self.ticker
            trading_date = query.get("date", [""])[0]
            refresh = query.get("refresh", ["0"])[0] == "1"
            cache_key = (
                ticker.upper(),
                trading_date,
                float(self.window),
                self.secondary_basis_cache_key(),
            )
            cached = None if refresh else self.response_cache.get(cache_key, self.intraday_cache_ttl_seconds)
            cache_status = "refresh" if refresh else ("hit" if cached is not None else "miss")
            try:
                if cached is None:
                    payload = self.intraday_service.load_state(trading_date, ticker, self.window, refresh=refresh)
                    if self.apply_secondary_basis:
                        self.apply_secondary_basis(payload, ticker)
                    cached = self.json_bytes(payload)
                    self.response_cache.set(cache_key, cached)
                self.send_bytes(cached, "application/json")
                self.log_api_timing(
                    "intraday",
                    started=started,
                    ticker=ticker,
                    trading_date=trading_date,
                    cache=cache_status,
                    size=len(cached),
                )
            except Exception as exc:
                self.response_cache.delete(cache_key)
                error = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_bytes(error, "application/json", HTTPStatus.BAD_REQUEST)
                self.log_api_timing(
                    "intraday",
                    started=started,
                    ticker=ticker,
                    trading_date=trading_date,
                    cache="error",
                    size=len(error),
                )
            return
        self.send("not found", "text/plain", HTTPStatus.NOT_FOUND)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def log_api_timing(self, endpoint: str, *, started: float, size: int = 0, **fields) -> None:
        elapsed_ms = (time.perf_counter() - started) * 1000
        parts = [f"{key}={value}" for key, value in fields.items() if value not in (None, "")]
        parts.append(f"size={size}")
        parts.append(f"ms={elapsed_ms:.1f}")
        print(f"[perf] api.{endpoint} " + " ".join(parts), flush=True)

    def write_sse(self, event: str, payload: dict) -> None:
        message = f"event: {event}\ndata: {json.dumps(payload, default=str, allow_nan=False)}\n\n"
        self.wfile.write(message.encode("utf-8"))
        self.wfile.flush()

    def float_query(self, query: dict, key: str) -> float | None:
        value = query.get(key, [None])[0]
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def json_bytes(self, payload: dict) -> bytes:
        return json.dumps(payload, default=str, allow_nan=False).encode("utf-8")

    def send_json_payload(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_bytes(self.json_bytes(payload), "application/json", status)

    def secondary_basis_cache_key(self) -> str:
        return str(getattr(self.state, "secondary_futures_ticker", "") or "")

    def send_static_asset(self, request_path: str) -> None:
        asset_path = resolve_asset_path(request_path)
        if asset_path is None:
            self.send("not found", "text/plain", HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(asset_path.name)[0] or "application/octet-stream"
        self.send_bytes(asset_path.read_bytes(), content_type)

    def send(self, body: str, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_bytes(body.encode("utf-8"), content_type, status)

    def send_bytes(self, data: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoding = None
        if len(data) >= _GZIP_MIN_BYTES and "gzip" in self.headers.get("Accept-Encoding", ""):
            compressed = gzip.compress(data, compresslevel=6)
            if len(compressed) < len(data):
                data = compressed
                encoding = "gzip"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_cors_headers()
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_cors_headers(self) -> None:
        if not self.cors_allowed_origin:
            return
        self.send_header("Access-Control-Allow-Origin", self.cors_allowed_origin)
        self.send_header("Vary", "Origin")


def configure_handler(
    *,
    state,
    ticker: str,
    window: float,
    snapshot_service: SnapshotService,
    intraday_service: IntradayService,
    greek_surface_service: GreekSurfaceService,
    apply_secondary_basis,
    cors_allowed_origin: str = "*",
) -> type[DashboardRequestHandler]:
    class ConfiguredDashboardRequestHandler(DashboardRequestHandler):
        pass

    ConfiguredDashboardRequestHandler.state = state
    ConfiguredDashboardRequestHandler.ticker = ticker
    ConfiguredDashboardRequestHandler.window = window
    ConfiguredDashboardRequestHandler.snapshot_service = snapshot_service
    ConfiguredDashboardRequestHandler.intraday_service = intraday_service
    ConfiguredDashboardRequestHandler.greek_surface_service = greek_surface_service
    ConfiguredDashboardRequestHandler.apply_secondary_basis = staticmethod(apply_secondary_basis)
    ConfiguredDashboardRequestHandler.cors_allowed_origin = cors_allowed_origin
    ConfiguredDashboardRequestHandler.response_cache = ResponseCache(max_entries=64)
    return ConfiguredDashboardRequestHandler
