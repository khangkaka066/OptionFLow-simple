"""Delete data/ files that are never read back by any script in this repo.

Targets only confirmed orphaned/write-only artifacts:
  1. data/market/session_levels.csv, data/market/session_levels.parquet (dead)
  2. data/market/ticker=*/date=*/*.csv (CSV siblings of the parquet tables; unread)
  3. data/options/iv_rank_history.csv (write-only cache)
  4. data/options/snapshot.log (unreferenced by any script)
  5. Empty data/options/<date>/raw/ directories left after storage.delete_raw()

Does NOT touch: timestamped per-run files, replay_index.jsonl, "latest" files,
interactive.html/levels.txt, data/options/<date>/history/, data/market/**/*.parquet,
data/market/**/*.sqlite, or anything under history/.

Usage:
  python3 scripts/cleanup_dead_data.py            # dry run, prints what would be deleted
  python3 scripts/cleanup_dead_data.py --apply    # actually delete
"""

from __future__ import annotations

import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_MARKET = PROJECT_ROOT / "data" / "market"
DATA_OPTIONS = PROJECT_ROOT / "data" / "options"


def find_targets() -> list[Path]:
    targets: list[Path] = []

    for name in ("session_levels.csv", "session_levels.parquet"):
        path = DATA_MARKET / name
        if path.is_file():
            targets.append(path)

    if DATA_MARKET.is_dir():
        targets.extend(sorted(DATA_MARKET.glob("ticker=*/date=*/*.csv")))

    iv_rank_csv = DATA_OPTIONS / "iv_rank_history.csv"
    if iv_rank_csv.is_file():
        targets.append(iv_rank_csv)

    snapshot_log = DATA_OPTIONS / "snapshot.log"
    if snapshot_log.is_file():
        targets.append(snapshot_log)

    empty_raw_dirs: list[Path] = []
    if DATA_OPTIONS.is_dir():
        for raw_dir in sorted(DATA_OPTIONS.glob("*/raw")):
            if raw_dir.is_dir() and not any(raw_dir.iterdir()):
                empty_raw_dirs.append(raw_dir)

    return targets, empty_raw_dirs


def run_cleanup(apply: bool, verbose: bool = True) -> tuple[int, int, float]:
    """Find and optionally delete dead data files. Returns (files_deleted, dirs_removed, MB_freed)."""
    files, empty_dirs = find_targets()

    if not files and not empty_dirs:
        if verbose:
            print("Nothing to clean up.")
        return 0, 0, 0.0

    total_bytes = sum(f.stat().st_size for f in files)
    if verbose:
        print(f"{'Deleting' if apply else 'Would delete'} {len(files)} file(s), {total_bytes / 1e6:.2f} MB:")
        for f in files:
            print(f"  {f.relative_to(PROJECT_ROOT)} ({f.stat().st_size / 1e3:.1f} KB)")
        if empty_dirs:
            print(f"{'Removing' if apply else 'Would remove'} {len(empty_dirs)} empty raw/ dir(s):")
            for d in empty_dirs:
                print(f"  {d.relative_to(PROJECT_ROOT)}")

    if not apply:
        if verbose:
            print("\nDry run only. Re-run with --apply to delete.")
        return 0, 0, 0.0

    for f in files:
        f.unlink()
    for d in empty_dirs:
        d.rmdir()
    if verbose:
        print(f"\nDeleted {len(files)} file(s) ({total_bytes / 1e6:.2f} MB) and removed {len(empty_dirs)} empty dir(s).")
    return len(files), len(empty_dirs), total_bytes / 1e6


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually delete files (default: dry run).")
    args = parser.parse_args()
    run_cleanup(apply=args.apply)


if __name__ == "__main__":
    main()
