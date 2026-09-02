from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = PROJECT_ROOT / "frontend"
INDEX_HTML_PATH = WEB_ROOT / "index.html"
ASSETS_ROOT = WEB_ROOT / "assets"


def read_index_html() -> str:
    return INDEX_HTML_PATH.read_text(encoding="utf-8")


def resolve_asset_path(request_path: str) -> Path | None:
    rel_path = unquote(request_path.removeprefix("/assets/"))
    asset_path = (ASSETS_ROOT / rel_path).resolve()
    try:
        asset_path.relative_to(ASSETS_ROOT.resolve())
    except ValueError:
        return None
    return asset_path if asset_path.is_file() else None
