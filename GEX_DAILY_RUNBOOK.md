# GEX Daily Runbook

File này ghi lại các lệnh để mỗi ngày có thể chạy thủ công pipeline QQQ options -> reconciled CBOE+Yahoo data -> Gamma/GEX -> HTML dashboard.

## Kiến Trúc Pipeline

```text
CBOE delayed (cdn.cboe.com)  ─┐  structural data / open interest
                               ├─→ Source reconciliation → Intraday T → BSM IV/Delta/Gamma
Yahoo latest (yfinance)      ─┘  spot / bid / ask / IV, cross-check
                                        → DEX/GEX
                                        → Gamma Wall / Put Support / Call Resistance / Gamma Flip / Delta Flip
                                        → HTML dashboard (Plotly) + TXT levels
                                        → raw JSON + Parquet + summary JSON + replay index
```

- **CBOE** (`cdn.cboe.com/api/global/delayed_quotes/options/{TICKER}.json`, free/unofficial endpoint): nguồn chính cho open interest và cấu trúc chain.
- **Yahoo** (`yfinance`): nguồn chính cho spot, bid/ask, implied volatility.
- **Reconciliation** (`scripts/reconcile.py`): outer-join theo (strike, option_type); OI ưu tiên CBOE (fallback Yahoo nếu thiếu); bid/ask/IV ưu tiên Yahoo (fallback CBOE); flag nếu IV lệch >20% hoặc spot hai nguồn lệch >1%.
- Không còn dashboard PNG (matplotlib) — đã bị xoá theo quyết định giữ HTML + TXT.

## 1. Cài Thư Viện

Chỉ cần chạy một lần, hoặc chạy lại khi đổi máy/môi trường Python.

```bash
python3 -m pip install -r requirements-options.txt
```

`requirements-options.txt` gồm: `yfinance`, `requests` (gọi CBOE), `pandas`, `numpy`, `scipy`, `pyarrow` (đọc/ghi Parquet), `plotly` (dashboard HTML).

## 2. Lấy Dữ Liệu Mới (CBOE + Yahoo) Và Render Dashboard

### Cách Khuyến Nghị: Một Lệnh Ra Kết Quả Cuối

```bash
python3 scripts/run_gex_dashboard.py --ticker QQQ
```

Nếu muốn chọn expiry cụ thể:

```bash
python3 scripts/run_gex_dashboard.py --ticker QQQ --expiry 2026-08-17
```

Lệnh này sẽ tự gọi:

```text
daily_qqq_snapshot.py   (fetch CBOE + Yahoo -> reconcile -> BSM -> GEX/DEX -> save raw/Parquet/JSON/replay)
      ↓
render_gex_interactive.py   (HTML dashboard GEX/DEX + Gamma Flip/Delta Flip)
      ↓
export_gex_levels_text.py   (TXT một dòng key levels)
```

Mỗi lần chạy là **một snapshot mới, không ghi đè** — dùng để "tua lại" (replay) diễn biến GEX trong ngày (xem mục 5).

Output nằm trong `data/options/YYYY-MM-DD/`, ví dụ:

```text
data/options/2026-08-18/raw/QQQ_2026-08-17_143022_cboe.json
data/options/2026-08-18/raw/QQQ_2026-08-17_143022_yahoo.json
data/options/2026-08-18/QQQ_2026-08-17_143022_by_strike.parquet
data/options/2026-08-18/QQQ_2026-08-17_143022_summary.json
data/options/2026-08-18/QQQ_2026-08-17_143022_reconciliation.json
data/options/2026-08-18/replay_index.jsonl

# "latest" - luôn là snapshot mới nhất trong ngày, dashboard/levels đọc các file này:
data/options/2026-08-18/QQQ_2026-08-17_by_strike.parquet
data/options/2026-08-18/QQQ_2026-08-17_summary.json
data/options/2026-08-18/QQQ_2026-08-17_interactive.html
data/options/2026-08-18/QQQ_2026-08-17_levels.txt
```

### Cách Tách Từng Bước

Chỉ lấy dữ liệu + tính toán, không render:

```bash
python3 scripts/daily_qqq_snapshot.py --ticker QQQ
```

Lệnh này sẽ:

- Lấy option chain QQQ từ Yahoo (spot/bid/ask/IV) và CBOE (OI/structure).
- Reconcile hai nguồn, tạo `_reconciliation.json` (số strike matched/flagged, spot hai nguồn).
- Tính gamma/delta bằng Black-Scholes với intraday T.
- Tính GEX/DEX theo strike, gamma flip, delta flip.
- Ghi raw JSON (CBOE + Yahoo), Parquet, summary JSON, cập nhật "latest", append `replay_index.jsonl`.

Chọn expiry cụ thể:

```bash
python3 scripts/daily_qqq_snapshot.py --ticker QQQ --expiry 2026-08-17
```

