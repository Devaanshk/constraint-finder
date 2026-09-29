"""Save and load PR records as JSON, so real analyses can be shipped with the demo."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

_TS_KEYS = {"created_at", "ready_at", "first_commit_at", "merged_at", "submitted_at"}


def _encode(obj: Any) -> Any:
    if isinstance(obj, dt.datetime):
        return obj.isoformat()
    raise TypeError(type(obj))


def _decode(d: dict) -> dict:
    for k in _TS_KEYS & d.keys():
        if d[k]:
            d[k] = dt.datetime.fromisoformat(d[k])
    return d


def save(path: str | Path, repo: str, days: int, records: list[dict]) -> None:
    payload = {
        "repo": repo,
        "days": days,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "records": records,
    }
    Path(path).write_text(json.dumps(payload, default=_encode, indent=1))


def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(), object_hook=_decode)
