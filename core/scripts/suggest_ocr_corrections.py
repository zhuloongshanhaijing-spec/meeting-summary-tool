#!/usr/bin/env python3
"""Annotation-only OCR term-correction suggestions (spec §3.3 → §4.6).

Decision ⑤ executor: when a word in the ASR literal record is homophone or
near-shape to a term seen on an OCR'd slide AND the ASR routes/engines
already measurably disagreed at that record, emit the OCR term as a
*candidate* correction. This script NEVER rewrites any record: the only
files it writes are --output (ocr_corrections.jsonl) and --receipt; every
input file stays byte-identical and clean_literal is never touched
(iron rule: ASR 原文永不静默改写; rendering is a later stage's job).

Disagreement gate — the honest signal (iron rule #10: confidence comes
only from MEASURED cross-route/cross-engine agreement, never invented):
  A record qualifies only when upstream actually measured disagreement:
    * record["uncertainty"] contains "acoustic_route_disagreement", or
    * record["route_agreement_min"] < 0.93, or
    * its record_id is listed in --asr-disagreements (external artifact).
  Field evidence: run_meeting.py stage_literal_records sets
  certainty="low" + uncertainty="acoustic_route_disagreement; ..." exactly
  when the min pairwise similarity across the qwen acoustic routes < 0.93
  (run_meeting.py:608-609, 616-617). Whisper-routed records carry
  route_agreement_min=1.0 / uncertainty=null (run_meeting.py:573-574) — a
  per-track engine choice with no per-record measured disagreement — so
  they never qualify on their own; we do NOT invent disagreement for them.

Candidate matching (all three spec conditions must hold; 宁缺毋滥):
  1. an OCR term — a CJK run of 2-6 chars, or a latin token of ≥3 chars
     containing a letter — against an equal-length literal span with
     identical tone-less pinyin (pypinyin, CJK only) → basis "pinyin",
     else edit distance ≤1 and not identical (latin compared case/space-
     folded) → basis "edit_distance". Pinyin is per-character (symmetric
     on both sides), checked before the edit-distance path.
  2. the record passes the disagreement gate above.
  3. the OCR term is not in the small built-in stopword set.
  pypinyin is imported lazily: --disable-pinyin or ImportError turns the
  pinyin path off (receipt pypinyin_available=false); the edit-distance
  path then still runs under bare stdlib python3.

Deliberate spec interpretation (coordinator ruling 2026-09-30, to be
recorded in the T10 ADR): spec §3.3 says 编辑距离≤1 unqualified, but the
CJK path here deliberately compares EQUAL-length spans only (hamming ≤ 1)
— the conservative direction: false negatives only, no non-word-window
false positives, bounded runtime. The latin path does handle length-
differing edits (insert/delete) on folded tokens. Known direction of the
per-char symmetric pinyin key: it can over-match polyphone readings into
plausible-looking pairs (e.g. 银形 → 银行, both "yin xing" per char);
output is capped, deduped, and human-reviewed annotation — never a
rewrite.

Output rows (spec §4.6): {record_id, original, suggested, ocr_page_id,
ocr_evidence_id (--evidence-map page_id→evidence_id, else null), basis,
engines_disagreed: true}. Identical (record_id, original, suggested)
pairs are deduped — the natural-sort-first page wins (numeric-aware key,
so P2 beats P10 and the EARLIEST slide keeps the citation); at most 5
suggestions per record, in deterministic scan order.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Iterator

CJK_RUN = re.compile(r"[\u3400-\u9fff]+")          # same CJK range as align_audio_slides.compact
LATIN_TOKEN = re.compile(r"[A-Za-z0-9]{3,}")
CJK_TERM_MIN, CJK_TERM_MAX = 2, 6
ROUTE_DISAGREEMENT_THRESHOLD = 0.93                 # mirrors run_meeting.py:608
MAX_SUGGESTIONS_PER_RECORD = 5

# 小型内建停用词表（spec §3.3 条件 3：歧义高频词不做修正候选）。
# 单字本就被 CJK_TERM_MIN=2 挡掉，列出只为口径完整。
OCR_STOPWORDS = frozenset("""
的 了 是 在 和 我 你 他 她 它 就 都 而 及 与 着 或 一个 我们 你们 他们 她们 它们
这个 那个 这些 那些 什么 怎么 因为 所以 但是 如果 已经 现在 今天 大家 就是
还有 没有 不是 可以 一下 一些 以及 或者 然后 这样 那样 自己 知道 觉得 时候
""".split())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}: 非法 JSON 行: {exc}") from None
        if isinstance(row, dict):
            rows.append(row)
    return rows


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def load_pypinyin(disabled: bool) -> Any | None:
    """Lazy import: only vendor/tools-venv ships pypinyin; system python3
    (zero third-party packages by design) must keep working on the
    edit-distance path alone."""
    if disabled:
        return None
    try:
        import pypinyin  # noqa: PLC0415 — deliberately lazy
    except ImportError:
        return None
    return pypinyin


class PinyinMatcher:
    """Tone-less per-character pinyin keys (cached); symmetric on both
    sides of a comparison, so context-free readings cannot make the
    comparison lopsided."""

    def __init__(self, module: Any):
        self._module = module
        self._cache: dict[str, str] = {}

    def key(self, text: str) -> tuple[str, ...]:
        return tuple(self._char(ch) for ch in text)

    def _char(self, ch: str) -> str:
        got = self._cache.get(ch)
        if got is None:
            parts = self._module.lazy_pinyin(ch)
            got = parts[0] if parts else ""
            self._cache[ch] = got
        return got


def within_one_edit(a: str, b: str) -> bool:
    """Levenshtein distance ≤ 1 between a and b (identical → True)."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    short, long = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(short) and short[i] == long[i]:
        i += 1
    return short[i:] == long[i + 1:]


