from __future__ import annotations

import numpy as np


def clean_value(value):
    if value is None:
        return None
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, (int, np.integer)):
        return int(value)
    return value


def clean_records(records: list[dict]) -> list[dict]:
    return [{key: clean_value(value) for key, value in row.items()} for row in records]
