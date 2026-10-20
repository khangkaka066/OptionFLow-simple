using QuantResearch.Atas;

int passed = 0;
void Check(bool condition, string name) {
    if (!condition) throw new Exception("FAIL: " + name);
    passed++;
}
void Reject(Action action, string name) {
    bool rejected = false;
    try { action(); } catch { rejected = true; }
    Check(rejected, name);
}
var nqTime = DateTimeOffset.Parse("2026-10-16T19:59:00Z");
var reference = new CloseReference { Ticker = "QQQ", Close = 721m,
    BarStartUtc = nqTime, ReferenceDate = "2026-10-16" };
var mapping = LockedMapping.FromCloses("2026-10-19", "NQZ6", 30000m, nqTime, reference);
Check(mapping.Map(725m, .25m) == 30166.50m, "source strike conversion and NQ tick rounding");
Check(mapping.Ratio == 30000m / 721m, "ratio uses closes, not current spot");
Check(new LockedMapping("x", "NQ", 1m, null, "test").Map(1.125m, .25m) == 1.25m, "Pine positive half-tick rounding");
Reject(() => mapping.Map(725m, 0), "zero tick rejected");
Reject(() => mapping.Map(-1, .25m), "negative strike rejected");
Reject(() => LockedMapping.FromCloses("2026-10-19", "NQ", 30000m, nqTime.AddMinutes(-1), reference), "mismatched minute rejected");
Reject(() => LockedMapping.FromCloses("2026-10-16", "NQ", 30000m, nqTime, reference), "same-session close cannot leak into morning ratio");
var winter = DateTimeOffset.Parse("2026-12-01T13:30:00Z");
Check(SessionClock.AfterLock(winter, false, 9, 0), "Pine fixed UTC-4 default");
Check(!SessionClock.AfterLock(winter, true, 9, 0), "New York DST-aware clock differs in winter");
Check(SessionClock.AfterLock(DateTimeOffset.Parse("2026-10-19T13:00:00Z"), true, 9, 0), "exact lock time accepted");
Check(SessionClock.Width(-25, 100, true) == .5, "sqrt scaling preserves magnitude of negative exposure");
Check(SessionClock.Width(25, 100, false) == .25, "linear scaling available");
Check(SessionClock.WallKey(ExposureMetric.Gex) != SessionClock.FlipKey(ExposureMetric.Gex), "wall is separate from flip");
var json = """
{"schema_version":1,"ticker":"QQQ","basis":"intraday","model":"classic_oi_type_sign_estimate",
 "net_totals":{"net_gex":-100},"levels":{"gamma_wall_abs":722,"gamma_flip":null},
 "rows":[{"strike":721,"net_gex":-80,"net_dex":null},{"strike":722,"net_gex":30}]}
""";
var snapshot = ExposureSnapshot.Parse(json);
Check(snapshot.Rows[0].Value(ExposureMetric.Gex) == -80, "signed source exposure preserved");
Check(snapshot.Rows[0].Value(ExposureMetric.Dex) is null, "missing exposure stays missing");
Check(snapshot.Levels["gamma_flip"] is null, "missing flip is not replaced by wall");
Reject(() => ExposureSnapshot.Parse(json.Replace("\"schema_version\":1", "\"schema_version\":2")), "schema version checked");
Reject(() => ExposureSnapshot.Parse(json.Replace("\"QQQ\"", "\"NDX\"")), "wrong symbol rejected");
Reject(() => ExposureSnapshot.Parse(json.Replace("\"strike\":722", "\"strike\":721")), "duplicate strikes rejected");
if (args.Length == 1) {
    var real = ExposureSnapshot.Parse(File.ReadAllText(args[0]));
    Check(real.Rows.Length > 0 && real.SnapshotUtc.HasValue, "Python server payload deserializes in C#");
}
Console.WriteLine($"{passed} checks passed.");
