from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


BASE_DIR = Path("/Users/nguyenvokhang/Downloads/quant-research")
DATA_PATH = BASE_DIR / "combined_1_7_dedup.csv"
OUT_DIR = BASE_DIR / "range69_visuals"

# CSV timestamps appear to be UTC+7 while the indicator is fixed UTC-4.
CSV_TO_INDICATOR_HOURS = -11

RANGE69_START = 6 * 60
RANGE69_END = 8 * 60 + 59
PREMARKET_START = 7 * 60
PREMARKET_END = 9 * 60
POST_START = 9 * 60 + 1
POST_END = 16 * 60 + 14

WINDOWS = [
    ("09:30-09:40", 9 * 60 + 30, 9 * 60 + 40),
    ("09:40-09:50", 9 * 60 + 40, 9 * 60 + 50),
    ("09:50-10:10", 9 * 60 + 50, 10 * 60 + 10),
    ("10:20-10:30", 10 * 60 + 20, 10 * 60 + 30),
]


def pct(part, total):
    return np.nan if total == 0 else part / total * 100


def load_data():
    df = pd.read_csv(
        DATA_PATH,
        sep=";",
        header=None,
        names=["dt_local", "open", "high", "low", "close"],
    )
    df["dt_local"] = pd.to_datetime(df["dt_local"], format="%Y-%d-%m %H:%M:%S")
    df["dt_indicator"] = df["dt_local"] + pd.Timedelta(hours=CSV_TO_INDICATOR_HOURS)
    df = df.sort_values("dt_indicator").reset_index(drop=True)
    df["session_date"] = df["dt_indicator"].dt.date
    df["tod_min"] = df["dt_indicator"].dt.hour * 60 + df["dt_indicator"].dt.minute
    return df


def first_window_break_outcome(window_rows, post_rows, side, high_level, low_level, size, ext):
    if window_rows.empty or post_rows.empty or size <= 0:
        return {"broke": False}

    if side == "up":
        break_idx = window_rows.index[window_rows["close"] > high_level]
        if len(break_idx) == 0:
            return {"broke": False}
        first_idx = break_idx[0]
        after = post_rows.loc[post_rows.index >= first_idx]
        target = high_level + size * ext
        reverse_level = high_level
        for _, bar in after.iterrows():
            if bar["high"] >= target:
                outcome = "continue"
                break
            if bar["close"] <= reverse_level:
                outcome = "return"
                break
        else:
            outcome = "neither"
        break_bar = window_rows.loc[first_idx]
        return {
            "broke": True,
            "outcome": outcome,
            "break_time": break_bar["dt_indicator"],
            "break_open": break_bar["open"],
            "break_high": break_bar["high"],
            "break_low": break_bar["low"],
            "break_close": break_bar["close"],
            "target": target,
            "reverse_level": reverse_level,
        }

    break_idx = window_rows.index[window_rows["close"] < low_level]
    if len(break_idx) == 0:
        return {"broke": False}
    first_idx = break_idx[0]
    after = post_rows.loc[post_rows.index >= first_idx]
    target = low_level - size * ext
    reverse_level = low_level
    for _, bar in after.iterrows():
        if bar["low"] <= target:
            outcome = "continue"
            break
        if bar["close"] >= reverse_level:
            outcome = "return"
            break
    else:
        outcome = "neither"
    break_bar = window_rows.loc[first_idx]
    return {
        "broke": True,
        "outcome": outcome,
        "break_time": break_bar["dt_indicator"],
        "break_open": break_bar["open"],
        "break_high": break_bar["high"],
        "break_low": break_bar["low"],
        "break_close": break_bar["close"],
        "target": target,
        "reverse_level": reverse_level,
    }


def already_broke_before(day, side, high_level, low_level, start_min):
    prior = day[(day["tod_min"] >= POST_START) & (day["tod_min"] < start_min)]
    if side == "up":
        return bool((prior["close"] > high_level).any())
    return bool((prior["close"] < low_level).any())


def level_at_or_before(day, minute, col):
    rows = day[day["tod_min"] <= minute]
    if rows.empty:
        return np.nan
    return rows.iloc[-1][col]


def level_at_or_after(day, minute, col):
    rows = day[day["tod_min"] >= minute]
    if rows.empty:
        return np.nan
    return rows.iloc[0][col]


