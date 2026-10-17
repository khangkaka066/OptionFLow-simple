import json
from pathlib import Path
from unittest.mock import patch

from live_dashboard.xynth_flow_service import XynthFlowService, is_same_day_expiry, parse_flow_html

HEADINGS = ['Time (ET)', 'Ticker', 'Side', 'Contract', 'Expiry', 'Stock', 'Size', 'Premium', 'Volume', 'OI', 'IV %']


def page(day, ticker='QQQ', clock='09:31:00', expiry=None):
    expiry = expiry or day
    cells = [f'{day} {clock}', ticker, 'ASK', f'$750 CALL {expiry}', expiry, '$751.20', '12', '$2.4K', '123', '456', '18.2%']
    return '<table class="flow-table"><thead><tr>' + ''.join(f'<th>{h}</th>' for h in HEADINGS) + '</tr></thead><tbody><tr>' + ''.join(f'<td>{v}</td>' for v in cells) + '</tr></tbody></table>'


def test_public_html_fields_are_distinct():
    rows = parse_flow_html(page('10/08/26'), 'QQQ')
    assert len(rows) == 1
    assert rows[0]['size'] == 12
    assert rows[0]['volume'] == 123
    assert rows[0]['iv_pct'] == 18.2
    assert rows[0]['trading_date'] == '2026-10-08'
    assert is_same_day_expiry(rows[0])
    assert not is_same_day_expiry(parse_flow_html(page('10/08/26', expiry='10/09/26'), 'QQQ')[0])


def test_keeps_previous_session_until_new_trade_date(tmp_path: Path):
    service = XynthFlowService(tmp_path)
    class Response:
        def __init__(self, text): self.text = text
        def raise_for_status(self): pass
    with patch('live_dashboard.xynth_flow_service.requests.get', side_effect=[Response(page('10/08/26')), Response(page('10/09/26', clock='09:15:00')), Response(page('10/09/26'))]):
        assert service.refresh()['trading_date'] == '2026-10-08'
        assert service.refresh()['trading_date'] == '2026-10-08'
        new = service.refresh()
        assert new['trading_date'] == '2026-10-09'
        assert len(new['rows']) == 1
    assert json.loads((tmp_path / 'QQQ' / '2026-10-08.json').read_text())['rows']
    assert json.loads((tmp_path / 'QQQ' / '2026-10-09.json').read_text())['rows']


def test_cached_mixed_expiries_only_expose_0dte(tmp_path: Path):
    file = tmp_path / 'QQQ' / '2026-10-08.json'
    file.parent.mkdir()
    rows = parse_flow_html(page('10/08/26'), 'QQQ') + parse_flow_html(page('10/08/26', expiry='10/09/26'), 'QQQ')
    file.write_text(json.dumps({'trading_date': '2026-10-08', 'rows': rows}))
    service = XynthFlowService(tmp_path)
    assert service.snapshot()['trading_date'] == '2026-10-08'
    assert len(service.snapshot()['rows']) == 1
