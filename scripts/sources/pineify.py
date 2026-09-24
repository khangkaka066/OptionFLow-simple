"""Fetch volume/open-interest supplement data from Pineify's public options-chain page.

Passive/public only: renders `https://pineify.app/options-chain` (a page
`robots.txt` explicitly allows, `Allow: /`) with a headless browser and reads
the on-page table already shown to any visitor. Pineify's `/api/*` paths are
disallowed by `robots.txt` (`Disallow: /api/`) and must never be called from
this module. No login, no CAPTCHA bypass, no polling faster than the
pipeline's own cadence (one fetch per pipeline run/cycle, not Pineify's own
~30s in-page auto-refresh).

Playwright is required here (unlike `optionwatch.py`) because Pineify has no
equivalent public JSON API — the table is rendered client-side after the
ticker is submitted.
"""

from __future__ import annotations

import re

import pandas as pd
from playwright.sync_api import sync_playwright

PINEIFY_URL = "https://pineify.app/options-chain"
EXPECTED_ROW_CELLS = 15

CHAIN_COLUMNS = ["strike", "option_type", "pf_volume", "pf_oi"]

_NUMBER_RE = re.compile(r"^-?[\d,.]+[KMB]?$")


def _parse_number(text: str) -> float | None:
    text = text.strip()
    if not text or text == "-":
        return None
    match = _NUMBER_RE.match(text)
    if not match:
        return None
    multiplier = 1.0
    if text[-1] in ("K", "M", "B"):
        multiplier = {"K": 1_000.0, "M": 1_000_000.0, "B": 1_000_000_000.0}[text[-1]]
        text = text[:-1]
    try:
        return float(text.replace(",", "")) * multiplier
    except ValueError:
        return None


def _extract_rows(page) -> list[list[str]]:
    table = page.locator("table").first
    rows = table.locator("tr")
    count = rows.count()
    data_rows = []
    for i in range(count):
        cells = rows.nth(i).locator("td")
        n = cells.count()
        if n != EXPECTED_ROW_CELLS:
            continue
        data_rows.append([cells.nth(j).inner_text().strip() for j in range(n)])
    return data_rows


def _rows_to_frame(rows: list[list[str]]) -> pd.DataFrame:
    out = []
    for cells in rows:
        strike = _parse_number(cells[7])
        if strike is None:
            continue
        call_vol = _parse_number(cells[3])
        call_oi = _parse_number(cells[4])
        put_vol = _parse_number(cells[11])
        put_oi = _parse_number(cells[12])
        out.append({"strike": strike, "option_type": "call", "pf_volume": call_vol, "pf_oi": call_oi})
        out.append({"strike": strike, "option_type": "put", "pf_volume": put_vol, "pf_oi": put_oi})
    return pd.DataFrame(out, columns=CHAIN_COLUMNS)


def fetch_chain(ticker: str, expiry: str | None = None, timeout: float = 20.0) -> pd.DataFrame:
    """Render the public Pineify options-chain page and extract volume/OI per strike.

    Raises on any failure (navigation timeout, missing table, unexpected row
    layout, no usable rows) — callers should treat this as a best-effort
    supplement and catch broadly.
    """
    timeout_ms = timeout * 1000
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(PINEIFY_URL, wait_until="networkidle", timeout=timeout_ms)

            ticker_input = page.locator("input[placeholder*='Enter ticker']")
            ticker_input.click()
            ticker_input.fill(ticker.upper())
            page.get_by_role("button", name="Load Chain").click()
            page.wait_for_selector("table", timeout=timeout_ms)
            page.wait_for_timeout(2000)

            if expiry:
                expiry_select = page.locator("select").nth(1)
                options = expiry_select.evaluate("el => Array.from(el.options).map(o => o.value)")
                if expiry in options:
                    expiry_select.select_option(expiry)
                    page.wait_for_timeout(2000)

            rows = _extract_rows(page)
        finally:
            browser.close()

    if not rows:
        raise RuntimeError(f"Pineify: no {EXPECTED_ROW_CELLS}-cell rows found for {ticker!r}")

    chain = _rows_to_frame(rows)
    if chain.empty:
        raise RuntimeError(f"Pineify: parsed 0 usable strikes for {ticker!r}")
    return chain
