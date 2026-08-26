#!/usr/bin/env python3
"""Xuất PDF hướng dẫn tiếng Việt cho QQQ GEX Dashboard."""

from __future__ import annotations

import textwrap
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "GEX_Dashboard_Trading_Guide.pdf"


PAGES = [
    {
        "title": "QQQ GEX Dashboard - Hướng dẫn sử dụng",
        "blocks": [
            ("text", "Tài liệu này giải thích cách đọc các panel GEX, DEX, Heat Tracker, Volatility Flow, IV Rank, Volatility Skew, OI × IV và OI by Strike."),
            ("text", "Dashboard dùng dữ liệu miễn phí, phù hợp để xác định vùng quan trọng và bối cảnh giao dịch. Không nên xem đây là dealer positioning chính xác tuyệt đối."),
        ],
    },
    {
        "title": "1. Rà soát nguồn dữ liệu",
        "blocks": [
            ("text", "Yahoo Finance: lấy spot, bid/ask, implied volatility, volume và option quote."),
            ("text", "CBOE delayed quotes: lấy cấu trúc option chain và open interest để đối chiếu với Yahoo."),
            ("text", "Reconcile: ưu tiên CBOE cho open interest và contract structure; ưu tiên Yahoo cho IV/bid/ask khi hợp lệ."),
            ("text", "Kết luận: công thức hiện tại phù hợp với free data để nhìn vùng exposure lớn, nhưng không thể thay thế dữ liệu dealer inventory trả phí."),
        ],
    },
    {
        "title": "2. Công thức Black-Scholes",
        "blocks": [
            ("text", "Ký hiệu: S là spot, K là strike, T là thời gian tới đáo hạn theo năm, r là risk-free rate, σ là implied volatility."),
            ("formula", r"$d_1=\frac{\ln(S/K)+(r+\frac{1}{2}\sigma^2)T}{\sigma\sqrt{T}}$"),
            ("formula", r"$\Gamma=\frac{\phi(d_1)}{S\sigma\sqrt{T}}$"),
            ("formula", r"$\Delta_{call}=N(d_1)$"),
            ("formula", r"$\Delta_{put}=N(d_1)-1$"),
            ("text", "Với 0DTE, hệ thống dùng số phút còn lại tới 16:00 New York, tối thiểu 30 phút để tránh gamma bị phóng đại quá mức."),
        ],
    },
    {
        "title": "3. Công thức GEX",
        "blocks": [
            ("formula", r"$Call\ GEX=+\Gamma\times OI\times100\times S^2\times0.01$"),
            ("formula", r"$Put\ GEX=-\Gamma\times OI\times100\times S^2\times0.01$"),
            ("formula", r"$Net\ GEX=Call\ GEX+Put\ GEX$"),
            ("text", "OI là open interest, 100 là multiplier của option Mỹ, còn S² × 0.01 quy đổi exposure cho biến động 1% của underlying."),
            ("text", "Net GEX dương thường gợi ý thị trường dễ pin hoặc mean-reversion hơn. Net GEX âm thường gợi ý biến động có thể mở rộng nhanh hơn."),
        ],
    },
    {
        "title": "4. Công thức DEX",
        "blocks": [
            ("formula", r"$DEX=\Delta\times OI\times100\times S$"),
            ("formula", r"$Net\ DEX=Call\ DEX+Put\ DEX$"),
            ("text", "Call delta thường từ 0 đến 1. Put delta thường từ -1 đến 0. Vì vậy put DEX thường mang dấu âm."),
            ("text", "DEX giúp đọc áp lực directional. Nếu GEX và DEX đồng thuận với hướng giá, setup continuation đáng tin hơn."),
        ],
    },
    {
        "title": "5. Key Levels",
        "blocks": [
            ("text", "Spot: giá QQQ hiện tại từ Yahoo, trước open có thể là premarket price nếu Yahoo trả về."),
            ("text", "Call Resistance: strike call phía trên spot có call GEX lớn nhất."),
            ("text", "Put Support: strike put phía dưới spot có put GEX âm mạnh nhất."),
            ("text", "Gamma Wall: strike có |Net GEX| lớn nhất. Gamma Flip: vùng cumulative Net GEX đổi dấu."),
            ("text", "Delta Wall: strike có |Net DEX| lớn nhất. Delta Flip: vùng cumulative Net DEX đổi dấu."),
        ],
    },
    {
        "title": "6. GEX Exposure",
        "blocks": [
            ("text", "Biểu đồ ngang theo strike. Cyan là Net GEX dương, cam là Net GEX âm. Thanh càng dài thì exposure càng lớn."),
            ("text", "Khi rê chuột vào từng strike sẽ thấy Net GEX, Net Call GEX và Net Put GEX theo đơn vị M/B."),
            ("text", "Spot gần Gamma Wall hoặc Call Resistance/Put Support: ưu tiên quan sát phản ứng giá. Break và giữ được qua wall thì continuation có xác suất tốt hơn."),
        ],
    },
    {
        "title": "7. DEX Exposure",
        "blocks": [
            ("text", "DEX Exposure giống GEX Exposure nhưng dùng delta thay vì gamma."),
            ("text", "DEX dương lớn cho thấy exposure nghiêng về chiều tăng. DEX âm lớn cho thấy exposure nghiêng về chiều giảm."),
            ("text", "Nếu giá break lên và DEX/GEX cùng ủng hộ, setup long tốt hơn. Nếu GEX cản nhưng DEX vẫn mạnh theo trend, không nên fade quá sớm."),
        ],
    },
    {
        "title": "8. Heat Tracker",
        "blocks": [
            ("text", "Heat Tracker là bản đồ Net GEX theo thời gian New York và strike. Mỗi ô màu đại diện cho một snapshot tại một strike."),
            ("text", "Cyan là GEX dương, cam là GEX âm. Ô càng sáng thì |Net GEX| càng lớn. GEX bằng 0 hoặc quá nhỏ sẽ không vẽ ô."),
            ("text", "Đường trắng là spot path. Khi rê chuột vào ô màu sẽ thấy strike, GEX, thời gian ET và dữ liệu snapshot."),
            ("text", "Hàng màu sáng kéo dài nhiều phút là wall ổn định. Spot chạm wall rồi bị từ chối thì ưu tiên reversal; spot cắt qua và giữ được nhiều snapshot thì ưu tiên continuation."),
        ],
    },
    {
        "title": "9. Volatility Flow và IV Rank",
        "blocks": [
            ("text", "Volatility Flow gồm ATM IV, Avg IV và Spot. Spot tăng + IV tăng là breakout có chất lượng hơn; spot tăng + IV giảm thường là grind/short-vol."),
            ("formula", r"$IV\ Rank_{60}=\frac{IV_{current}-IV_{min,60}}{IV_{max,60}-IV_{min,60}}\times100\%$"),
            ("text", "IV Rank trong dashboard là rolling 60 daily sessions IV Rank dựa trên lịch sử ATM IV daily đã lưu. ATM IV được dùng thay vì Avg IV toàn chain để tránh bị méo bởi strike quá xa spot hoặc quote miễn phí bị stale."),
            ("text", "IV cao: tránh đuổi option quá muộn. IV thấp: breakout cần xác nhận rõ hơn."),
        ],
    },
    {
        "title": "10. Volatility Skew, OI × IV, OI by Strike",
        "blocks": [
            ("text", "Volatility Skew so sánh IV call và IV put theo strike. Put IV cao phía dưới spot thể hiện downside protection; Call IV cao phía trên spot thể hiện upside demand."),
            ("text", "OI × IV by Strike cho biết strike nào vừa có open interest lớn vừa có IV cao, tức là đang được thị trường pricing mạnh."),
            ("text", "OI by Strike chỉ hiển thị open interest call/put. Dùng để xác nhận wall, không nên dùng một mình để vào lệnh."),
        ],
    },
    {
        "title": "11. Quy trình lên plan lúc 20:25 Việt Nam",
        "blocks": [
            ("text", "20:25 Việt Nam thường tương đương 09:25 New York trong giờ daylight saving, rất gần open 09:30."),
            ("text", "Bước 1: lấy snapshot mới, ghi Spot, Call Resistance, Put Support, Gamma Wall, Gamma Flip, Delta Wall, Delta Flip."),
            ("text", "Bước 2: xem spot đang ở trên/dưới Gamma Flip hay bị kẹp giữa hai wall."),
            ("text", "Bước 3: dùng Heat Tracker để xem wall nào sáng và ổn định gần spot."),
            ("text", "Bước 4: dùng DEX để xác nhận áp lực directional, rồi viết trước hai kịch bản continuation và reversal."),
        ],
    },
    {
        "title": "12. Mẫu plan giao dịch",
        "blocks": [
            ("text", "Reversal quanh wall: giá chạm Call Resistance, Put Support hoặc Gamma Wall; Heat Tracker có hàng màu sáng; giá thất bại giữ trên/dưới level. Entry sau rejection, stop ngoài wall, target về spot/Gamma Flip/level đối diện."),
            ("text", "Continuation sau break: giá break wall lớn, retest giữ được, GEX/DEX không còn cản mạnh phía trước. Entry sau retest, stop sau wall, target tới wall tiếp theo."),
            ("text", "Không giao dịch khi spot bị kẹp giữa hai wall quá gần, GEX và DEX mâu thuẫn, IV spike nhưng giá không đi đâu, hoặc data bị stale."),
        ],
    },
    {
        "title": "13. Checklist 30 giây",
        "blocks": [
            ("text", "Giá đang gần wall nào? Wall đó là hỗ trợ hay kháng cự?"),
            ("text", "GEX và DEX có đồng thuận không? Heat Tracker có hàng màu sáng liên tục tại level đó không?"),
            ("text", "IV đang tăng hay giảm theo giá? Nếu break thì target wall tiếp theo ở đâu? Nếu reject thì target quay về level nào?"),
            ("text", "Invalidation và stop nằm ở đâu? Nếu không trả lời rõ được câu này thì chưa nên vào lệnh."),
        ],
    },
]


def draw_text(ax, text: str, x: float, y: float, width: int = 88, size: int = 11) -> float:
    for line in textwrap.wrap(text, width=width):
        ax.text(x, y, line, fontsize=size, color="#111827", va="top", ha="left")
        y -= 0.035
    return y


def draw_formula(ax, formula: str, x: float, y: float) -> float:
    ax.text(
        x,
        y,
        formula,
        fontsize=15,
        color="#000000",
        va="top",
        ha="left",
        bbox={"facecolor": "#F3F4F6", "edgecolor": "#D1D5DB", "boxstyle": "round,pad=0.45"},
    )
    return y - 0.065


def main() -> None:
    plt.rcParams["font.family"] = "DejaVu Sans"
    with PdfPages(OUT) as pdf:
        for page in PAGES:
            fig = plt.figure(figsize=(8.27, 11.69), facecolor="#FFFFFF")
            ax = fig.add_axes([0, 0, 1, 1])
            ax.set_axis_off()
            ax.text(0.08, 0.94, page["title"], fontsize=21, fontweight="bold", color="#000000", va="top")
            y = 0.875
            for kind, content in page["blocks"]:
                if kind == "formula":
                    y = draw_formula(ax, content, 0.1, y)
                else:
                    y = draw_text(ax, "• " + content, 0.1, y)
                    y -= 0.022
            ax.text(0.08, 0.055, "OptionFLow-simple | Free-data GEX/DEX approximation", fontsize=8.5, color="#374151")
            pdf.savefig(fig, facecolor=fig.get_facecolor())
            plt.close(fig)
    print(OUT)


if __name__ == "__main__":
    main()
