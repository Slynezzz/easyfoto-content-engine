"""Плейбук: что публиковать. Форматы, хуки и темы с весами.

Выбор — «многорукий бандит» по каждому измерению отдельно: с вероятностью
explore_share берём случайный вариант (эксперимент), иначе — пропорционально
весу (то, что уже работает). Веса обновляет аналитик по метрикам.
"""
from __future__ import annotations

import copy
import json
import random
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DEFAULT_PATH = Path(__file__).with_name("default_playbook.json")
MIN_WEIGHT = 0.05


@dataclass
class Choice:
    format: str
    hook_id: str
    theme: str
    explore: bool
    occasion: str | None


def load(path: Path) -> dict:
    """Рабочая копия плейбука; при первом запуске — копия стандартного."""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    pb = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
    save(path, pb)
    return pb


def save(path: Path, pb: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(pb, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _in_window(today: date, start: str, end: str) -> bool:
    md = (today.month, today.day)
    s = tuple(int(x) for x in start.split("-"))
    e = tuple(int(x) for x in end.split("-"))
    if s <= e:
        return s <= md <= e
    return md >= s or md <= e  # окно через Новый год


def active_seasons(pb: dict, today: date) -> list[dict]:
    return [s for s in pb.get("seasons", []) if _in_window(today, s["from"], s["to"])]


def theme_weights(pb: dict, today: date) -> dict[str, float]:
    weights = {k: max(MIN_WEIGHT, v["weight"]) for k, v in pb["themes"].items()}
    active = active_seasons(pb, today)
    boosted = set()
    for s in active:
        for theme, k in s.get("boost", {}).items():
            if theme in weights:
                weights[theme] *= k
                boosted.add(theme)
    # Сезонные темы, у которых окно закрыто, не публикуем совсем.
    only_seasonal = {"newyear", "halloween"}
    for t in only_seasonal & set(weights):
        if t not in boosted:
            weights[t] = 0.0
    return weights


def _weighted(rng: random.Random, weights: dict[str, float], explore: bool) -> str:
    keys = [k for k, w in weights.items() if w > 0]
    if not keys:
        raise ValueError("нет доступных вариантов")
    if explore:
        return rng.choice(keys)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def choose(pb: dict, today: date, explore_share: float = 0.3,
           rng: random.Random | None = None, allowed_formats: set[str] | None = None) -> Choice:
    rng = rng or random.Random()
    explore = rng.random() < explore_share

    fmt_w = {k: max(MIN_WEIGHT, v["weight"]) for k, v in pb["formats"].items()
             if allowed_formats is None or k in allowed_formats}
    fmt = _weighted(rng, fmt_w, explore)

    hook_w = {k: max(MIN_WEIGHT, v["weight"]) for k, v in pb["hooks"].items() if fmt in v["formats"]}
    seasons = active_seasons(pb, today)
    if not seasons:
        hook_w.pop("season", None)
    hook = _weighted(rng, hook_w, explore)

    theme = _weighted(rng, theme_weights(pb, today), explore)
    occasion = seasons[0]["occasion"] if seasons else None
    return Choice(fmt, hook, theme, explore, occasion)


def reweight(pb: dict, scores: dict[str, dict[str, float]], note: str = "",
             insights: list[str] | None = None, learning_rate: float = 0.5) -> dict:
    """Новый плейбук: веса сдвигаются к относительной эффективности.

    scores = {"formats": {"before_after": 1.4, ...}, "hooks": {...}, "themes": {...}},
    где 1.0 — средний результат. Сдвиг сглажен, чтобы одна удачная
    неделя не обнулила остальные варианты.
    """
    new = copy.deepcopy(pb)
    base = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
    for dim in ("formats", "hooks", "themes"):
        for key, score in scores.get(dim, {}).items():
            if key not in new[dim]:
                continue
            w = new[dim][key]["weight"]
            # Цель считается от исходного веса, а не от текущего: при тех же
            # данных веса сходятся к base*score, а не растут каждую неделю.
            b = new[dim][key].get("base_weight") or base.get(dim, {}).get(key, {}).get("weight", 1.0)
            target = b * max(0.2, min(3.0, score))
            new[dim][key]["weight"] = round(max(MIN_WEIGHT, min(5.0, w + learning_rate * (target - w))), 3)
    if insights is not None:
        new["insights"] = insights[:12]
    new["version"] = pb.get("version", 1) + 1
    new["last_note"] = note
    return new
