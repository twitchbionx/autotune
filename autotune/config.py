"""Config loader -- minimal YAML-ish parser to avoid adding a PyYAML
dependency. Accepts a simple `key: value` format with `#` comments and
no nesting beyond what we need."""

from __future__ import annotations

import logging
from dataclasses import fields
from pathlib import Path
from typing import Any

from .tuner import TunerConfig

log = logging.getLogger(__name__)


def _coerce(raw: str, target_type: Any) -> Any:
    s = raw.strip()
    if target_type is int:
        return int(s)
    if target_type is float:
        return float(s)
    if target_type is bool:
        return s.lower() in ("1", "true", "yes", "on")
    if target_type is Path:
        return Path(s.strip('"').strip("'"))
    return s.strip('"').strip("'")


def load_tuner_config(path: Path) -> TunerConfig:
    if not path.exists():
        raise FileNotFoundError(path)

    values: dict[str, Any] = {}
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        # strip comments
        if "#" in line:
            line = line.split("#", 1)[0]
        line = line.rstrip()
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"{path}:{lineno}: expected 'key: value'")
        key, _, val = line.partition(":")
        values[key.strip()] = val

    # Type-coerce by inspecting TunerConfig field types
    field_types = {f.name: f.type for f in fields(TunerConfig)}
    out: dict[str, Any] = {}
    for k, v in values.items():
        if k not in field_types:
            log.warning("Unknown config key '%s' (ignored)", k)
            continue
        t = field_types[k]
        # Field types are strings under `from __future__ import annotations`;
        # resolve the common ones manually.
        t_map = {
            "int": int, "float": float, "bool": bool, "str": str, "Path": Path,
        }
        out[k] = _coerce(v, t_map.get(str(t), str))

    return TunerConfig(**out)