def build_events(df):
    daily_rows = []
    event_rows = []

    for session_date in sorted(df["session_date"].unique()):
        day = df[df["session_date"] == session_date]
        range69 = day[(day["tod_min"] >= RANGE69_START) & (day["tod_min"] <= RANGE69_END)]
        premarket = day[(day["tod_min"] >= PREMARKET_START) & (day["tod_min"] <= PREMARKET_END)]
        post = day[(day["tod_min"] >= POST_START) & (day["tod_min"] <= POST_END)]
        if len(range69) < 120 or len(premarket) < 90 or len(post) < 120:
            continue

        r_high = range69["high"].max()
        r_low = range69["low"].min()
        r_size = r_high - r_low
        p_high = premarket["high"].max()
        p_low = premarket["low"].min()
        p_size = p_high - p_low
        if r_size <= 0 or p_size <= 0:
            continue

        open_0930 = level_at_or_after(day, 9 * 60 + 30, "open")
        close_0929 = level_at_or_before(day, 9 * 60 + 29, "close")
        open_0900 = level_at_or_after(day, 9 * 60, "open")

        open_loc_pm = (open_0930 - p_low) / p_size
        open_loc_r69 = (open_0930 - r_low) / r_size
        preopen_momentum_pm = (close_0929 - open_0900) / p_size
        preopen_momentum_r69 = (close_0929 - open_0900) / r_size

        daily_rows.append(
            {
                "session_date": session_date,
                "range69_high": r_high,
                "range69_low": r_low,
                "range69_size": r_size,
                "premarket_high": p_high,
                "premarket_low": p_low,
                "premarket_size": p_size,
                "open_0930": open_0930,
                "open_loc_pm": open_loc_pm,
                "open_loc_r69": open_loc_r69,
                "preopen_momentum_pm": preopen_momentum_pm,
                "preopen_momentum_r69": preopen_momentum_r69,
                "r69_inside_pm": r_high <= p_high and r_low >= p_low,
                "same_high_as_pm": r_high == p_high,
                "same_low_as_pm": r_low == p_low,
            }
        )

        bases = [
            ("r69", "Range69", r_high, r_low, r_size, 0.33),
            ("pm", "PreMarket", p_high, p_low, p_size, 0.111),
        ]

        for window_label, start_min, end_min in WINDOWS:
            window = day[(day["tod_min"] >= start_min) & (day["tod_min"] <= end_min)]
            if len(window) < max(3, int((end_min - start_min + 1) * 0.5)):
                continue

            for base_key, base_label, high_level, low_level, size, ext in bases:
                for side in ["up", "down"]:
                    result = first_window_break_outcome(
                        window, post, side, high_level, low_level, size, ext
                    )
                    if not result["broke"]:
                        continue

                    already = already_broke_before(day, side, high_level, low_level, start_min)
                    candle_range = result["break_high"] - result["break_low"]
                    body = abs(result["break_close"] - result["break_open"])
                    body_ratio = np.nan if candle_range == 0 else body / candle_range
                    if side == "up":
                        close_position = (
                            np.nan
                            if candle_range == 0
                            else (result["break_close"] - result["break_low"]) / candle_range
                        )
                        room_to_r69_033 = (r_high + r_size * 0.33 - result["break_close"]) / p_size
                    else:
                        close_position = (
                            np.nan
                            if candle_range == 0
                            else (result["break_high"] - result["break_close"]) / candle_range
                        )
                        room_to_r69_033 = (result["break_close"] - (r_low - r_size * 0.33)) / p_size

                    event_rows.append(
                        {
                            "session_date": session_date,
                            "base": base_key,
                            "base_label": base_label,
                            "side": side,
                            "window": window_label,
                            "first_break_in_window": not already,
                            "outcome": result["outcome"],
                            "is_continue": result["outcome"] == "continue",
                            "is_return": result["outcome"] == "return",
                            "break_time": result["break_time"],
                            "break_close": result["break_close"],
                            "body_ratio": body_ratio,
                            "close_position": close_position,
                            "open_loc_pm": open_loc_pm,
                            "open_loc_r69": open_loc_r69,
                            "preopen_momentum_pm": preopen_momentum_pm,
                            "preopen_momentum_r69": preopen_momentum_r69,
                            "range69_size": r_size,
                            "premarket_size": p_size,
                            "room_to_r69_033": room_to_r69_033,
                            "r69_inside_pm": r_high <= p_high and r_low >= p_low,
                            "same_high_as_pm": r_high == p_high,
                            "same_low_as_pm": r_low == p_low,
                        }
                    )

    daily = pd.DataFrame(daily_rows)
    events = pd.DataFrame(event_rows)
    daily["range69_size_ratio_20d"] = daily["range69_size"] / daily["range69_size"].rolling(
        20, min_periods=10
    ).median()
    daily["premarket_size_ratio_20d"] = daily["premarket_size"] / daily[
        "premarket_size"
    ].rolling(20, min_periods=10).median()
    events = events.merge(
        daily[
            [
                "session_date",
                "range69_size_ratio_20d",
                "premarket_size_ratio_20d",
            ]
        ],
        on="session_date",
        how="left",
    )
    return daily, events