Nếu muốn lưu vào folder riêng:

```bash
python3 scripts/daily_qqq_snapshot.py \
  --ticker QQQ \
  --expiry 2026-08-17 \
  --output-root data/options/live
```

## 3. Render Dashboard HTML Riêng

Sau khi đã có dataset (Parquet + summary JSON) trong một folder, render lại HTML:

```bash
python3 scripts/render_gex_interactive.py \
  --input-dir data/options/2026-08-17 \
  --ticker QQQ \
  --expiry 2026-08-17
```

HTML sẽ được lưu trong cùng folder, ví dụ:

```text
data/options/2026-08-17/QQQ_2026-08-17_interactive.html
```

Dashboard hiển thị Net GEX/DEX theo strike, cùng các đường mốc: Call Resistance, Put Support, Gamma Wall, Gamma Wall +, **Gamma Flip**, **Delta Flip**.

Xuất lại TXT một dòng:

```bash
python3 scripts/export_gex_levels_text.py \
  --input-dir data/options/2026-08-17 \
  --ticker QQQ \
  --expiry 2026-08-17
```

## 4. Recompute Từ Dataset Đã Lưu

Dùng khi đã có raw CBOE/Yahoo JSON rồi, nhưng muốn tính lại GEX với `snapshot-date` khác (dùng `--input-dir` trỏ tới thư mục chứa `raw/`).

```bash
python3 scripts/daily_qqq_snapshot.py \
  --ticker QQQ \
  --expiry 2026-08-17 \
  --snapshot-date 2026-08-14 \
  --input-dir data/options/2026-08-17 \
  --output-root data/options/recomputed
```

Sau đó render dashboard:

```bash
python3 scripts/render_gex_interactive.py \
  --input-dir data/options/recomputed/2026-08-14 \
  --ticker QQQ \
  --expiry 2026-08-17
```

## 5. Replay — Xem GEX Đổi Theo Thời Gian Trong Ngày

Mỗi lần chạy `run_gex_dashboard.py` / `daily_qqq_snapshot.py` trong cùng một ngày sẽ append một dòng vào `replay_index.jsonl` (không ghi đè các snapshot cũ). Sau khi đã chạy nhiều lần trong ngày, dựng dashboard animation:

```bash
python3 scripts/replay.py --ticker QQQ --expiry 2026-08-17 --date 2026-08-18
```

Nếu bỏ `--date`, mặc định lấy ngày hôm nay. Output:

```text
data/options/2026-08-18/QQQ_2026-08-17_replay.html
```

File HTML này có thanh trượt thời gian (slider) và nút Play/Pause để xem net GEX theo strike thay đổi qua từng snapshot trong ngày.

## 6. Nếu GEX Ra Toàn 0

Nguyên nhân thường gặp:

- `snapshot-date` trùng `expiry`, làm script hiểu là 0DTE và `T` quá nhỏ.
- Yahoo trả `impliedVolatility` quá thấp, ví dụ `0.00001`.
- Chain bị stale, bid/ask bằng 0, IV không đáng tin.
- CBOE endpoint không trả OI cho strike đó (xem `_reconciliation.json` để biết strike nào rơi vào `oi_fallback_count`).
- Đang dùng dữ liệu ngày khác nhưng snapshot-date bị đặt sai.

Cách sửa nhanh:

```bash
python3 scripts/daily_qqq_snapshot.py \
  --ticker QQQ \
  --expiry 2026-08-17 \
  --snapshot-date 2026-08-14 \
  --input-dir data/options/2026-08-17 \
  --output-root data/options/recomputed
```

Hoặc ép trực tiếp số ngày tới expiry:

```bash
python3 scripts/daily_qqq_snapshot.py \
  --ticker QQQ \
  --expiry 2026-08-17 \
  --time-to-expiry-days 3 \
  --input-dir data/options/2026-08-17 \
  --output-root data/options/recomputed
```

## 7. Nên Lấy Data Lúc Mấy Giờ Cho 9:30 Open?

Mục tiêu của bạn là dự đoán hoặc chuẩn bị cho market open `09:30 New York time`.

### Khung Giờ Khuyến Nghị

Tốt nhất:

```text
09:25 - 09:29 New York time
```

Lý do:

- Gần open nhất, nên spot price và option chain mới hơn.
- Vẫn kịp render dashboard trước 9:30.
- Phù hợp nếu dùng GEX như context trước open.

Nếu muốn ổn định hơn, ít bị lỗi chain/bid-ask:

```text
09:31 - 09:35 New York time
```

Lý do:

- Sau open, bid/ask và IV thường cập nhật tốt hơn.
- Nhưng lúc này không còn là “pre-open prediction” nữa, mà là “early session confirmation”.

Nếu chỉ cần OI/gamma wall từ dữ liệu qua đêm:

```text
08:30 - 09:15 New York time
```

Lý do:

- Open interest thường là dữ liệu từ ngày trước, không cần chờ sát 9:30.
- Dùng tốt để chuẩn bị key levels trước phiên.

### Nếu Chạy Lúc 20:15 Việt Nam

Khi Mỹ đang dùng giờ mùa hè EDT:

```text
20:15 Việt Nam = 09:15 New York
```

Đây là thời điểm tốt để lấy **pre-open context** sớm. Tuy nhiên Yahoo option chain trước open có thể vẫn là dữ liệu phiên trước. Script đã tự kiểm tra `lastTradeDate`:

```text
Nếu chain mới nhất vẫn là ngày trước
=> dùng ngày đó làm effective snapshot date để tính T
```

Với 0DTE đúng ngày hiện tại, script dùng thời gian còn lại tới `16:00 New York` để tính T, thay vì để T gần bằng 0.

Khuyến nghị thực tế — chạy nhiều lần để có dữ liệu cho replay:

```text
20:15 VN: chạy lần 1 để chuẩn bị key levels
20:25-20:29 VN: chạy lần 2 để lấy dữ liệu sát open hơn
20:31-20:35 VN: chạy lần 3 nếu muốn confirm sau open
```

### Quy Đổi Giờ Việt Nam

Khi Mỹ đang dùng giờ mùa hè EDT:

```text
09:25 - 09:29 New York = 20:25 - 20:29 Việt Nam
09:30 New York          = 20:30 Việt Nam
```

Khi Mỹ dùng giờ mùa đông EST:

```text
09:25 - 09:29 New York = 21:25 - 21:29 Việt Nam
09:30 New York          = 21:30 Việt Nam
```

## 8. Workflow Mỗi Ngày Đề Xuất

### Trước Open

Khoảng `09:25 - 09:29 New York`:

```bash
python3 scripts/run_gex_dashboard.py --ticker QQQ
```

### Sau Open Để Confirm

Khoảng `09:31 - 09:35 New York`, chạy lại (cùng output-root để tích luỹ vào replay index của ngày):

```bash
python3 scripts/run_gex_dashboard.py --ticker QQQ
```

### Cuối Ngày: Xem Replay

```bash
python3 scripts/replay.py --ticker QQQ --expiry 2026-08-17
```

## 9. Diễn Giải Nhanh

- `Call Resistance`: strike phía trên spot có call GEX lớn.
- `Put Support`: strike phía dưới spot có put GEX âm lớn.
- `Gamma Wall`: strike có net GEX lớn nhất theo độ lớn tuyệt đối.
- `Gamma Flip`: strike nơi cumulative net GEX (tính từ strike thấp lên) đổi dấu — trên mức này môi trường thiên dương gamma, dưới mức này thiên âm gamma.
- `Delta Flip`: tương tự Gamma Flip nhưng tính trên cumulative net DEX.
- `Net GEX dương`: môi trường dễ pin/mean-revert hơn.
- `Net GEX âm`: môi trường dễ expansion/trend hơn.

Đây là approximation cá nhân từ dữ liệu CBOE delayed + Yahoo, đã cross-check giữa hai nguồn nhưng vẫn không phải dealer positioning thật.

## 10. Chạy Tự Động Bằng GitHub Actions

Repo có sẵn workflow:

```text
.github/workflows/daily-gex-dashboard.yml
```

Workflow này chạy tự động mỗi thứ Hai đến thứ Sáu lúc:

```text
20:25 Việt Nam = 13:25 UTC
```

Khi Mỹ đang dùng giờ mùa hè, thời điểm này là khoảng `09:25 New York`, phù hợp để lấy dữ liệu sát trước open `09:30`. Khi Mỹ dùng giờ mùa đông, `20:25 Việt Nam` sẽ là `08:25 New York`; nếu muốn sát open mùa đông thì đổi cron thành `25 14 * * 1-5` để chạy `21:25 Việt Nam`.

Workflow sẽ chạy:

```bash
python scripts/run_gex_dashboard.py --ticker QQQ --no-open
```

Kết quả không được commit ngược vào repo. GitHub sẽ lưu thành artifact trong tab **Actions** gồm HTML dashboard, levels TXT, summary JSON, reconciliation JSON và Parquet.

### Upload Lên GitHub Lần Đầu

Folder hiện tại cần được biến thành git repo trước:

```bash
git init
git branch -M main
git add requirements-options.txt GEX_DAILY_RUNBOOK.md scripts .github .gitignore
git commit -m "Add automated QQQ GEX dashboard workflow"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

Sau khi push xong, vào GitHub repo → tab **Actions** → bật workflow nếu GitHub hỏi xác nhận. Bạn cũng có thể bấm **Run workflow** để chạy thủ công ngay, không cần chờ tới 20:25.
