from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .web_assets import read_index_html, resolve_asset_path


class DashboardRequestHandler(BaseHTTPRequestHandler):
    state = None
    ticker: str = "QQQ"
    window: float = 14.0
    list_trading_days: Callable[[str], list[dict]] | None = None
    load_snapshot_state: Callable[[str, str, float], dict] | None = None
    load_intraday_state: Callable[[str, str, float], dict] | None = None
    apply_secondary_basis: Callable[[dict, str], None] | None = None

    def log_message(self, fmt: str, *args) -> None:
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
            self.send(json.dumps(self.state.snapshot(), default=str, allow_nan=False), "application/json")
            return
        if parsed.path == "/api/history":
            ticker = parse_qs(parsed.query).get("ticker", [self.ticker])[0] or self.ticker
            payload = {"snapshots": self.list_trading_days(ticker)}
            self.send(json.dumps(payload, default=str, allow_nan=False), "application/json")
            return
        if parsed.path == "/api/snapshot":
            ticker = parse_qs(parsed.query).get("ticker", [self.ticker])[0] or self.ticker
            snapshot_id = parse_qs(parsed.query).get("id", [""])[0]
            try:
                payload = self.load_snapshot_state(snapshot_id, ticker, self.window)
                if self.apply_secondary_basis:
                    self.apply_secondary_basis(payload, ticker)
                self.send(json.dumps(payload, default=str, allow_nan=False), "application/json")
            except Exception as exc:
                self.send(json.dumps({"error": str(exc)}), "application/json", HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/intraday":
            ticker = parse_qs(parsed.query).get("ticker", [self.ticker])[0] or self.ticker
            trading_date = parse_qs(parsed.query).get("date", [""])[0]
            try:
                payload = self.load_intraday_state(trading_date, ticker, self.window)
                self.send(json.dumps(payload, default=str, allow_nan=False), "application/json")
            except Exception as exc:
                self.send(json.dumps({"error": str(exc)}), "application/json", HTTPStatus.BAD_REQUEST)
            return
        self.send("not found", "text/plain", HTTPStatus.NOT_FOUND)

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
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def configure_handler(
    *,
    state,
    ticker: str,
    window: float,
    list_trading_days: Callable[[str], list[dict]],
    load_snapshot_state: Callable[[str, str, float], dict],
    load_intraday_state: Callable[[str, str, float], dict],
    apply_secondary_basis: Callable[[dict, str], None],
) -> type[DashboardRequestHandler]:
    class ConfiguredDashboardRequestHandler(DashboardRequestHandler):
        pass

    ConfiguredDashboardRequestHandler.state = state
    ConfiguredDashboardRequestHandler.ticker = ticker
    ConfiguredDashboardRequestHandler.window = window
    ConfiguredDashboardRequestHandler.list_trading_days = staticmethod(list_trading_days)
    ConfiguredDashboardRequestHandler.load_snapshot_state = staticmethod(load_snapshot_state)
    ConfiguredDashboardRequestHandler.load_intraday_state = staticmethod(load_intraday_state)
    ConfiguredDashboardRequestHandler.apply_secondary_basis = staticmethod(apply_secondary_basis)
    return ConfiguredDashboardRequestHandler
