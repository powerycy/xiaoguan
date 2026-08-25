#!/usr/bin/env python3
"""Search the local xiaoguan JSONL corpus with lightweight lexical ranking."""

from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any


DEFAULT_CANDIDATES = [
    Path.home() / "Documents" / "ChatGPT" / "销冠" / "资料库",
    Path(__file__).resolve().parent.parent / "references" / "corpus",
]


def find_corpus(explicit: str | None) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if os.environ.get("XIAOGUAN_CORPUS_DIR"):
        candidates.append(Path(os.environ["XIAOGUAN_CORPUS_DIR"]).expanduser())
    candidates.extend(DEFAULT_CANDIDATES)
    for candidate in candidates:
        path = candidate.resolve()
        if path.is_dir():
            path = path / "分片" / "全部分片.jsonl"
        if path.is_file():
            return path
    raise FileNotFoundError(
        "找不到销冠语料库；请设置 XIAOGUAN_CORPUS_DIR 或通过 --corpus 指定资料库目录"
    )


def terms(query: str) -> list[str]:
    raw = [x.lower() for x in re.findall(r"[\w\u3400-\u9fff-]{2,}", query)]
    seen = set()
    return [x for x in raw if not (x in seen or seen.add(x))]


def score_row(row: dict[str, Any], query: str, tokens: list[str], include: str) -> float:
    text = str(row.get("text", ""))
    path = str(row.get("source_path", ""))
    haystack = text.lower()
    path_lower = path.lower()
    if include and include.lower() not in path_lower:
        return 0.0
    score = 0.0
    phrase = query.strip().lower()
    if phrase and phrase in haystack:
        score += 12.0
    matched = 0
    for token in tokens:
        count = haystack.count(token)
        if count:
            matched += 1
            score += 2.0 + min(6.0, math.log2(count + 1) * 1.6)
        if token in path_lower:
            score += 4.0
    if tokens and matched == len(tokens):
        score += 5.0
    elif tokens:
        score *= matched / len(tokens)
    return score


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True, help="Space-separated sales concepts")
    parser.add_argument("--top-k", type=int, default=3, choices=range(1, 21))
    parser.add_argument("--max-chars", type=int, default=5000, choices=range(1000, 30001))
    parser.add_argument("--include", default="", help="Optional source-path substring")
    parser.add_argument("--corpus", help="Corpus directory or JSONL path")
    args = parser.parse_args()
    try:
        corpus = find_corpus(args.corpus)
        query_terms = terms(args.query)
        if not query_terms:
            raise ValueError("检索词至少包含一个两字符以上的词")
        heap: list[tuple[float, int, dict[str, Any]]] = []
        with corpus.open(encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                row = json.loads(line)
                score = score_row(row, args.query, query_terms, args.include)
                if score <= 0:
                    continue
                item = (score, index, row)
                if len(heap) < args.top_k:
                    heapq.heappush(heap, item)
                elif score > heap[0][0]:
                    heapq.heapreplace(heap, item)
        results = []
        used = 0
        for score, _, row in sorted(heap, reverse=True):
            text = str(row.get("text", ""))
            remaining = args.max_chars - used
            if remaining <= 0:
                break
            excerpt = text[:remaining]
            used += len(excerpt)
            results.append(
                {
                    "score": round(score, 3),
                    "chunk_id": row.get("chunk_id"),
                    "source_path": row.get("source_path"),
                    "text": excerpt,
                }
            )
        print(
            json.dumps(
                {
                    "ok": True,
                    "query": args.query,
                    "corpus": str(corpus),
                    "count": len(results),
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
