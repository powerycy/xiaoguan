#!/usr/bin/env python3
"""Build the local xiaoguan FTS5 + dense-vector hybrid retrieval index."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ZH_MODEL = "BAAI/bge-small-zh-v1.5"
EN_MODEL = "BAAI/bge-small-en"
INDEX_VERSION = "hybrid-v2"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_corpus() -> Path:
    return Path.home() / "Documents" / "ChatGPT" / "销冠" / "资料库"


def normalized_hash(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def content_body(text: str) -> str:
    if text.startswith("来源标题：") or text.startswith("来源图片："):
        parts = text.split("\n\n", 1)
        if len(parts) == 2:
            return parts[1].strip()
    return text.strip()


def language_of(text: str) -> str:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return "en"
    han = sum("\u3400" <= ch <= "\u9fff" for ch in compact)
    return "zh" if han >= 20 and han / len(compact) >= 0.12 else "en"


def chunk_text(text: str, language: str) -> list[str]:
    target, overlap = (900, 120) if language == "zh" else (1600, 200)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > target:
            if current:
                chunks.append(current.strip())
                current = ""
            start = 0
            while start < len(paragraph):
                end = min(len(paragraph), start + target)
                chunks.append(paragraph[start:end].strip())
                if end == len(paragraph):
                    break
                start = max(start + 1, end - overlap)
            continue
        candidate = paragraph if not current else current + "\n\n" + paragraph
        if len(candidate) <= target:
            current = candidate
        else:
            chunks.append(current.strip())
            available = max(0, target - len(paragraph) - 2)
            tail = current[-min(overlap, available) :].strip() if available else ""
            current = (tail + "\n\n" + paragraph).strip() if tail else paragraph
    if current:
        chunks.append(current.strip())
    return [chunk for chunk in chunks if chunk]


STAGE_TERMS = {
    "prospecting": ["outbound", "prospecting", "cold email", "cold call", "开发信", "陌生拜访", "获客"],
    "discovery": ["discovery", "spin", "qualification", "需求发现", "需求访谈", "痛点"],
    "solution": ["demo", "proposal", "business case", "方案", "演示", "试点", "poc"],
    "procurement": ["procurement", "legal", "security review", "采购", "法务", "招投标"],
    "negotiation": ["negotiation", "pricing", "discount", "objection", "谈判", "报价", "折扣", "异议"],
    "closing": ["closing", "contract", "成交", "签约", "合同"],
    "success": ["onboarding", "customer success", "renewal", "expansion", "客户成功", "续约", "增购"],
}
CHANNEL_TERMS = {
    "online": ["email", "wechat", "linkedin", "message", "邮件", "微信", "私信", "线上"],
    "phone": ["phone", "call", "电话", "视频会议"],
    "offline": ["visit", "in-person", "meeting", "拜访", "线下", "现场", "会议"],
}


def infer_tags(text: str, mapping: dict[str, list[str]]) -> str:
    lowered = text.lower()
    tags = [tag for tag, terms in mapping.items() if any(term in lowered for term in terms)]
    return ",".join(tags)


def source_tier(record: dict[str, Any], source_path: Path) -> int:
    source_id = str(record.get("source_id", source_path.stem)).lower()
    origin = str(record.get("origin", "")).lower()
    if source_id.startswith("cnlaw_") or any(
        domain in origin for domain in ("gov.cn", "npc.gov.cn", "samr.gov.cn", "court.gov.cn")
    ):
        return 5
    if source_id.startswith("open_") or record.get("access_level") in {"open_license", "public_legal_text"}:
        return 4
    if source_id.startswith(("owned_", "local_")):
        return 4
    if source_id.startswith(("gh_", "pd_", "web_")):
        return 3
    if record.get("kind") == "book_metadata":
        return 1
    return 2


def source_domain(record: dict[str, Any], source_path: Path) -> str:
    existing = str(record.get("knowledge_domain", "")).strip()
    if existing:
        return existing
    stem = source_path.stem.lower()
    if stem.startswith("cnlaw_"):
        return "中国经营法规"
    if stem.startswith("gh_"):
        return "开源销售与GTM"
    if stem.startswith("open_"):
        return "开放教材"
    if stem.startswith("pd_"):
        return "公共领域经典"
    if stem.startswith("local_"):
        return "沟通与关系材料"
    if stem.startswith("owned_"):
        return "用户自有材料"
    if stem.startswith("web_"):
        return "公开网页与报告"
    return "其他"


def first_title(text: str, fallback: str) -> str:
    for line in text.splitlines()[:20]:
        cleaned = re.sub(r"^#+\s*", "", line).strip()
        if 4 <= len(cleaned) <= 160:
            return cleaned
    return fallback


def load_records(corpus: Path) -> tuple[dict[str, dict[str, Any]], list[Path]]:
    detail_path = corpus / "索引" / "资料明细.json"
    records = json.loads(detail_path.read_text(encoding="utf-8")) if detail_path.exists() else []
    by_text_path = {
        str(Path(item["text_path"]).expanduser().resolve()): item
        for item in records
        if item.get("text_path")
    }
    source_paths: set[Path] = set()
    jsonl = corpus / "分片" / "全部分片.jsonl"
    with jsonl.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            path = Path(str(row.get("source_path", ""))).expanduser()
            if path.is_file():
                source_paths.add(path.resolve())
    return by_text_path, sorted(source_paths)


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = NORMAL;
        CREATE TABLE chunks (
            row_id INTEGER PRIMARY KEY,
            chunk_id TEXT NOT NULL UNIQUE,
            source_id TEXT NOT NULL,
            title TEXT NOT NULL,
            source_path TEXT NOT NULL,
            origin TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            license TEXT NOT NULL,
            language TEXT NOT NULL,
            source_tier INTEGER NOT NULL,
            knowledge_domain TEXT NOT NULL,
            stages TEXT NOT NULL,
            channels TEXT NOT NULL,
            content_hash TEXT NOT NULL UNIQUE,
            text TEXT NOT NULL,
            unicode_chars INTEGER NOT NULL
        );
        CREATE INDEX idx_chunks_language ON chunks(language);
        CREATE INDEX idx_chunks_source ON chunks(source_id);
        CREATE INDEX idx_chunks_domain ON chunks(knowledge_domain);
        CREATE VIRTUAL TABLE chunks_word USING fts5(
            title, text, source_path,
            content='chunks', content_rowid='row_id',
            tokenize='porter unicode61 remove_diacritics 2'
        );
        CREATE VIRTUAL TABLE chunks_trigram USING fts5(
            title, text, source_path,
            content='',
            tokenize='trigram'
        );
        """
    )


