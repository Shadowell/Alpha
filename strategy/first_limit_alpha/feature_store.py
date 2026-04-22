from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


def timestamp_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_version_dir(root: Path, category: str, prefix: str | None = None) -> Path:
    label = timestamp_tag()
    if prefix:
        label = f"{label}_{prefix}"
    return ensure_dir(root / category / label)


def write_dataframe(df: pd.DataFrame, path: Path) -> Path:
    ensure_dir(path.parent)
    df.to_csv(path, index=False)
    return path


def read_dataframe(path: str | Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    for col in ("symbol", "trade_date", "entry_date"):
        if col in frame.columns:
            frame[col] = frame[col].astype(str)
    return frame


def write_json(payload: dict[str, Any], path: Path) -> Path:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def latest_child(root: str | Path) -> Path | None:
    base = Path(root)
    if not base.exists():
        return None
    children = [item for item in base.iterdir() if item.is_dir()]
    if not children:
        return None
    return sorted(children)[-1]
