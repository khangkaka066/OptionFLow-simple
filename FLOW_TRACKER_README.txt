CÁCH ĐỌC KẾT HỢP VOLATILITY FLOW, FLOW TRACKER VÀ HEAT TRACKER
==============================================================

Mục tiêu:
Không đọc riêng từng panel. Luôn dùng cả 3 lớp:

1. Heat Tracker
   - Spot đang ở đâu so với Call Resistance, Put Support, Gamma/Vanna wall,
     và vùng GEX dày.

2. Flow Tracker
   - Dòng lệnh option đang tạo pressure bullish hay bearish.

3. Volatility Flow
   - IV/ARV đang ủng hộ breakout, reversal, hay chỉ là noise.

4. So với nến spot
   - Flow có được spot xác nhận không.


FLOW TRACKER - ĐỌC LABEL PREMIUM $
==================================

Trong tooltip Premium $, phần quan trọng nhất là:

DIRECTIONAL FLOW (est.)

Ý nghĩa:

- Call pressure +
  Bullish pressure. Thường là call buying hoặc call-side demand.

- Call pressure -
  Bearish pressure. Call bị bán hoặc thoát.

- Put pressure -
  Bearish pressure. Thường là put buying.

- Put pressure +
  Bullish pressure. Thường là put selling / put covering.

- Net pressure + BULLISH
  Tổng pressure nghiêng tăng.

- Net pressure - BEARISH
  Tổng pressure nghiêng giảm.


ĐỌC NHANH FLOW TRACKER
======================

- Call pressure + và Put pressure +
  Bullish sạch.

- Call pressure - và Put pressure -
  Bearish sạch.

- Một bên dương, một bên âm
  Mixed. Xem Net pressure bên nào lớn hơn.

- Flow bullish nhưng nến spot đóng đỏ
  Bullish flow bị hấp thụ. Chưa long confirmation.
  Nếu nến sau thủng low nến đỏ -> dễ là bull trap.

- Flow bearish nhưng nến spot đóng xanh hoặc giữ đáy
  Bearish flow bị hấp thụ.
  Nếu nến sau vượt high -> dễ là bear trap.


VOLATILITY FLOW - ĐỌC IV / ARV
==============================

- IV tăng, ARV tăng, spot break theo hướng flow
  Breakout/trend có xác suất tốt hơn.

- IV tăng nhưng ARV không tăng, spot không đi theo
  Dễ là premium chase / noise.

- ARV tăng trong lúc spot đi ngược flow
  Absorption mạnh, cẩn thận trap.

- IV cao hơn ARV nhiều
  Premium đang đắt. Tốt để xác nhận risk, không nên chỉ vì flow mà đuổi giá.

- ARV cao hơn hoặc đuổi sát IV
  Biến động thực đang thật hơn. Breakout/reversal quanh wall có thể mạnh.


HEAT TRACKER - ĐỌC LOCATION
===========================

Heat Tracker dùng để chọn vùng phản ứng, không dùng một mình để đo hướng.

- Spot chạm Put Support / vùng GEX dày phía dưới
  + Flow Tracker bullish
  + Nến giữ đáy
  -> Ưu tiên long / reversal.

- Spot chạm Call Resistance / wall phía trên
  + Flow Tracker bearish
  + Nến bị từ chối
  -> Ưu tiên short / reversal.

- Spot phá wall và giữ được ngoài wall
  + Flow Tracker cùng hướng
  + IV/ARV cùng tăng
  -> Ưu tiên trend-follow.

- Spot chạm wall nhưng flow ngược hướng và nến không xác nhận
  -> Đứng ngoài, dễ trap.


CHECKLIST RA QUYẾT ĐỊNH INTRADAY
================================

1. Spot đang ở gần wall nào trên Heat Tracker?
2. Net pressure trên Flow Tracker là bullish hay bearish?
3. Call pressure và Put pressure có cùng chiều không, hay mixed?
4. Nến spot có xác nhận flow không?
5. IV/ARV có đang tăng cùng hướng breakout/reversal không?
6. Nếu flow và spot mâu thuẫn, ưu tiên đứng ngoài chờ nến sau xác nhận.


QUY TẮC GỌN
===========

- Flow cùng hướng với spot
  Có thể follow.

- Flow ngược spot
  Coi là absorption / trap warning.

- Flow mạnh nhưng spot không chạy
  Không đuổi. Chờ break high/low của nến vừa đóng.

- Spot ở giữa range, xa wall
  Giảm size hoặc bỏ qua, vì location kém.