def summarize_events(events, first_only=False):
    data = events[events["first_break_in_window"]] if first_only else events
    group_cols = ["base_label", "side", "window"]
    rows = []
    for keys, group in data.groupby(group_cols):
        total = len(group)
        cont = int((group["outcome"] == "continue").sum())
        ret = int((group["outcome"] == "return").sum())
        rows.append(
            {
                "base": keys[0],
                "side": keys[1],
                "window": keys[2],
                "sample": total,
                "continue": cont,
                "return": ret,
                "neither": int((group["outcome"] == "neither").sum()),
                "continue_pct": pct(cont, total),
                "return_pct": pct(ret, total),
            }
        )
    return pd.DataFrame(rows)


def bin_summary(events, base, side, feature, bins, labels, first_only=True):
    data = events[(events["base"] == base) & (events["side"] == side)].copy()
    if first_only:
        data = data[data["first_break_in_window"]]
    data = data[np.isfinite(data[feature])]
    data["bin"] = pd.cut(data[feature], bins=bins, labels=labels, include_lowest=True)
    rows = []
    for label, group in data.groupby("bin", observed=False):
        total = len(group)
        cont = int((group["outcome"] == "continue").sum())
        ret = int((group["outcome"] == "return").sum())
        rows.append(
            {
                "base": base,
                "side": side,
                "feature": feature,
                "bin": str(label),
                "sample": total,
                "continue_pct": pct(cont, total),
                "return_pct": pct(ret, total),
            }
        )
    return pd.DataFrame(rows)


