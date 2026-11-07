"""Lead features, the shared Cohort object, and time helpers.

A lead's four features are packed into one small int (`tup`, 4 bits per feature)
so every aggregate can be keyed by the full feature tuple. Any cohort or segment
is then just a set of tups, which is what lets detection, experiments, dry runs
and the supervisor view all use *the same* `Cohort` (Product memo, §2).
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

import numpy as np

from .settings import MIN_PER_DAY

D0 = dt.date(2027, 1, 4)  # documented in docs/SIMULATOR.md §1 (D0 00:00 IST)
_date_cache: dict[str, int] = {}

DEFAULT_FEATURES = ["lang", "city_tier", "age_band", "value_band"]
BITS = 4
MAXV = 1 << BITS


def ts_to_minute(ts: str) -> int:
    d = ts[:10]
    off = _date_cache.get(d)
    if off is None:
        off = (dt.date.fromisoformat(d) - D0).days * MIN_PER_DAY
        _date_cache[d] = off
    return off + int(ts[11:13]) * 60 + int(ts[14:16])


def label(minute: int) -> str:
    day, mod = divmod(int(minute), MIN_PER_DAY)
    return f"D{day} {mod // 60:02d}:{mod % 60:02d}"


def parse_label(s: str) -> int:
    s = s.strip()
    m = re.fullmatch(r"[Dd](-?\d+)(?:\s+(\d{1,2}):(\d{2}))?", s)
    if not m:
        return int(s)
    return int(m.group(1)) * MIN_PER_DAY + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)


def day_of(minute: int) -> int:
    return int(minute) // MIN_PER_DAY


def hour_of(minute: int) -> int:
    return (int(minute) % MIN_PER_DAY) // 60


class FeatureSpace:
    """Feature vocabularies, discovered from lead events and persisted (codes never change)."""

    def __init__(self, store=None):
        self.store = store
        self.names: list[str] = list(DEFAULT_FEATURES)
        self.codes: dict[str, dict[str, int]] = {f: {} for f in self.names}
        if store is not None:
            saved = store.meta("feature_names")
            if saved:
                self.names = saved
                self.codes = {f: {} for f in self.names}
            for f in self.names:
                self.codes[f] = store.vocab(f"feat:{f}")
        self._rev = None

    def encode(self, feats: dict) -> int:
        t = 0
        for i, f in enumerate(self.names):
            v = feats.get(f)
            c = self.codes[f].get(v)
            if c is None:
                c = len(self.codes[f])
                if c >= MAXV:
                    raise ValueError(f"feature {f} has more than {MAXV} values")
                self.codes[f][v] = c
                self._rev = None
                if self.store is not None:
                    self.store.add_vocab(f"feat:{f}", v, c)
            t |= c << (BITS * i)
        return t

    def rev(self) -> dict[str, dict[int, str]]:
        if self._rev is None:
            self._rev = {f: {c: n for n, c in self.codes[f].items()} for f in self.names}
        return self._rev

    def values(self, f: str) -> list[str]:
        return sorted(self.codes[f], key=_natural)

    def col(self, tups: np.ndarray, f: str) -> np.ndarray:
        i = self.names.index(f)
        return (np.asarray(tups) >> (BITS * i)) & (MAXV - 1)

    def decode(self, tup: int) -> dict[str, str]:
        r = self.rev()
        return {f: r[f].get((tup >> (BITS * i)) & (MAXV - 1)) for i, f in enumerate(self.names)}

    def where_mask(self, where: dict, tups: np.ndarray) -> np.ndarray:
        tups = np.asarray(tups)
        m = np.ones(len(tups), dtype=bool)
        for f, vals in (where or {}).items():
            if f not in self.codes:
                return np.zeros(len(tups), dtype=bool)
            codes = [self.codes[f][v] for v in vals if v in self.codes[f]]
            m &= np.isin(self.col(tups, f), codes)
        return m


def _natural(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(s))]


FEAT_SHORT = {"lang": "lang", "city_tier": "tier", "age_band": "age", "value_band": "value"}


@dataclass
class Cohort:
    """THE cohort object. Same id in the config, in detection, in experiments and in the view."""
    id: str
    where: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        if not self.where:
            return "everyone else" if self.id == "default" else self.id
        parts = []
        for f, vals in self.where.items():
            parts.append(f"{FEAT_SHORT.get(f, f)} {'/'.join(vals)}")
        return " · ".join(parts)

    def to_config(self) -> dict:
        return {"id": self.id, "where": {k: list(v) for k, v in self.where.items()}}

    def mask(self, fs: FeatureSpace, tups) -> np.ndarray:
        return fs.where_mask(self.where, tups)


def config_cohorts(cfg: dict) -> list[Cohort]:
    return [Cohort(c["id"], c.get("where") or {}) for c in cfg.get("cohorts") or []]


def assign(cfg: dict, fs: FeatureSpace, tups) -> np.ndarray:
    """Cohort name per tup under `cfg`, first-match semantics (same rule the dialler uses)."""
    tups = np.asarray(tups)
    out = np.full(len(tups), "default", dtype=object)
    free = np.ones(len(tups), dtype=bool)
    for c in config_cohorts(cfg):
        m = free & c.mask(fs, tups)
        out[m] = c.id
        free &= ~m
    return out


def cohort_id(where: dict) -> str:
    parts = []
    for f, vals in where.items():
        parts.append(f"{FEAT_SHORT.get(f, f)}={'+'.join(vals)}")
    return "&".join(parts) or "all"


def design_cohorts(fs: FeatureSpace, tup_counts: dict[int, float], primary: str, secondary: str | None,
                   max_cohorts: int = 20, min_share: float = 0.03) -> list[Cohort]:
    """Partition the population into primary × secondary cells.

    Cells below `min_share` of leads are merged (within their primary group) into
    one "other" cell, so no cohort is too small to carry its own allocation, and
    the total never exceeds the 20-cohort limit. Every cohort is disjoint, so the
    config's first-match order does not matter.
    """
    total = float(sum(tup_counts.values())) or 1.0
    tups = np.array(list(tup_counts), dtype=np.int64)
    cnt = np.array([tup_counts[t] for t in tups], dtype=float)

    def share(mask):
        return cnt[mask].sum() / total

    p_vals = [v for v in fs.values(primary) if share(fs.where_mask({primary: [v]}, tups)) > 0]
    # merge tiny primary groups into their neighbour
    groups: list[list[str]] = []
    for v in p_vals:
        if groups and share(fs.where_mask({primary: groups[-1]}, tups)) < min_share * 2:
            groups[-1].append(v)
        else:
            groups.append([v])
    if len(groups) > 1 and share(fs.where_mask({primary: groups[-1]}, tups)) < min_share * 2:
        last = groups.pop()
        groups[-1].extend(last)

    cohorts: list[Cohort] = []
    for g in groups:
        gm = fs.where_mask({primary: g}, tups)
        if not secondary:
            cohorts.append(Cohort(cohort_id({primary: g}), {primary: g}))
            continue
        s_vals = [v for v in fs.values(secondary) if share(gm & fs.where_mask({secondary: [v]}, tups)) > 0]
        big = [v for v in s_vals if share(gm & fs.where_mask({secondary: [v]}, tups)) >= min_share]
        small = [v for v in s_vals if v not in big]
        if small and big and share(gm & fs.where_mask({secondary: small}, tups)) < min_share:
            # fold the leftovers into the smallest big cell
            big.sort(key=lambda v: share(gm & fs.where_mask({secondary: [v]}, tups)))
            merged = [big[0]] + small
            cells = [[v] for v in big[1:]] + [merged]
        else:
            cells = [[v] for v in big] + ([small] if small else [])
        if len(cells) <= 1:
            cohorts.append(Cohort(cohort_id({primary: g}), {primary: g}))
            continue
        for c in cells:
            where = {primary: g, secondary: sorted(c, key=_natural)}
            cohorts.append(Cohort(cohort_id(where), where))
    # enforce the hard cap by merging the two smallest cells sharing a primary group
    while len(cohorts) > max_cohorts:
        sizes = [share(c.mask(fs, tups)) for c in cohorts]
        order = np.argsort(sizes)
        a = cohorts[order[0]]
        partner = next((cohorts[j] for j in order[1:] if cohorts[j].where.get(primary) == a.where.get(primary)
                        and cohorts[j] is not a), None)
        if partner is None:
            break
        where = {primary: a.where[primary]}
        if secondary:
            where[secondary] = sorted(set(a.where.get(secondary, [])) | set(partner.where.get(secondary, [])),
                                      key=_natural)
        merged = Cohort(cohort_id(where), where)
        cohorts = [c for c in cohorts if c is not a and c is not partner] + [merged]
    return cohorts
