"""Read-only maintenance & curation reports (Mnesis side of the dream-cycle).

The maintenance agent reaches Mnesis only over MCP, so the upkeep operations it
drives must live behind MCP tools. Two of them are *purely diagnostic* and are
defined here so the MCP server (and CLI) stay thin wrappers:

  - :func:`health_report` — a side-effect-free snapshot of system health, read
    from the store, the graph cache, the search index, and the state store. It
    computes nothing it cannot derive cheaply and writes nothing.
  - :func:`find_duplicates` — a **heuristic** near-duplicate finder. It proposes
    and changes nothing; it only surfaces candidate page pairs with a rationale,
    so a human or an agent can decide. The heuristic (title/tag overlap, shared
    graph edges, FTS co-retrieval) is a stand-in **pending Phase-5 vectors**,
    which will replace it with semantic similarity.

Both functions are strictly read-only (CLAUDE.md §8/§12): the only maintenance
*writers* are ``graph_lint(fix=True)`` and ``decay``, which live elsewhere and
are idempotent + git-audited.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime

from . import config, confidence, graph, graph_lint, search, state, store, tenancy
from .graph import PAGE_NODE_TYPE

# Pairs larger than this in a shared-tag/edge group are skipped as candidate
# generators — a popular tag (``project:atlas``) would otherwise yield O(k^2)
# weak pairs. Strong-signal scoring still ranks anything FTS co-retrieves.
_GROUP_CANDIDATE_CAP = 8

# Below this blended similarity a candidate pair is not worth surfacing.
_DUP_THRESHOLD = 0.25

# Blend weights for the duplicate heuristic (sum to 1.0).
_W_TITLE, _W_TAGS, _W_EDGES, _W_FTS = 0.35, 0.25, 0.25, 0.15


# --- Health report ----------------------------------------------------------


def health_report(now: datetime | None = None) -> dict:
    """A cheap, side-effect-free snapshot of system health.

    Returns a structured dict (documented shape below); reads only. Confidence is
    computed with the stale cap **off** so "low confidence" reflects intrinsic
    quality, consistent with the lifecycle's staleness decision (CLAUDE.md §8).

    Shape::

        {
          "pages_total": int,
          "by_status": {"active": int, "stale": int, ...},
          "by_kind": {"fact": int, "digest": int, "note": int, ...},
          "no_sources": [page_id, ...],          # pages lacking any source
          "low_confidence": int,                  # intrinsic conf < STALE_THRESHOLD
          "low_confidence_pages": [page_id, ...],
          "stale": int,
          "open_contradictions": int,
          "graph": {"entities": int, "edges": int, "demoted": int},
          "orphan_entities": int,                 # declared but in no edge
          "undeclared_entities": int,             # in an edge but no page tags it
          "dangling_structural": int,             # supersedes/contradicts to a gone page
          "index": {
            "markdown_pages": int, "indexed_pages": int, "fresh": bool,
            "missing_from_index": [page_id, ...], "extra_in_index": [page_id, ...],
          },
          "graph_index": {
            "present": bool, "fresh": bool,
            "missing_page_nodes": [page_id, ...], "extra_page_nodes": [page_id, ...],
          },
        }
    """
    pages = store.list_pages()
    md_ids = {p.id for p in pages}

    by_status: dict[str, int] = defaultdict(int)
    by_kind: dict[str, int] = defaultdict(int)
    no_sources: list[str] = []
    low_conf_pages: list[str] = []
    for p in pages:
        by_status[p.status] += 1
        by_kind[p.kind] += 1
        if not p.sources:
            no_sources.append(p.id)
        conf, _ = confidence.compute_confidence(
            p, access=state.get_access(p.id), now=now, apply_stale_cap=False
        )
        if conf < config.STALE_THRESHOLD:
            low_conf_pages.append(p.id)

    # Graph cache: stats + the lint flags (lint with fix=False writes nothing).
    g_stats = graph.graph_stats()
    lint = graph_lint.graph_lint(fix=False, now=now)

    # Search-index freshness: which markdown ids are (not) reflected in the index.
    indexed = search.indexed_ids()
    missing_from_index = sorted(md_ids - indexed)
    extra_in_index = sorted(indexed - md_ids)

    # Graph-index freshness: the page nodes the graph holds vs the markdown pages.
    graph_present = (tenancy.current().cache_dir / "graph.db").exists()
    page_nodes = {
        e["ref"][len("page:"):]
        for e in graph.get_graph_backend().all_entities()
        if e["type"] == PAGE_NODE_TYPE
    }
    missing_page_nodes = sorted(md_ids - page_nodes)
    extra_page_nodes = sorted(page_nodes - md_ids)

    return {
        "pages_total": len(pages),
        "by_status": dict(sorted(by_status.items())),
        "by_kind": dict(sorted(by_kind.items())),
        "no_sources": sorted(no_sources),
        "low_confidence": len(low_conf_pages),
        "low_confidence_pages": sorted(low_conf_pages),
        "stale": by_status.get("stale", 0),
        "open_contradictions": len(state.list_open_reviews()),
        "graph": {
            "entities": g_stats["entities"],
            "edges": g_stats["edges"],
            "demoted": g_stats["demoted"],
        },
        "orphan_entities": len(lint.orphan_entities),
        "undeclared_entities": len(lint.undeclared_entities),
        "dangling_structural": len(lint.dangling_structural),
        "index": {
            "markdown_pages": len(md_ids),
            "indexed_pages": len(indexed),
            "fresh": not missing_from_index and not extra_in_index,
            "missing_from_index": missing_from_index,
            "extra_in_index": extra_in_index,
        },
        "graph_index": {
            "present": graph_present,
            "fresh": graph_present and not missing_page_nodes and not extra_page_nodes,
            "missing_page_nodes": missing_page_nodes,
            "extra_page_nodes": extra_page_nodes,
        },
    }


# --- Duplicate finder (heuristic, read-only) --------------------------------


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _triples(page: store.Page) -> set[tuple]:
    return {
        (r["s"], r["p"], r["o"])
        for r in page.relations
        if {"s", "p", "o"} <= r.keys()
    }


def find_duplicates(limit: int = 20) -> list[dict]:
    """Heuristic near-duplicate candidate pairs — read-only, proposes nothing.

    Scores candidate page pairs by blending **title token overlap**, **tag
    overlap**, **shared graph edges**, and **FTS co-retrieval** (does one page
    surface when the other's title is searched). Pairs already linked by a
    supersede relationship are excluded (that duplication is intentional history).

    Returns up to ``limit`` pairs, highest similarity first, each as::

        {"page_a", "page_b", "title_a", "title_b", "similarity",
         "signals": {"title": f, "tags": f, "edges": f, "fts": bool},
         "rationale": str}

    This is a **heuristic stand-in pending Phase-5 vectors** (semantic similarity);
    it deliberately changes nothing.
    """
    pages = store.list_pages()
    by_id = {p.id: p for p in pages}
    title_tok = {p.id: _tokens(p.title) for p in pages}
    tag_set = {p.id: set(p.tags) for p in pages}
    triple_set = {p.id: _triples(p) for p in pages}

    # Pairs already in a supersede relationship — not duplicates to flag.
    linked: set[frozenset] = set()
    for p in pages:
        if p.superseded_by:
            linked.add(frozenset({p.id, p.superseded_by}))
        if p.supersedes:
            linked.add(frozenset({p.id, p.supersedes}))

    candidates: set[frozenset] = set()
    fts_pairs: set[frozenset] = set()

    # FTS co-retrieval: a page's title query surfacing another page.
    for p in pages:
        for hit in search.search(p.title, limit=6, include_stale=True):
            if hit.id != p.id and hit.id in by_id:
                pair = frozenset({p.id, hit.id})
                candidates.add(pair)
                fts_pairs.add(pair)

    # Shared-tag and shared-edge groups (bounded), to catch what FTS misses.
    groups: list[list[str]] = []
    tag_index: dict[str, list[str]] = defaultdict(list)
    for p in pages:
        for t in tag_set[p.id]:
            tag_index[t].append(p.id)
    triple_index: dict[tuple, list[str]] = defaultdict(list)
    for p in pages:
        for tr in triple_set[p.id]:
            triple_index[tr].append(p.id)
    groups.extend(tag_index.values())
    groups.extend(triple_index.values())
    for grp in groups:
        if 2 <= len(grp) <= _GROUP_CANDIDATE_CAP:
            for i in range(len(grp)):
                for j in range(i + 1, len(grp)):
                    candidates.add(frozenset({grp[i], grp[j]}))

    results: list[dict] = []
    for pair in candidates:
        if pair in linked:
            continue
        a, b = sorted(pair)
        title_sim = _jaccard(title_tok[a], title_tok[b])
        tag_sim = _jaccard(tag_set[a], tag_set[b])
        edge_sim = _jaccard(triple_set[a], triple_set[b])
        is_fts = pair in fts_pairs
        similarity = (
            _W_TITLE * title_sim
            + _W_TAGS * tag_sim
            + _W_EDGES * edge_sim
            + _W_FTS * (1.0 if is_fts else 0.0)
        )
        if similarity < _DUP_THRESHOLD:
            continue

        reasons = []
        if title_sim:
            reasons.append(f"title overlap {title_sim:.2f}")
        shared_tags = sorted(tag_set[a] & tag_set[b])
        if shared_tags:
            reasons.append(f"shared tags {tag_sim:.2f} ({', '.join(shared_tags)})")
        if edge_sim:
            reasons.append(f"shared edges {edge_sim:.2f}")
        if is_fts:
            reasons.append("co-retrieved by FTS")
        results.append({
            "page_a": a,
            "page_b": b,
            "title_a": by_id[a].title,
            "title_b": by_id[b].title,
            "similarity": round(similarity, 3),
            "signals": {
                "title": round(title_sim, 3),
                "tags": round(tag_sim, 3),
                "edges": round(edge_sim, 3),
                "fts": is_fts,
            },
            "rationale": "; ".join(reasons),
        })

    results.sort(key=lambda r: (-r["similarity"], r["page_a"], r["page_b"]))
    return results[:limit]