def fold_latin(token: str) -> str:
    """拉丁词忽略大小写/空格（spec §3.3 条件 1）。"""
    return re.sub(r"\s+", "", token).lower()


def extract_terms(text: str) -> list[tuple[str, str]]:
    """(term, kind) pairs from one OCR item text, first-occurrence order,
    deduped, stopwords dropped. kind ∈ {"cjk", "latin"}."""
    found: dict[str, str] = {}
    for run in CJK_RUN.findall(text or ""):
        if CJK_TERM_MIN <= len(run) <= CJK_TERM_MAX and run not in OCR_STOPWORDS:
            found.setdefault(run, "cjk")
    for token in LATIN_TOKEN.findall(text or ""):
        if any(ch.isalpha() for ch in token) and token not in OCR_STOPWORDS:
            found.setdefault(token, "latin")
    return list(found.items())


def cjk_windows_by_length(clean_literal: str) -> dict[int, list[str]]:
    """Every CJK span of length 2-6 inside the literal, position order."""
    out: dict[int, list[str]] = {}
    for run in CJK_RUN.findall(clean_literal or ""):
        for size in range(CJK_TERM_MIN, min(CJK_TERM_MAX, len(run)) + 1):
            bucket = out.setdefault(size, [])
            bucket.extend(run[i:i + size] for i in range(len(run) - size + 1))
    return out


def natural_page_key(stem: str) -> tuple:
    """Numeric-aware sort key for page stems: real slides are un-zero-padded
    P{n}.png, so lexicographic order would put P10 before P2 and let a
    chronologically LATER slide win the dedupe citation."""
    return tuple((0, int(part)) if part.isdigit() else (1, part)
                 for part in re.split(r"(\d+)", stem) if part)


def match_term(term: str, kind: str, windows: dict[int, list[str]],
               latin_tokens: list[str],
               pinyin: PinyinMatcher | None) -> Iterator[tuple[str, str]]:
    """(original, basis) pairs for one OCR term against one record."""
    if kind == "cjk":
        term_key = pinyin.key(term) if pinyin else None
        for window in windows.get(len(term), ()):
            if window == term:
                continue
            if term_key is not None and pinyin.key(window) == term_key:
                yield window, "pinyin"
            elif within_one_edit(window, term):
                yield window, "edit_distance"
    else:
        folded_term = fold_latin(term)
        for token in latin_tokens:
            folded_token = fold_latin(token)
            if folded_token == folded_term:
                continue  # 仅大小写差异不是修正
            if within_one_edit(folded_token, folded_term):
                yield token, "edit_distance"


def record_disagreed(record: dict[str, Any], external_ids: set[str]) -> bool:
    """Only MEASURED upstream disagreement qualifies (see module docstring;
    iron rule #10 — never invent a disagreement)."""
    if str(record.get("record_id")) in external_ids:
        return True
    uncertainty = record.get("uncertainty")
    if isinstance(uncertainty, str) and "acoustic_route_disagreement" in uncertainty:
        return True
    agreement = record.get("route_agreement_min")
    if (isinstance(agreement, (int, float)) and not isinstance(agreement, bool)
            and agreement < ROUTE_DISAGREEMENT_THRESHOLD):
        return True
    return False