def normalize_vectors(vectors: "Any") -> "Any":
    import numpy as np

    array = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return array / norms


def embed_language(
    conn: sqlite3.Connection,
    language: str,
    model_name: str,
    model_path: Path,
    output_dir: Path,
    batch_size: int,
) -> dict[str, Any]:
    import numpy as np
    from fastembed import TextEmbedding

    rows = conn.execute(
        "SELECT row_id, title, text FROM chunks WHERE language = ? ORDER BY row_id", (language,)
    ).fetchall()
    texts = [f"{row['title']}\n{row['text']}" for row in rows]
    model = TextEmbedding(
        model_name,
        specific_model_path=str(model_path),
        local_files_only=True,
        threads=max(1, min(8, os.cpu_count() or 4)),
    )
    vectors = normalize_vectors(list(model.embed(texts, batch_size=batch_size)))
    np.save(output_dir / f"vectors-{language}.npy", vectors)
    np.save(output_dir / f"row-ids-{language}.npy", np.asarray([row["row_id"] for row in rows], dtype=np.int64))
    return {"language": language, "rows": len(rows), "dimensions": int(vectors.shape[1])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=str(default_corpus()))
    parser.add_argument("--index-dir")
    parser.add_argument("--batch-size", type=int, default=64)
    args = parser.parse_args()
    corpus = Path(args.corpus).expanduser().resolve()
    index_root = Path(args.index_dir).expanduser().resolve() if args.index_dir else corpus / "混合索引"
    output_dir = index_root / "index"
    output_dir.mkdir(parents=True, exist_ok=True)
    models = index_root / "models"
    model_paths = {
        "zh": models / "bge-small-zh-v1.5",
        "en": models / "bge-small-en",
    }
    for path in model_paths.values():
        if not (path / "model_optimized.onnx").exists():
            raise FileNotFoundError(f"缺少已解压的本地模型：{path}")

    records, source_paths = load_records(corpus)
    db_tmp = output_dir / "hybrid.sqlite3.tmp"
    db_final = output_dir / "hybrid.sqlite3"
    for stale in (db_tmp, Path(str(db_tmp) + "-wal"), Path(str(db_tmp) + "-shm")):
        if stale.exists():
            stale.unlink()
    conn = sqlite3.connect(db_tmp)
    conn.row_factory = sqlite3.Row
    create_schema(conn)
    seen_files: set[str] = set()
    seen_chunks: set[str] = set()
    language_counts: Counter[str] = Counter()
    duplicate_files = 0
    duplicate_chunks = 0
    row_id = 0
    for source_path in source_paths:
        raw = content_body(source_path.read_text(encoding="utf-8", errors="replace"))
        if not raw:
            continue
        file_hash = normalized_hash(raw)
        if file_hash in seen_files:
            duplicate_files += 1
            continue
        seen_files.add(file_hash)
        record = records.get(str(source_path), {})
        file_language = language_of(raw)
        title = str(record.get("title", "")).strip() or first_title(raw, source_path.stem)
        source_id = str(record.get("source_id", source_path.stem))
        for chunk_index, text in enumerate(chunk_text(raw, file_language), 1):
            digest = normalized_hash(text)
            if digest in seen_chunks:
                duplicate_chunks += 1
                continue
            seen_chunks.add(digest)
            row_id += 1
            language = language_of(text)
            language_counts[language] += 1
            tag_text = f"{title}\n{text}"
            conn.execute(
                """
                INSERT INTO chunks(
                    row_id, chunk_id, source_id, title, source_path, origin,
                    source_kind, license, language, source_tier, knowledge_domain,
                    stages, channels, content_hash, text, unicode_chars
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    f"hybrid_{source_path.stem}_{chunk_index:04d}",
                    source_id,
                    title,
                    str(source_path),
                    str(record.get("origin", "")),
                    str(record.get("kind", "")),
                    str(record.get("license", "")),
                    language,
                    source_tier(record, source_path),
                    source_domain(record, source_path),
                    infer_tags(tag_text, STAGE_TERMS),
                    infer_tags(tag_text, CHANNEL_TERMS),
                    digest,
                    text,
                    len(text),
                ),
            )
    conn.commit()
    conn.execute("INSERT INTO chunks_word(chunks_word) VALUES('rebuild')")
    conn.execute(
        "INSERT INTO chunks_trigram(rowid, title, text, source_path) "
        "SELECT row_id, title, text, source_path FROM chunks WHERE language = 'zh'"
    )
    conn.commit()

    vector_stats = [
        embed_language(conn, "zh", ZH_MODEL, model_paths["zh"], output_dir, args.batch_size),
        embed_language(conn, "en", EN_MODEL, model_paths["en"], output_dir, args.batch_size),
    ]
    conn.close()
    if db_final.exists():
        db_final.unlink()
    db_tmp.replace(db_final)
    for sidecar in (Path(str(db_tmp) + "-wal"), Path(str(db_tmp) + "-shm")):
        if sidecar.exists():
            sidecar.unlink()
    manifest = {
        "index_version": INDEX_VERSION,
        "built_at": now_iso(),
        "corpus": str(corpus),
        "source_files": len(source_paths),
        "indexed_files": len(seen_files),
        "duplicate_files_removed": duplicate_files,
        "chunks": row_id,
        "duplicate_chunks_removed": duplicate_chunks,
        "languages": dict(language_counts),
        "models": {"zh": ZH_MODEL, "en": EN_MODEL},
        "vectors": vector_stats,
        "lexical": ["FTS5 porter/unicode61", "FTS5 trigram"],
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"ok": True, **manifest}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise
