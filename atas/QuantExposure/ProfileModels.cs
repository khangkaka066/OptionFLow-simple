using System.Text.Json;
using System.Text.Json.Serialization;

namespace QuantResearch.Atas;

public enum ExposureMetric { Gex, Dex, Vex, Chex }
public enum ExposureBasis { Intraday, Eod }

public sealed class ExposureRow
{
    public decimal Strike { get; set; }
    [JsonPropertyName("net_gex")] public double? Gex { get; set; }
    [JsonPropertyName("net_dex")] public double? Dex { get; set; }
    [JsonPropertyName("net_vex")] public double? Vex { get; set; }
    [JsonPropertyName("net_chex")] public double? Chex { get; set; }
    public double? Value(ExposureMetric metric) => metric switch {
        ExposureMetric.Dex => Dex, ExposureMetric.Vex => Vex,
        ExposureMetric.Chex => Chex, _ => Gex
    };
}

public sealed class ExposureSnapshot
{
    [JsonPropertyName("schema_version")] public int Version { get; set; }
    public string Ticker { get; set; } = "";
    public string Basis { get; set; } = "";
    public string Model { get; set; } = "";
    [JsonPropertyName("snapshot_utc")] public DateTimeOffset? SnapshotUtc { get; set; }
    [JsonPropertyName("generated_utc")] public DateTimeOffset? GeneratedUtc { get; set; }
    [JsonPropertyName("source_timestamp")] public string? SourceTimestamp { get; set; }
    [JsonPropertyName("source_is_stale")] public bool SourceIsStale { get; set; }
    public bool Stale { get; set; }
    public string Status { get; set; } = "";
    public string Expiry { get; set; } = "";
    public decimal? Spot { get; set; }
    [JsonPropertyName("included_expiries")] public string[] IncludedExpiries { get; set; } = [];
    [JsonPropertyName("net_totals")] public Dictionary<string, double?> Totals { get; set; } = [];
    public Dictionary<string, decimal?> Levels { get; set; } = [];
    public ExposureRow[] Rows { get; set; } = [];

    public static readonly JsonSerializerOptions JsonOptions = new() { PropertyNameCaseInsensitive = true };
    public static ExposureSnapshot Parse(string json)
    {
        var result = JsonSerializer.Deserialize<ExposureSnapshot>(json, JsonOptions)
            ?? throw new FormatException("Empty exposure response");
        if (result.Version != 1 || result.Ticker != "QQQ" || result.Rows is null || result.Rows.Length == 0 ||
            result.Totals is null || result.Levels is null || result.IncludedExpiries is null ||
            result.Basis is not ("intraday" or "eod") || result.Model != "classic_oi_type_sign_estimate")
            throw new FormatException("Unsupported or empty exposure payload");
        if (result.Rows.Any(r => r.Strike <= 0))
            throw new FormatException("Invalid source strike");
        if (result.Rows.Select(r => r.Strike).Distinct().Count() != result.Rows.Length)
            throw new FormatException("Duplicate source strike");
        return result;
    }
}

public sealed class CloseReference
{
    public string Ticker { get; set; } = "";
    public decimal Close { get; set; }
    [JsonPropertyName("bar_start_utc")] public DateTimeOffset BarStartUtc { get; set; }
    [JsonPropertyName("reference_date")] public string ReferenceDate { get; set; } = "";
}

public sealed record LockedMapping(string SessionDate, string Instrument, decimal Ratio,
    DateTimeOffset? ReferenceUtc, string Mode)
{
    public decimal Map(decimal strike, decimal tick)
    {
        if (Ratio <= 0 || tick <= 0 || strike <= 0)
            throw new ArgumentOutOfRangeException(nameof(strike), "Positive price, ratio and tick required");
        // Pine math.round_to_mintick rounds a positive half tick upwards.
        return decimal.Round(strike * Ratio / tick, 0, MidpointRounding.AwayFromZero) * tick;
    }

    public static LockedMapping FromCloses(string day, string instrument, decimal nq,
        DateTimeOffset nqTime, CloseReference qqq)
    {
        if (nq <= 0 || qqq.Close <= 0 || qqq.Ticker != "QQQ" || nqTime != qqq.BarStartUtc)
            throw new InvalidOperationException("NQ and QQQ must have the same completed 15:59 minute");
        var ny = TimeZoneInfo.ConvertTime(nqTime, SessionClock.NewYork);
        if (ny.Hour != 15 || ny.Minute != 59 || ny.Second != 0 ||
            ny.ToString("yyyy-MM-dd") != qqq.ReferenceDate ||
            string.CompareOrdinal(qqq.ReferenceDate, day) >= 0)
            throw new InvalidOperationException("Reference must be a prior-session 15:59 New York bar");
        return new(day, instrument, nq / qqq.Close, nqTime, "AUTO 15:59");
    }
}

public static class SessionClock
{
    public static readonly TimeZoneInfo NewYork = TimeZoneInfo.FindSystemTimeZoneById("America/New_York");
    public static TimeZoneInfo Zone(bool followDst) => followDst ? NewYork :
        TimeZoneInfo.CreateCustomTimeZone("UTC-4", TimeSpan.FromHours(-4), "UTC-4", "UTC-4");
    public static string Day(DateTimeOffset now, bool followDst) =>
        TimeZoneInfo.ConvertTime(now, Zone(followDst)).ToString("yyyy-MM-dd");
    public static bool AfterLock(DateTimeOffset now, bool followDst, int hour, int minute)
    {
        var local = TimeZoneInfo.ConvertTime(now, Zone(followDst));
        return local.TimeOfDay >= new TimeSpan(Math.Clamp(hour, 0, 23), Math.Clamp(minute, 0, 59), 0);
    }
    public static double Width(double value, double maximum, bool sqrt) => maximum <= 0 ? 0 :
        Math.Clamp(sqrt ? Math.Sqrt(Math.Abs(value) / maximum) : Math.Abs(value) / maximum, 0, 1);
    public static string WallKey(ExposureMetric m) => m switch {
        ExposureMetric.Dex => "dex_wall_abs", ExposureMetric.Vex => "vanna_wall_abs",
        ExposureMetric.Chex => "charm_wall_abs", _ => "gamma_wall_abs"
    };
    public static string FlipKey(ExposureMetric m) => m switch {
        ExposureMetric.Dex => "delta_flip", ExposureMetric.Vex => "vanna_flip",
        ExposureMetric.Chex => "charm_flip", _ => "gamma_flip"
    };
}