def save_table_png(table, title, path, cols=None):
    table = table.copy()
    if cols:
        table = table[cols]
    fig_h = max(2.8, 0.35 * len(table) + 1.1)
    fig, ax = plt.subplots(figsize=(12, fig_h))
    ax.axis("off")
    ax.set_title(title, fontsize=14, pad=14)
    display_table = table.copy()
    for col in display_table.columns:
        if display_table[col].dtype.kind in "fc":
            display_table[col] = display_table[col].map(lambda x: "" if pd.isna(x) else f"{x:.2f}")
    tbl = ax.table(
        cellText=display_table.values,
        colLabels=display_table.columns,
        cellLoc="center",
        loc="center",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9)
    tbl.scale(1, 1.35)
    fig.tight_layout()
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def heatmap(summary, title, path, base, side):
    data = summary[(summary["base"] == base) & (summary["side"] == side)]
    matrix = data.pivot(index="side", columns="window", values="continue_pct").reindex(
        columns=[w[0] for w in WINDOWS]
    )
    fig, ax = plt.subplots(figsize=(10.5, 2.4))
    arr = matrix.to_numpy()
    im = ax.imshow(arr, cmap="RdYlGn", vmin=0, vmax=100, aspect="auto")
    ax.set_title(title, fontsize=13)
    ax.set_xticks(np.arange(len(matrix.columns)), labels=matrix.columns, rotation=25, ha="right")
    ax.set_yticks(np.arange(len(matrix.index)), labels=matrix.index)
    for i in range(arr.shape[0]):
        for j in range(arr.shape[1]):
            value = arr[i, j]
            if np.isfinite(value):
                sample = int(data[data["window"] == matrix.columns[j]]["sample"].iloc[0])
                ax.text(j, i, f"{value:.1f}%\nn={sample}", ha="center", va="center", fontsize=10)
    fig.colorbar(im, ax=ax, label="Continue %")
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def stacked_bar(summary, title, path, base):
    data = summary[summary["base"] == base].copy()
    data["label"] = data["window"] + " " + data["side"]
    data = data.sort_values(["window", "side"], key=lambda s: s.map({w[0]: i for i, w in enumerate(WINDOWS)}).fillna(s))
    x = np.arange(len(data))
    fig, ax = plt.subplots(figsize=(13, 5.2))
    ax.bar(x, data["continue_pct"], color="#2563eb", label="Continue")
    ax.bar(x, data["return_pct"], bottom=data["continue_pct"], color="#f97316", label="Return")
    ax.set_title(title, fontsize=14)
    ax.set_ylabel("% of break cases")
    ax.set_ylim(0, 105)
    ax.set_xticks(x, data["label"], rotation=35, ha="right")
    for idx, row in enumerate(data.itertuples()):
        ax.text(idx, 102, f"n={row.sample}", ha="center", va="bottom", fontsize=8)
    ax.legend()
    ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def feature_bar(feature_summary, title, path):
    data = feature_summary.copy()
    fig, ax = plt.subplots(figsize=(11, 4.8))
    x = np.arange(len(data))
    ax.bar(x, data["continue_pct"], color="#16a34a")
    ax.set_title(title, fontsize=14)
    ax.set_ylabel("Continue %")
    ax.set_ylim(0, 105)
    ax.set_xticks(x, data["bin"], rotation=30, ha="right")
    for idx, row in enumerate(data.itertuples()):
        ax.text(idx, row.continue_pct + 2 if np.isfinite(row.continue_pct) else 2, f"n={row.sample}", ha="center", fontsize=8)
    ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def main():
    OUT_DIR.mkdir(exist_ok=True)
    df = load_data()
    daily, events = build_events(df)
    any_summary = summarize_events(events, first_only=False)
    first_summary = summarize_events(events, first_only=True)

    daily.to_csv(OUT_DIR / "daily_features.csv", index=False)
    events.to_csv(OUT_DIR / "break_events.csv", index=False)
    any_summary.to_csv(OUT_DIR / "window_any_break_summary.csv", index=False)
    first_summary.to_csv(OUT_DIR / "window_first_break_summary.csv", index=False)

    save_table_png(
        any_summary.sort_values(["base", "side", "window"]),
        "Any close-break inside each window",
        OUT_DIR / "table_any_break_summary.png",
        ["base", "side", "window", "sample", "continue_pct", "return_pct"],
    )
    save_table_png(
        first_summary.sort_values(["base", "side", "window"]),
        "First close-break inside each window",
        OUT_DIR / "table_first_break_summary.png",
        ["base", "side", "window", "sample", "continue_pct", "return_pct"],
    )

    for summary, prefix, label in [
        (any_summary, "any", "Any break"),
        (first_summary, "first", "First break only"),
    ]:
        for base in ["Range69", "PreMarket"]:
            stacked_bar(
                summary,
                f"{label}: {base} continue vs return by time window",
                OUT_DIR / f"{prefix}_{base.lower()}_stacked_bar.png",
                base,
            )
            for side in ["up", "down"]:
                heatmap(
                    summary,
                    f"{label}: {base} {side} continue %",
                    OUT_DIR / f"{prefix}_{base.lower()}_{side}_heatmap.png",
                    base,
                    side,
                )

    feature_tables = []
    feature_specs = [
        (
            "open_loc_pm",
            [-np.inf, 0.0, 0.2, 0.4, 0.6, 0.8, 1.0, np.inf],
            ["<0", "0-0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "0.8-1", ">1"],
        ),
        (
            "premarket_size_ratio_20d",
            [-np.inf, 0.7, 1.0, 1.3, np.inf],
            ["small <0.7", "0.7-1.0", "1.0-1.3", "large >1.3"],
        ),
        (
            "preopen_momentum_pm",
            [-np.inf, -0.3, -0.1, 0.1, 0.3, np.inf],
            ["strong down", "mild down", "flat", "mild up", "strong up"],
        ),
        (
            "body_ratio",
            [-np.inf, 0.25, 0.5, 0.75, np.inf],
            ["weak body", "mid body", "strong body", "very strong"],
        ),
        (
            "close_position",
            [-np.inf, 0.25, 0.5, 0.75, np.inf],
            ["bad close", "mid close", "good close", "extreme close"],
        ),
    ]

    for feature, bins, labels in feature_specs:
        for base in ["pm", "r69"]:
            for side in ["up", "down"]:
                table = bin_summary(events, base, side, feature, bins, labels, first_only=True)
                feature_tables.append(table)
                feature_bar(
                    table,
                    f"First-break continue % by {feature} | {base.upper()} {side}",
                    OUT_DIR / f"feature_{base}_{side}_{feature}.png",
                )

    feature_summary = pd.concat(feature_tables, ignore_index=True)
    feature_summary.to_csv(OUT_DIR / "feature_bin_summary.csv", index=False)
    save_table_png(
        feature_summary[
            (feature_summary["base"] == "pm")
            & (feature_summary["side"].isin(["up", "down"]))
            & (feature_summary["feature"].isin(["open_loc_pm", "body_ratio", "premarket_size_ratio_20d"]))
        ],
        "Selected feature bins: first-break continue %",
        OUT_DIR / "table_selected_feature_bins.png",
        ["base", "side", "feature", "bin", "sample", "continue_pct", "return_pct"],
    )

    print(f"Saved visuals and tables to: {OUT_DIR}")
    print(f"Daily feature rows: {len(daily)}")
    print(f"Break event rows: {len(events)}")


if __name__ == "__main__":
    main()
