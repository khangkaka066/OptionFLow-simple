# QQQ Exposure trên chart NQ — ATAS X / macOS

Indicator đọc exposure từ **server quant-research hiện có**, tự cập nhật và vẽ
profile hẹp bên phải, theo cùng trục giá với nến NQ/MNQ. Đây là profile snapshot
hiện tại, không phải heatmap lưu lịch sử hoặc công cụ replay lịch sử exposure.

## Cài và mở

1. Server: mở `atas/start-local-server.command`, hoặc chạy từ thư mục dự án:

   ```sh
   .venv/bin/python scripts/live_server.py --host 127.0.0.1 --port 8899 --no-github-pull --skip-retention --secondary-ticker "" --interval-seconds 60
   ```

   `--skip-retention` tránh xóa dữ liệu lưu cũ lúc mở server. Launcher không pull
   hoặc ghi đè Git. Collector vẫn dùng nguồn và công thức có sẵn của dự án.
   Endpoint: `http://127.0.0.1:8899/api/atas/exposure?ticker=QQQ&basis=intraday`.

2. ATAS X: mở chart **đúng hợp đồng NQ/MNQ đang giao dịch**. Indicators → tìm
   **Quant Research — QQQ Exposure on NQ**. Nếu chưa có, bấm Add custom indicator
   và chọn `atas/dist/QuantExposure.dll`. Trên macOS thư mục tự nạp là
   `~/Library/Application Support/ATAS/Indicators/`.
3. `Server URL`: `http://127.0.0.1:8899`. Nếu dùng server từ xa, nhập URL HTTPS
   của backend đã có hai endpoint mới. `Poll seconds` mặc định 10; polling không
   làm nguồn upstream cập nhật nhanh hơn collector (launcher mặc định 60 giây).
4. Chừa khoảng trống bên phải chart khoảng **240 px**. `Protect candle area`
   mặc định bật: nếu thiếu chỗ, indicator chỉ hiện header/hướng dẫn, không phủ
   histogram lên nến. Có thể chỉnh Width (mặc định 220), Right inset, Collapsed,
   Font size, Metric GEX/DEX/VEX/CHEX và bật/tắt nhãn strike/key levels.

## Quy đổi giống cơ chế Pine trong All in one.txt

- Đọc **M1 của chính instrument trên chart**, kể cả chart chính là footprint/range.
- Chọn nến **15:59 America/New_York của phiên trước**; request server lấy close
  QQQ M1 không điều chỉnh ở **đúng timestamp đó**, không dùng daily close/giá gần nhất.
- `ratio = NQ_close_1559 / QQQ_close_1559`. Khóa ratio ở giờ cấu hình, mặc định
  **09:00 UTC-4 cố định**, đúng mặc định Pine. Bật `Follow New York DST` nếu muốn
  giờ khóa đi theo New York; UTC-4 và New York khác nhau vào mùa đông.
- `NQ level = round_to_tick(QQQ strike * ratio)`. Ratio chỉ đổi khi sang ngày
  khóa mới, đổi instrument hoặc đổi cấu hình mapping; exposure vẫn cập nhật.
- Thiếu M1/QQQ close thì báo thiếu mapping. Có thể nhập **Manual ratio** và
  **Manual ratio date YYYY-MM-DD** để dùng đúng tỷ lệ từ TradingView. Ngày phải
  khớp ngày hiện tại trong múi giờ khóa; không tự mang tỷ lệ thủ công sang phiên sau.
- QQQ M1 trong chế độ tự động lấy qua Yahoo; NQ lấy từ feed ATAS. Vì hai feed có
  thể khác close TradingView, đây là cùng công thức, không cam kết từng tick khớp
  TradingView. Manual ratio dùng khi cần đồng nhất tuyệt đối với tỷ lệ Pine.
- Cần tải ít nhất một phiên trước có đủ M1. Ngày nghỉ/đóng sớm thiếu 15:59 sẽ báo
  thiếu cặp close khớp; không tự thay bằng close 13:00.

Profile xuất toàn bộ strike trong cửa sổ server; chỉ vẽ mức nằm trong vùng giá
đang nhìn. `Show source strike labels` cho biết strike QQQ gốc. Phiên bản này
không dùng QQQ spot 09:00 để cắt đúng 15 strike như phần lưới strike của Pine:
profile cần hiển thị exposure có sẵn trong cửa sổ giá server. Không tự tạo thêm
strike/giá trị exposure không có trong dữ liệu.

## Đọc đúng trạng thái

- `Intraday` lấy `latest_summary/by_strike`; `Eod` lấy `levels_summary/levels_by_strike`.
  Không tự fallback EOD khi thiếu intraday. Lúc chưa có snapshot, header báo HTTP/
  unavailable; collector cập nhật xong sẽ tự lấy lại.
- `snapshot_utc`, nhãn expiry và `STALE` luôn được thể hiện. Network lỗi giữ dữ
  liệu cuối cùng với cảnh báo, không biến dữ liệu cũ thành live. Ngưỡng stale
  hiện tại 3 phút; session_closed/premarket được giữ đúng từ backend.
- Model là **classic_oi_type_sign_estimate**: GEX/Greeks × OI với quy ước dấu
  call/put của dự án. Không phải dealer inventory/market-maker GEX được xác nhận.
  OI có thể là từ phiên trước dù Greeks/giá được cập nhật trong phiên.
- Exposure vẫn là **QQQ** sau khi ánh xạ vị trí lên NQ. Không nhân tỷ lệ giá vào
  giá trị dollar exposure và không cộng chung với NDX để nhận là dealer NQ GEX.
- Header net lấy tổng summary của nguồn, có thể khác tổng các strike trong cửa
  sổ server. Histogram chỉ thể hiện cửa sổ; không suy ra net bằng số cột xanh/đỏ.
- `Wall` và `Flip` là hai trường riêng. Thiếu flip thì không vẽ. Các mức kế thừa
  cách tính của server, không suy đoán gamma flip từ màu một strike.
- Square-root scale giữ thanh nhỏ còn nhìn thấy; chiều dài không tỷ lệ tuyến
  tính với exposure. Tắt nếu muốn linear.
- Server cũ khóa collector cuối phiên; mở lại launcher trước phiên mới để seed
  lại session. Indicator tự đổi ngày ratio, nhưng không thể mở khóa session của
  server thay cho việc khởi động lại server.

## Build và kiểm tra

Yêu cầu .NET 10 SDK và DLL từ ATAS X đang cài:

```sh
dotnet build atas/QuantExposure/QuantExposure.csproj -c Release
dotnet run --project atas/QuantExposure.Checks/QuantExposure.Checks.csproj
.venv/bin/python -m pytest tests/test_atas_exposure.py -q
```

Đường dẫn ATAS mặc định `/Applications/ATAS X.app/Contents/MonoBundle`.
Nếu cài nơi khác: `dotnet build ... -p:AtasPath="/đường/dẫn/MonoBundle"`.
Không cần WPF/Windows Forms, không giao dịch/đặt lệnh, không cài thêm gói dữ liệu.

## API

- `GET /api/atas/exposure?ticker=QQQ&basis=intraday` — snapshot hiện tại, không
  response-cache; 503 nếu chưa có dữ liệu, 400 nếu ticker/basis sai. Phiên bản 1
  phục vụ ticker chính của server, indicator hiện giới hạn QQQ → NQ/MNQ.
- `GET /api/atas/reference?date=2026-10-16` — close QQQ M1 15:59 của ngày đó;
  timestamp phải khớp nến NQ. Cache thành công một ngày, 503 nếu nguồn thiếu.
