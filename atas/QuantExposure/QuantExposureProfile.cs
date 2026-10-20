using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Drawing;
using System.Net.Http;
using System.Text.Json;
using ATAS.Indicators;
using OFT.Rendering.Context;
using OFT.Rendering.Tools;

namespace QuantResearch.Atas;

[DisplayName("Quant Research — QQQ Exposure on NQ")]
[Category("Quant Research")]
[Description("QQQ OI-model exposure from quant-research, projected onto NQ using a daily locked 15:59 ratio.")]
public sealed class QuantExposureProfile : Indicator
{
    private readonly CancellationTokenSource _stop = new();
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(15) };
    private volatile ExposureSnapshot? _snapshot;
    private volatile LockedMapping? _mapping;
    private volatile ChartContext? _chart;
    private volatile string _connection = "Connecting";
    private volatile string _mappingMessage = "Waiting for mapping";
    private string _mappingKey = "";
    private DateTimeOffset _nextMappingAttempt;
    private bool _started;
    private sealed record ChartContext(string Instrument, decimal Tick, ICandlesDataProvider? Candles);

    [Display(Name = "Server URL", GroupName = "Connection", Order = 1)]
    public string ServerUrl { get; set; } = "http://127.0.0.1:8899";
    [Display(Name = "Exposure basis", GroupName = "Connection", Order = 2)]
    public ExposureBasis Basis { get; set; } = ExposureBasis.Intraday;
    [Range(2, 300)]
    [Display(Name = "Poll seconds", GroupName = "Connection", Order = 3)]
    public int PollSeconds { get; set; } = 10;
    [Display(Name = "Metric", GroupName = "Profile", Order = 1)]
    public ExposureMetric Metric { get; set; } = ExposureMetric.Gex;
    [Range(120, 360)]
    [Display(Name = "Width px", GroupName = "Profile", Order = 2)]
    public int Width { get; set; } = 220;
    [Range(0, 600)]
    [Display(Name = "Right inset px", GroupName = "Profile", Order = 3)]
    public int RightInset { get; set; } = 0;
    [Display(Name = "Collapsed", GroupName = "Profile", Order = 4)]
    public bool Collapsed { get; set; }
    [Display(Name = "Protect candle area", GroupName = "Profile", Order = 5)]
    public bool ProtectCandles { get; set; } = true;
    [Display(Name = "Square-root scale", GroupName = "Profile", Order = 6)]
    public bool SqrtScale { get; set; } = true;
    [Display(Name = "Key levels", GroupName = "Profile", Order = 7)]
    public bool ShowKeyLevels { get; set; } = true;
    [Display(Name = "Source strike labels", GroupName = "Profile", Order = 8)]
    public bool ShowStrikeLabels { get; set; } = true;
    [Range(8, 18)]
    [Display(Name = "Font size", GroupName = "Profile", Order = 9)]
    public int FontSize { get; set; } = 10;
    [Display(Name = "Follow New York DST", Description = "False matches the Pine default UTC-4; true follows America/New_York.", GroupName = "Locked ratio", Order = 1)]
    public bool FollowNewYorkDst { get; set; }
    [Range(0, 23)]
    [Display(Name = "Lock hour", GroupName = "Locked ratio", Order = 2)]
    public int LockHour { get; set; } = 9;
    [Range(0, 59)]
    [Display(Name = "Lock minute", GroupName = "Locked ratio", Order = 3)]
    public int LockMinute { get; set; }
    [Display(Name = "Manual ratio (0 = auto)", Description = "Copy the exact locked TradingView NQ/QQQ ratio if Yahoo/ATAS differs or M1 is unavailable.", GroupName = "Locked ratio", Order = 4)]
    public decimal ManualRatio { get; set; }
    [Display(Name = "Manual ratio date YYYY-MM-DD", Description = "Must match today's date in the lock timezone. Prevents carrying yesterday's manual ratio silently.", GroupName = "Locked ratio", Order = 5)]
    public string ManualRatioDate { get; set; } = "";

    public QuantExposureProfile() : base(true)
    {
        EnableCustomDrawing = true;
        DrawAbovePrice = true;
        DenyToChangePanel = true;
        ((ValueDataSeries)DataSeries[0]).VisualType = VisualMode.Hide;
        ((ValueDataSeries)DataSeries[0]).ScaleIt = false;
        SubscribeToDrawingEvents(DrawingLayouts.Final);
    }

    protected override void OnInitialize()
    {
        CaptureChart();
        if (_started) return;
        _started = true;
        _ = Task.Run(PollAsync);
    }

    protected override void OnCalculate(int bar, decimal value)
    {
        if (bar == CurrentBar - 1) CaptureChart();
    }

    private void CaptureChart() => _chart = new(InstrumentInfo?.Instrument ?? "", InstrumentInfo?.TickSize ?? 0, CandlesDataProvider);

    private Uri Endpoint(string path)
    {
        if (!Uri.TryCreate(ServerUrl.TrimEnd('/') + path, UriKind.Absolute, out var uri) ||
            uri.Scheme is not ("http" or "https"))
            throw new InvalidOperationException("Use an http:// or https:// server URL");
        return uri;
    }

    private async Task PollAsync()
    {
        try
        {
            while (!_stop.IsCancellationRequested)
            {
                try
                {
                    var basis = Basis.ToString().ToLowerInvariant();
                    using var response = await _http.GetAsync(Endpoint($"/api/atas/exposure?ticker=QQQ&basis={basis}"), _stop.Token);
                    response.EnsureSuccessStatusCode();
                    var payload = ExposureSnapshot.Parse(await response.Content.ReadAsStringAsync(_stop.Token));
                    if (payload.Basis != basis) throw new FormatException("Exposure basis mismatch");
                    _snapshot = payload;
                    _connection = "Connected";
                }
                catch (Exception ex) when (!_stop.IsCancellationRequested)
                {
                    _connection = ex is HttpRequestException he ? $"HTTP/offline {he.StatusCode}" : "Invalid response / unavailable";
                }
                try { await UpdateMappingAsync(); }
                catch (Exception) when (!_stop.IsCancellationRequested)
                {
                    _mappingMessage = "Missing matched 15:59 closes; retry/manual ratio";
                }
                RequestRedraw();
                await Task.Delay(TimeSpan.FromSeconds(Math.Clamp(PollSeconds, 2, 300)), _stop.Token);
            }
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        finally { _http.Dispose(); }
    }

    private async Task UpdateMappingAsync()
    {
        var now = DateTimeOffset.UtcNow;
        var chart = _chart;
        var day = SessionClock.Day(now, FollowNewYorkDst);
        var key = $"{day}|{chart?.Instrument}|{FollowNewYorkDst}|{LockHour}|{LockMinute}|{ManualRatio}|{ManualRatioDate}";
        if (_mappingKey != key)
        {
            _mappingKey = key;
            _mapping = null;
            _nextMappingAttempt = DateTimeOffset.MinValue;
        }
        if (chart is null || chart.Tick <= 0 ||
            !(chart.Instrument.StartsWith("NQ", StringComparison.OrdinalIgnoreCase) || chart.Instrument.StartsWith("MNQ", StringComparison.OrdinalIgnoreCase)))
        {
            _mappingMessage = "Attach to an NQ or MNQ contract";
            return;
        }
        if (ManualRatio > 0)
        {
            if (ManualRatioDate != day) { _mappingMessage = $"Manual ratio date must be {day}"; return; }
            _mapping = new(day, chart.Instrument, ManualRatio, null, "MANUAL");
            _mappingMessage = "Manual ratio";
            return;
        }
        if (!SessionClock.AfterLock(now, FollowNewYorkDst, LockHour, LockMinute))
        {
            _mappingMessage = $"Waiting for lock {LockHour:00}:{LockMinute:00}";
            return;
        }
        if (_mapping is not null || now < _nextMappingAttempt) return;
        _nextMappingAttempt = now.AddMinutes(1);
        if (chart.Candles is not { IsAvailable: true })
        {
            _mappingMessage = "M1 provider unavailable; enter manual ratio/date";
            return;
        }
        var candles = await chart.Candles.GetCandlesAsync(new CandleSeriesRequest {
            TimeFrame = CandleSeriesTimeFrame.Minutes(1), DaysToLoad = 10
        }, _stop.Token).WaitAsync(TimeSpan.FromSeconds(20), _stop.Token);
        var previous = candles.Select(c => new { Candle = c, Utc = CandleUtc(c.Time) })
            .Where(c => {
                var ny = TimeZoneInfo.ConvertTime(c.Utc, SessionClock.NewYork);
                return ny.Hour == 15 && ny.Minute == 59 &&
                    string.CompareOrdinal(ny.ToString("yyyy-MM-dd"), day) < 0 &&
                    c.Utc.AddMinutes(1) <= now && now - c.Utc < TimeSpan.FromDays(10);
            }).OrderByDescending(c => c.Utc).FirstOrDefault();
        if (previous is null) { _mappingMessage = "Load prior-session NQ M1 history or enter ratio"; return; }
        var referenceDate = TimeZoneInfo.ConvertTime(previous.Utc, SessionClock.NewYork).ToString("yyyy-MM-dd");
        using var response = await _http.GetAsync(Endpoint($"/api/atas/reference?date={referenceDate}"), _stop.Token);
        response.EnsureSuccessStatusCode();
        var qqq = JsonSerializer.Deserialize<CloseReference>(await response.Content.ReadAsStringAsync(_stop.Token), ExposureSnapshot.JsonOptions)
            ?? throw new FormatException("Missing QQQ close");
        var mapped = LockedMapping.FromCloses(day, chart.Instrument, previous.Candle.Close, previous.Utc, qqq);
        if (_mappingKey == key && !_stop.IsCancellationRequested)
        {
            _mapping = mapped;
            _mappingMessage = $"{mapped.Mode} {referenceDate}";
        }
    }

    // ATAS indicator candle timestamps are UTC; Unspecified is treated as UTC,
    // never as the Mac's local timezone.
    private static DateTimeOffset CandleUtc(DateTime time) => new(time.Kind == DateTimeKind.Local
        ? time.ToUniversalTime() : DateTime.SpecifyKind(time, DateTimeKind.Utc));

    private void RequestRedraw()
    {
        if (_stop.IsCancellationRequested) return;
        DoActionInGuiThread(() => { if (!IsDisposed) RedrawChart(); });
    }

    protected override void OnRender(RenderContext context, DrawingLayouts layout)
    {
        if (ChartInfo is null || ChartArea.Width < 160) return;
        var snapshot = _snapshot;
        var mapping = _mapping;
        var chart = _chart;
        var area = ChartArea;
        int width = Collapsed ? 110 : Math.Min(Math.Clamp(Width, 120, 360), Math.Max(120, area.Width / 4));
        int right = area.Right - Math.Clamp(RightInset, 0, Math.Max(0, area.Width - width));
        int left = right - width;
        int top = area.Top + 4;
        var font = new RenderFont(ChartInfo.PriceAxisFont.FontFamily, Math.Clamp(FontSize, 8, 18));
        var text = Color.FromArgb(230, 228, 235, 246);
        var amber = Color.FromArgb(255, 234, 190, 107);
        var background = Color.FromArgb(230, 12, 20, 32);
        context.FillRectangle(background, new Rectangle(left, top, width, 76));
        context.DrawString($"QQQ → NQ  {Metric.ToString().ToUpperInvariant()}", font, text, left + 6, top + 4);
        string status = snapshot is null ? _connection : $"{snapshot.Basis} · {snapshot.Status}";
        var age = snapshot?.SnapshotUtc is { } ts ? DateTimeOffset.UtcNow - ts : TimeSpan.MaxValue;
        bool stale = snapshot is null || snapshot.Stale || snapshot.SourceIsStale || age > TimeSpan.FromMinutes(3) || _connection != "Connected";
        context.DrawString(Collapsed ? (stale ? "STALE" : status) : status + (stale ? " · STALE" : ""), font, stale ? amber : text, left + 6, top + 21);
        if (Collapsed) return;
        var stamp = snapshot?.SnapshotUtc?.ToString("MM-dd HH:mm 'UTC'") ?? "No snapshot";
        context.DrawString(stamp + (snapshot is null ? "" : $" · {snapshot.Expiry}"), font, text, left + 6, top + 38);
        var netKey = "net_" + Metric.ToString().ToLowerInvariant();
        double? total = null;
        snapshot?.Totals.TryGetValue(netKey, out total);
        context.DrawString($"OI estimate · net {Compact(total)}", font, text, left + 6, top + 55);
        if (snapshot is null || chart is null || mapping is null || mapping.Instrument != chart.Instrument ||
            mapping.SessionDate != SessionClock.Day(DateTimeOffset.UtcNow, FollowNewYorkDst))
        {
            context.DrawString(_mappingMessage, font, amber, Math.Max(area.Left + 4, left - 190), top + 81);
            return;
        }
        var prices = ChartInfo.PriceChartContainer;
        if (ProtectCandles && CurrentBar > 0)
        {
            int last = Math.Clamp(LastVisibleBarNumber, 0, CurrentBar - 1);
            var candleRight = prices.GetXByBar(last, false) + (int)prices.BarsWidth + 10;
            if (left < candleRight)
            {
                context.DrawString($"Reserve {width + 20}px right margin", font, amber, left + 4, top + 81);
                return;
            }
        }
        int bottom = area.Bottom - 34;
        int plotTop = top + 81;
        if (bottom <= plotTop) return;
        context.FillRectangle(Color.FromArgb(stale ? 115 : 160, 12, 20, 32), new Rectangle(left, plotTop, width, bottom - plotTop));
        int zero = left + width / 2;
        int half = Math.Max(1, width / 2 - 10);
        context.DrawLine(new RenderPen(Color.FromArgb(120, 140, 160, 180)), zero, plotTop, zero, bottom);
        var rows = snapshot.Rows.Where(r => r.Value(Metric) is double v && double.IsFinite(v)).ToArray();
        var maximum = rows.Select(r => Math.Abs(r.Value(Metric)!.Value)).DefaultIfEmpty(0).Max();
        int previousLabelY = int.MinValue;
        foreach (var row in rows.OrderBy(r => r.Strike))
        {
            var value = row.Value(Metric)!.Value;
            if (value == 0) continue;
            var price = mapping.Map(row.Strike, chart.Tick);
            int y = prices.GetYByPrice(price, false);
            if (y < plotTop + 8 || y > bottom - 8) continue;
            int length = Math.Max(1, (int)(half * SessionClock.Width(value, maximum, SqrtScale)));
            var color = value >= 0 ? Color.FromArgb(stale ? 110 : 215, 53, 215, 137) : Color.FromArgb(stale ? 110 : 215, 154, 111, 255);
            context.FillRectangle(color, new Rectangle(value >= 0 ? zero : zero - length, y - 3, length, 6));
            if (ShowStrikeLabels && Math.Abs((long)y - previousLabelY) >= FontSize + 4)
            {
                context.DrawString($"{row.Strike:0.##}", font, text, left + 4, y - FontSize - 5);
                previousLabelY = y;
            }
        }
        if (ShowKeyLevels)
        {
            var keys = new[] { ("call_resistance", "CR"), ("put_support", "PS"),
                (SessionClock.WallKey(Metric), "Wall"), (SessionClock.FlipKey(Metric), "Flip") };
            int labelY = int.MinValue;
            foreach (var level in keys.Where(k => snapshot.Levels.GetValueOrDefault(k.Item1) > 0)
                         .OrderByDescending(k => snapshot.Levels[k.Item1]))
            {
                var source = snapshot.Levels[level.Item1]!.Value;
                var mapped = mapping.Map(source, chart.Tick);
                int y = prices.GetYByPrice(mapped, false);
                if (y < plotTop + 14 || y > bottom - 8) continue;
                var color = level.Item2 == "Flip" ? amber : Color.FromArgb(180, 166, 175, 225);
                context.DrawLine(new RenderPen(color), Math.Max(area.Left, left - 80), y, right, y);
                if (Math.Abs((long)y - labelY) >= FontSize + 6)
                {
                    context.FillRectangle(background, new Rectangle(left + 2, y - FontSize - 5, width - 4, FontSize + 4));
                    context.DrawString($"{level.Item2} {mapped:0.00} ← {source:0.##}", font, color, left + 4, y - FontSize - 5);
                    labelY = y;
                }
            }
        }
        context.DrawString(SqrtScale ? "−   sqrt scale   +" : "−   linear scale   +", font, text, left + 4, bottom + 3);
        context.DrawString($"R {mapping.Ratio:0.00000} · {mapping.Mode}", font, text, left + 4, bottom + 17);
    }

    private static string Compact(double? value)
    {
        if (value is null || !double.IsFinite(value.Value)) return "n/a";
        var n = value.Value;
        return Math.Abs(n) >= 1e9 ? $"{n / 1e9:+0.0;-0.0;0}B" :
            Math.Abs(n) >= 1e6 ? $"{n / 1e6:+0.0;-0.0;0}M" : $"{n:+0;-0;0}";
    }

    protected override void OnDispose()
    {
        _stop.Cancel();
        if (!_started) _http.Dispose();
        base.OnDispose();
    }
}
