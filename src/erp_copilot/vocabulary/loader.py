"""Runtime vocabulary authority — single source for the entity catalogs.

datasets/knowledge/manifest.yaml is the declared authority for product / region
/ unit catalogs, but until this loader nothing read it at runtime: classify_intent,
routing and recovery_decision each carried their own hardcoded mirror and drifted
from it silently. This module is the one place the runtime reads those catalogs;
consumers read get_catalog() so a manifest change propagates instead of diverging.

get_catalog() merges the manifest seed with approved vocabulary_terms rows when
the LLM-driven pipeline is enabled (vocabulary_llm_updates_enabled). The
module-level lazy cache mirrors skill_matcher's pattern: with the flag off the
catalog loads once per process and is dropped only by invalidate(); with the
flag on it also rebuilds on a TTL so approved terms reach the runtime without a
restart. A missing DB or not-yet-run migration degrades gracefully to the seed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from time import monotonic
from typing import NamedTuple

import yaml
from pydantic import BaseModel
from sqlalchemy.exc import ProgrammingError

from erp_copilot.infrastructure.config import Settings

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_MANIFEST_PATH = _ROOT / "datasets" / "knowledge" / "manifest.yaml"


class StatusLifecycle(BaseModel):
    """Order state machine from the manifest (drift guards read it directly)."""

    initial: str
    terminal: tuple[str, ...]
    transitions: dict[str, tuple[str, ...]]


class ManifestCatalog(BaseModel):
    """All catalogs the manifest declares, including the structural lifecycle."""

    products: tuple[str, ...]
    regions: tuple[str, ...]
    units: tuple[str, ...]
    order_status_lifecycle: StatusLifecycle


class VocabularyCatalog(BaseModel):
    """Runtime entity view — manifest seed merged with approved DB terms."""

    products: tuple[str, ...]
    regions: tuple[str, ...]
    units: tuple[str, ...]


class VocabularyTermLike(NamedTuple):
    """A mergeable term (vocab_type in {product, region, unit})."""

    vocab_type: str
    canonical: str


def load_manifest_catalog() -> ManifestCatalog:
    """Parse the manifest's catalogs (pure — no cache, no DB)."""
    with open(_MANIFEST_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    lifecycle = raw["order_status_lifecycle"]
    return ManifestCatalog(
        products=tuple(p["name"] for p in raw["products"]),
        regions=tuple(raw["supplier_region_enum"]),
        units=tuple(raw["product_unit_enum"]),
        order_status_lifecycle=StatusLifecycle(
            initial=lifecycle["initial"],
            terminal=tuple(lifecycle["terminal"]),
            transitions={k: tuple(v) for k, v in lifecycle["transitions"].items()},
        ),
    )


def merge_catalog(
    seed: ManifestCatalog,
    terms: Sequence[VocabularyTermLike],
) -> VocabularyCatalog:
    """Merge approved terms into the manifest seed — deduped, order-preserving.

    Pure and deterministic; the DB merge (later phase) calls this with the
    loaded vocabulary_terms rows, and unit tests exercise it directly. A
    canonical already present in the seed (or already appended) is skipped, so
    merging is idempotent across repeated loads.
    """
    products = list(seed.products)
    regions = list(seed.regions)
    units = list(seed.units)
    for term in terms:
        if term.vocab_type == "product" and term.canonical not in products:
            products.append(term.canonical)
        elif term.vocab_type == "region" and term.canonical not in regions:
            regions.append(term.canonical)
        elif term.vocab_type == "unit" and term.canonical not in units:
            units.append(term.canonical)
    return VocabularyCatalog(
        products=tuple(products),
        regions=tuple(regions),
        units=tuple(units),
    )


_CACHE: VocabularyCatalog | None = None
_CACHE_AT: float = 0.0


def _db_terms() -> list[VocabularyTermLike]:
    """Active approved vocabulary_terms rows (global scope) as mergeable terms.

    Imports are deferred so this module stays importable without a DB engine;
    a missing engine or not-yet-run migration degrades to the manifest seed.
    """
    try:
        from erp_copilot.domain.entities import VocabularyTerm
        from erp_copilot.infrastructure.database import get_session

        session = get_session()
        try:
            rows = (
                session.query(VocabularyTerm)
                .filter_by(is_active=True, tenant_id=None)
                .all()
            )
            return [
                VocabularyTermLike(vocab_type=row.vocab_type, canonical=row.canonical)
                for row in rows
            ]
        finally:
            session.close()
    except (RuntimeError, ProgrammingError):
        return []


def get_catalog() -> VocabularyCatalog:
    """Return the merged runtime catalog, cached per process until invalidate().

    With the flag off the cache is dropped only by invalidate(); with the flag
    on it also rebuilds on a TTL so approved terms reach the runtime without a
    restart. The manifest is read on every rebuild so a manifest change
    propagates to the process on the next rebuild, not only at restart.
    """
    global _CACHE, _CACHE_AT
    settings = Settings()  # type: ignore[call-arg]
    if (
        _CACHE is None
        or (
            settings.vocabulary_llm_updates_enabled
            and settings.vocabulary_cache_ttl_s > 0
            and monotonic() - _CACHE_AT > settings.vocabulary_cache_ttl_s
        )
    ):
        terms = _db_terms() if settings.vocabulary_llm_updates_enabled else ()
        _CACHE = merge_catalog(load_manifest_catalog(), terms)
        _CACHE_AT = monotonic()
    return _CACHE


def invalidate() -> None:
    """Drop the cached catalog so the next get_catalog() reloads the manifest."""
    global _CACHE, _CACHE_AT
    _CACHE = None
    _CACHE_AT = 0.0


# Compiled-regex cache keyed by (kind, catalog tuple) so a catalog change
# recompiles lazily instead of the old module-level regex going stale.
_PATTERN_CACHE: dict[tuple[str, tuple[str, ...]], re.Pattern] = {}


def get_quantity_pattern(units: tuple[str, ...]) -> re.Pattern:
    """Compile the quantity regex (classify_intent._QUANTITY_RE) for *units*."""
    key = ("quantity", units)
    if key not in _PATTERN_CACHE:
        alternatives = "|".join(re.escape(u) for u in units)
        _PATTERN_CACHE[key] = re.compile(r"(\d+(?:\.\d+)?)\s*(" + alternatives + r")?")
    return _PATTERN_CACHE[key]


def get_product_association_pattern(products: tuple[str, ...]) -> re.Pattern:
    """Compile the "X的供应商" regex (routing._PRODUCT_ASSOCIATION_RE) for *products*."""
    key = ("product_association", products)
    if key not in _PATTERN_CACHE:
        alternatives = "|".join(re.escape(p) for p in products)
        _PATTERN_CACHE[key] = re.compile(r"(?:" + alternatives + r")的供应商")
    return _PATTERN_CACHE[key]
