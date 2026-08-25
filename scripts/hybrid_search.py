#!/usr/bin/env python3
"""Search the local xiaoguan hybrid FTS5 + dense-vector RAG index."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import warnings
from pathlib import Path
from typing import Any


ZH_MODEL = "BAAI/bge-small-zh-v1.5"
EN_MODEL = "BAAI/bge-small-en"
RRF_K = 60
TIER_LABELS = {1: "线索", 2: "一般", 3: "参考", 4: "较高", 5: "权威"}
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")


def default_corpus() -> Path:
    return Path.home() / "Documents" / "ChatGPT" / "销冠" / "资料库"


def find_index_root(explicit: str | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if os.environ.get("XIAOGUAN_HYBRID_INDEX"):
        candidates.append(Path(os.environ["XIAOGUAN_HYBRID_INDEX"]).expanduser())
    candidates.append(default_corpus() / "混合索引")
    for candidate in candidates:
        root = candidate.resolve()
        if (root / "index" / "hybrid.sqlite3").is_file():
            return root
    raise FileNotFoundError("找不到混合索引；请先运行 build_hybrid_index.py")


def ensure_runtime(index_root: Path) -> None:
    try:
        import fastembed  # noqa: F401

        return
    except ImportError:
        pass
    configured = os.environ.get("XIAOGUAN_RAG_PYTHON")
    runtime = Path(configured).expanduser() if configured else index_root / "runtime" / "bin" / "python3"
    runtime_prefix = runtime.parent.parent.resolve()
    if runtime.is_file() and Path(sys.prefix).resolve() != runtime_prefix:
        os.execv(str(runtime), [str(runtime), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise RuntimeError("混合RAG运行时不可用；本地FTS索引仍可由关键词版脚本检索")


def unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    return [item for item in items if item and not (item in seen or seen.add(item))]


def quote_fts(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def word_query(text: str) -> str:
    terms = unique(
        [token.lower() for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_+.-]{1,}", text)]
    )
    return " OR ".join(quote_fts(term) for term in terms[:24])


def trigram_query(text: str) -> str:
    concepts = re.findall(r"[\u3400-\u9fff]{3,}", text)
    cleaned = unique([re.sub(r"\s+", " ", item).strip().lower() for item in concepts])
    return " OR ".join(quote_fts(item) for item in cleaned[:16])


def fts_ranks(
    conn: sqlite3.Connection, table: str, match_query: str, limit: int
) -> dict[int, int]:
    if not match_query:
        return {}
    try:
        rows = conn.execute(
            f"SELECT rowid, bm25({table}, 3.0, 1.0, 0.2) AS rank_score "
            f"FROM {table} WHERE {table} MATCH ? ORDER BY rank_score LIMIT ?",
            (match_query, limit),
        ).fetchall()
        return {int(row["rowid"]): rank for rank, row in enumerate(rows, 1)}
    except sqlite3.OperationalError:
        return {}


def normalized_rrf(rank_maps: list[dict[int, int]]) -> dict[int, float]:
    raw: dict[int, float] = {}
    for ranks in rank_maps:
        for row_id, rank in ranks.items():
            raw[row_id] = raw.get(row_id, 0.0) + 1.0 / (RRF_K + rank)
    # Each row belongs to one language, so a top hit in either route must be able
    # to reach 1.0; do not divide by the number of mutually exclusive routes.
    maximum = 1.0 / (RRF_K + 1)
    return {row_id: min(1.0, score / maximum) for row_id, score in raw.items()}


def top_vector_ranks(
    query: str,
    language: str,
    model_name: str,
    model_path: Path,
    index_dir: Path,
    limit: int,
) -> dict[int, int]:
    import numpy as np
    from fastembed import TextEmbedding

    vectors_path = index_dir / f"vectors-{language}.npy"
    rows_path = index_dir / f"row-ids-{language}.npy"
    vectors = np.load(vectors_path, mmap_mode="r")
    row_ids = np.load(rows_path, mmap_mode="r")
    model = TextEmbedding(
        model_name,
        specific_model_path=str(model_path),
        local_files_only=True,
        threads=max(1, min(8, os.cpu_count() or 4)),
    )
    query_vector = np.asarray(list(model.query_embed([query]))[0], dtype=np.float32)
    norm = float(np.linalg.norm(query_vector))
    if norm:
        query_vector /= norm
    with np.errstate(all="ignore"):
        similarities = np.asarray(vectors @ query_vector)
    similarities = np.nan_to_num(similarities, nan=-1.0, posinf=-1.0, neginf=-1.0)
    take = min(limit, similarities.shape[0])
    if take == 0:
        return {}
    indices = np.argpartition(similarities, -take)[-take:]
    ordered = indices[np.argsort(similarities[indices])[::-1]]
    return {int(row_ids[index]): rank for rank, index in enumerate(ordered, 1)}


def fetch_candidates(conn: sqlite3.Connection, row_ids: set[int]) -> dict[int, dict[str, Any]]:
    if not row_ids:
        return {}
    ordered = sorted(row_ids)
    placeholders = ",".join("?" for _ in ordered)
    rows = conn.execute(f"SELECT * FROM chunks WHERE row_id IN ({placeholders})", ordered).fetchall()
    return {int(row["row_id"]): dict(row) for row in rows}


def tag_matches(tags: str, requested: str) -> bool:
    return requested == "any" or requested in {tag for tag in tags.split(",") if tag}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--query-zh", default="", help="Chinese semantic query generated from the same intent")
    parser.add_argument("--query-en", default="", help="English semantic query generated from the same intent")
    parser.add_argument(
        "--stage",
        default="any",
        choices=["any", "prospecting", "discovery", "solution", "procurement", "negotiation", "closing", "success"],
    )
    parser.add_argument("--channel", default="any", choices=["any", "online", "phone", "offline"])
    parser.add_argument(
        "--intent",
        default="sales",
        choices=["sales", "marketing", "psychology", "customer_success", "general"],
    )
    parser.add_argument("--include", default="", help="Optional source path/domain substring")
    parser.add_argument("--top-k", type=int, default=3, choices=range(1, 13))
    parser.add_argument("--candidate-k", type=int, default=50, choices=range(10, 101))
    parser.add_argument("--max-chars", type=int, default=5000, choices=range(1000, 20001))
    parser.add_argument("--index-dir")
    args = parser.parse_args()

    index_root = find_index_root(args.index_dir)
    ensure_runtime(index_root)
    index_dir = index_root / "index"
    conn = sqlite3.connect(index_dir / "hybrid.sqlite3")
    conn.row_factory = sqlite3.Row

    lexical_text = " ".join(part for part in (args.query, args.query_zh, args.query_en) if part)
    word_ranks = fts_ranks(conn, "chunks_word", word_query(lexical_text), args.candidate_k)
    trigram_ranks = fts_ranks(conn, "chunks_trigram", trigram_query(lexical_text), args.candidate_k)
    lexical_scores = normalized_rrf([word_ranks, trigram_ranks])

    warnings_out: list[str] = []
    semantic_maps: list[dict[int, int]] = []
    models = index_root / "models"
    try:
        semantic_maps.append(
            top_vector_ranks(
                args.query_zh or args.query,
                "zh",
                ZH_MODEL,
                models / "bge-small-zh-v1.5",
                index_dir,
                args.candidate_k,
            )
        )
    except Exception as exc:
        warnings_out.append(f"中文向量召回不可用：{exc}")
    try:
        semantic_maps.append(
            top_vector_ranks(
                args.query_en or args.query,
                "en",
                EN_MODEL,
                models / "bge-small-en",
                index_dir,
                args.candidate_k,
            )
        )
    except Exception as exc:
        warnings_out.append(f"英文向量召回不可用：{exc}")
    semantic_scores = normalized_rrf(semantic_maps)
    candidate_ids = set(lexical_scores) | set(semantic_scores)
    candidates = fetch_candidates(conn, candidate_ids)
    conn.close()

    ranked: list[dict[str, Any]] = []
    include_lower = args.include.lower().strip()
    for row_id, item in candidates.items():
        if include_lower and include_lower not in (
            f"{item['source_path']} {item['knowledge_domain']} {item['title']}".lower()
        ):
            continue
        lexical = lexical_scores.get(row_id, 0.0)
        semantic = semantic_scores.get(row_id, 0.0)
        dynamic_text = f"{item['title']} {item['text']}".lower()
        dynamic_stage_match = args.stage == "negotiation" and any(
            term in dynamic_text for term in ("objection", "pricing", "discount", "异议", "报价", "折扣")
        )
        stage = 1.0 if tag_matches(item["stages"], args.stage) or dynamic_stage_match else 0.0
        channel = 1.0 if tag_matches(item["channels"], args.channel) else 0.0
        tier = max(0.0, min(1.0, (int(item["source_tier"]) - 1) / 4))
        weights = {"lexical": 0.39, "semantic": 0.43, "tier": 0.03}
        if args.stage != "any":
            weights["stage"] = 0.10
        if args.channel != "any":
            weights["channel"] = 0.05
        denominator = sum(weights.values())
        final_score = (
            lexical * weights["lexical"]
            + semantic * weights["semantic"]
            + tier * weights["tier"]
            + stage * weights.get("stage", 0.0)
            + channel * weights.get("channel", 0.0)
        ) / denominator
        legal_domain = any(term in item["knowledge_domain"] for term in ("法规", "法律", "政治法律"))
        if legal_domain and args.intent != "general":
            final_score *= 0.72
        final_score = min(1.0, final_score)
        ranked.append(
            {
                **item,
                "score": final_score,
                "score_components": {
                    "lexical": round(lexical, 4),
                    "semantic": round(semantic, 4),
                    "source_tier": int(item["source_tier"]),
                    "stage_match": bool(stage) if args.stage != "any" else None,
                    "channel_match": bool(channel) if args.channel != "any" else None,
                },
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)

    results: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    used_chars = 0
    for item in ranked:
        source_id = str(item["source_id"])
        if source_counts.get(source_id, 0) >= 2:
            continue
        remaining = args.max_chars - used_chars
        if remaining <= 0 or len(results) >= args.top_k:
            break
        excerpt = str(item["text"])[:remaining]
        if not excerpt:
            continue
        source_counts[source_id] = source_counts.get(source_id, 0) + 1
        used_chars += len(excerpt)
        results.append(
            {
                "rank": len(results) + 1,
                "score": round(float(item["score"]), 4),
                "score_components": item["score_components"],
                "chunk_id": item["chunk_id"],
                "title": item["title"],
                "source_path": item["source_path"],
                "origin": item["origin"],
                "knowledge_domain": item["knowledge_domain"],
                "language": item["language"],
                "evidence_grade": TIER_LABELS.get(int(item["source_tier"]), "一般"),
                "stages": [tag for tag in item["stages"].split(",") if tag],
                "channels": [tag for tag in item["channels"].split(",") if tag],
                "text": excerpt,
            }
        )
    mode = "hybrid" if semantic_scores else "lexical_fallback"
    print(
        json.dumps(
            {
                "ok": True,
                "mode": mode,
                "query": args.query,
                "query_zh": args.query_zh or None,
                "query_en": args.query_en or None,
                "stage": args.stage,
                "channel": args.channel,
                "intent": args.intent,
                "returned_chars": used_chars,
                "count": len(results),
                "warnings": warnings_out,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise
