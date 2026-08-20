"""Runtime vocabulary authority — single source for the entity catalogs.

datasets/knowledge/manifest.yaml is the declared authority for product / region
/ unit catalogs, but until this loader nothing read it at runtime: classify_intent,
routing and recovery_decision each carried their own hardcoded mirror and drifted
from it silently. This module is the one place the runtime reads those catalogs;
consumers read get_catalog() so a manifest change propagates instead of diverging.

The module-level lazy cache mirrors skill_matcher's pattern: the catalog loads
once per process and is dropped by invalidate() (or rebuilt on a TTL when
vocabulary_llm_updates_enabled is on — the DB merge lands in a later phase).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

import yaml
from pydantic import BaseModel

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


def get_catalog() -> VocabularyCatalog:
    """Return the merged runtime catalog, cached per process until invalidate()."""
    global _CACHE
    if _CACHE is None:
        _CACHE = merge_catalog(load_manifest_catalog(), ())
    return _CACHE


def invalidate() -> None:
    """Drop the cached catalog so the next get_catalog() reloads the manifest."""
    global _CACHE
    _CACHE = None


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