def collect_pages(ocr_rows: list[dict[str, Any]]) -> list[tuple[str, list[tuple[str, str]]]]:
    """(page_id, terms) per OCR row with usable terms, natural-sorted by page
    stem (P2 < P10) for deterministic earliest-page-wins dedupe."""
    pages: list[tuple[str, list[tuple[str, str]]]] = []
    for row in ocr_rows:
        file_name = str(row.get("file") or "")
        if not file_name:
            continue
        terms: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in row.get("items") or []:
            if not isinstance(item, dict):
                continue
            for term, kind in extract_terms(str(item.get("text") or "")):
                if term not in seen:
                    seen.add(term)
                    terms.append((term, kind))
        if terms:
            pages.append((Path(file_name).stem, terms))
    pages.sort(key=lambda pair: natural_page_key(pair[0]))
    return pages


def build_suggestions(records: list[dict[str, Any]],
                      pages: list[tuple[str, list[tuple[str, str]]]],
                      external_ids: set[str],
                      evidence_map: dict[str, str],
                      pinyin: PinyinMatcher | None) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str, str]] = set()
    for record in records:
        record_id = record.get("record_id")
        if record_id is None or not record_disagreed(record, external_ids):
            continue
        literal = str(record.get("clean_literal") or "")
        if not literal:
            continue
        windows = cjk_windows_by_length(literal)
        latin_tokens = [t for t in LATIN_TOKEN.findall(literal) if any(ch.isalpha() for ch in t)]
        emitted = 0
        for page_id, terms in pages:
            for term, kind in terms:
                for original, basis in match_term(term, kind, windows, latin_tokens, pinyin):
                    key = (str(record_id), original, term)
                    if key in seen_pairs:
                        continue  # 同页/跨页重复候选：首次出现（排序最小页）胜出
                    seen_pairs.add(key)
                    suggestions.append({
                        "record_id": record_id,
                        "original": original,
                        "suggested": term,
                        "ocr_page_id": page_id,
                        "ocr_evidence_id": evidence_map.get(page_id),
                        "basis": basis,
                        "engines_disagreed": True,
                    })
                    emitted += 1
                    if emitted >= MAX_SUGGESTIONS_PER_RECORD:
                        break
                if emitted >= MAX_SUGGESTIONS_PER_RECORD:
                    break
            if emitted >= MAX_SUGGESTIONS_PER_RECORD:
                break
    return suggestions


def main() -> int:
    parser = argparse.ArgumentParser(
        description="OCR 标注式修正建议（只写建议文件，绝不改写任何 ASR 记录）")
    parser.add_argument("--records", required=True, type=Path,
                        help="literal_records.jsonl（clean_literal 原样保留，永不改写）")
    parser.add_argument("--slides-ocr", required=True, type=Path,
                        help="run_vision_ocr 输出 ocr.jsonl（{file, items:[{text,…}], error}）")
    parser.add_argument("--output", required=True, type=Path, help="ocr_corrections.jsonl")
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--asr-disagreements", type=Path,
                        help="可选：外部引擎分歧工件 jsonl（行含 record_id）")
    parser.add_argument("--evidence-map", type=Path,
                        help="可选：page_id→evidence_id 映射 jsonl（{page_id, evidence_id}）")
    parser.add_argument("--disable-pinyin", action="store_true",
                        help="关闭同音路径（仅近形/编辑距离）；pypinyin 缺失时自动等效")
    args = parser.parse_args()

    for required in (args.records, args.slides_ocr):
        if not required.is_file():
            raise SystemExit(f"input file not found: {required}")
    records = read_jsonl(args.records)
    ocr_rows = read_jsonl(args.slides_ocr)

    external_ids: set[str] = set()
    if args.asr_disagreements is not None:
        if not args.asr_disagreements.is_file():
            raise SystemExit(f"input file not found: {args.asr_disagreements}")
        external_ids = {str(row["record_id"])
                        for row in read_jsonl(args.asr_disagreements)
                        if row.get("record_id") is not None}
    evidence_map: dict[str, str] = {}
    if args.evidence_map is not None:
        if not args.evidence_map.is_file():
            raise SystemExit(f"input file not found: {args.evidence_map}")
        evidence_map = {str(row["page_id"]): str(row["evidence_id"])
                        for row in read_jsonl(args.evidence_map)
                        if row.get("page_id") is not None and row.get("evidence_id") is not None}

    module = load_pypinyin(args.disable_pinyin)
    pinyin = PinyinMatcher(module) if module is not None else None
    suggestions = build_suggestions(records, collect_pages(ocr_rows),
                                    external_ids, evidence_map, pinyin)
    atomic_write_text(
        args.output,
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in suggestions))
    atomic_write_text(args.receipt, json.dumps({
        "schema_version": 1,
        "status": "complete",
        "records_scanned": len(records),
        "corrections_count": len(suggestions),
        "pypinyin_available": pinyin is not None,
    }, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
