#!/usr/bin/env python3
"""Fragment assembly plan generator (v2, semantic).

Reads independently transcribed fragment events (runs/fragment-*/literal_records.jsonl),
judges candidate fragment pairs (lexical prefilter + optional local Ollama LLM
four-way classification), groups fragments per meeting, orders them, and writes a
candidate assembly plan JSON for human review.

铁律（docs/HANDOVER.md §3）：
- literal_records.jsonl 是不可变事实层：本脚本只读，绝不改写。
- 写盘仅限 --output 一个路径（原子写：临时文件 + os.replace）。
- stdlib only：Ollama 经 urllib 调用，所有请求 timeout 严格受控。

四分类：same_continuous（同会议且相邻）/ same_gap（同会议疑似缺段）/
same_reorder（同会议非相邻）/ different（不同会议）。
LLM 不可用或单对判定失败 → 保守的词重叠兜底，失败对记入 degraded_pairs。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

DEFAULT_RUNS_DIR = "runs"
DEFAULT_OUTPUT = "runs/.assembly-plan.json"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen3:8b"
DEFAULT_PAIR_TIMEOUT = 20.0

MAX_NEIGHBORS = 4          # per-fragment candidate neighbours (controls LLM cost)
LLM_BATCH = 4              # pairs per LLM request — small local models drift JSON on long batches
DEFAULT_PAIR_TIMEOUT = 90.0  # per-request; small models generate slowly, big ones cold-start
SUMMARY_MAX = 40           # gap side summaries, chars
HEAD_TAIL_SENTENCES = 2
TEXT_CAP = 180

# Conservative fallback thresholds (v1-informed: 0.20 tail->head Jaccard + margin).
CONTINUOUS_THRESHOLD = 0.20
DIRECTION_MARGIN = 0.05
SAME_MEETING_THRESHOLD = 0.30
MIN_SHARED_KEYWORDS = 2
WEAK_DIRECTION = 0.10
NOTE_HINT_THRESHOLD = 0.10

RELATIONS = ("same_continuous", "same_gap", "same_reorder", "different")
GROUPING_RELATIONS = ("same_continuous", "same_gap", "same_reorder")
NOTICE = "装配为候选视图，逐句原文未改写，缺段如实保留为缺口。"
PROBE_PROMPT = "连接测试：请只回复两个字符：ok"

PROMPT_HEAD = """你是本地会议碎片装配判定器。只依据给出的片段原文与词面证据判断，不得编造任何内容。对每一对片段输出一个 JSON 对象，最终只输出一个 JSON 数组，禁止输出数组以外的任何文字。
关系定义：
- same_continuous：同一会议，且左片段结尾与右片段开头内容直接衔接（相邻）。
- same_gap：同一会议，但两者之间疑似缺少一段内容（不直接衔接）。
- same_reorder：同一会议，但两者不是相邻片段。
- different：不属于同一个会议。
判定校准（重要）：
- 同一会议的碎片通常共享具体主题锚点：同一项目名、同一部门、同一组数字/日期、同一议程条目。
- different 的锚点是具体主题内容不同（如：预算数字审批 vs 志愿者活动排班），而不是语域或文体不同。两个片段"都是正式会议口吻"不构成 same 的理由；判定时先默想各自的核心议题（钱？人？项目？活动？），核心议题不同即判 different。
- 碎片可能很短，头部/尾部只是截取，不要因为文本短或信息少而默认判 different；但同样不要因为都像会议就默认判 same_*——以议题内容为准。
- 同一场会议的【开场段】（Good morning/Welcome/介绍议程）与【收尾段】（To conclude/Final reminders/致谢）内容性质与中段不同，仍属同一会议：判定锚点是议程主题是否同一，不是段落功能。
- 词面证据（共享关键词等）由本地词面统计给出，仅供参考：它与语义判断冲突时以语义为准，不得据此编造原文没有的内容。
示例（仅演示输出格式与判定粒度，内容与本次任务无关）：
片段原文：
- 示例甲 头部：季度销售总结会开始。华东区汇报了十月数据。
- 示例甲 尾部：华东区十月完成率百分之九十二。
- 示例乙 头部：继续看销售会议的下一项。十一月目标需要上调。
- 示例乙 尾部：会议决定十一月目标上调到一百万。
- 示例丙 头部：社区图书馆志愿者排班讨论。
- 示例丙 尾部：下周三之前请确认班次。
待判定片段对：
- ["示例甲", "示例乙"] // 词面证据: 共享关键词数 3（如 销售、会议、目标），尾首衔接分 fwd=0.31/0.05
- ["示例甲", "示例丙"] // 词面证据: 共享关键词数 0，尾首衔接分 fwd=0.00/0.00
- ["示例乙", "示例丙"] // 词面证据: 共享关键词数 1（如 会议），尾首衔接分 fwd=0.05/0.00
输出：
[{"pair": ["示例甲", "示例乙"], "relation": "same_gap", "left": "示例甲", "right": "示例乙", "left_summary": "华东区十月完成率百分之九十二", "right_summary": "十一月目标需要上调"}, {"pair": ["示例甲", "示例丙"], "relation": "different", "left": "", "right": "", "left_summary": "", "right_summary": ""}, {"pair": ["示例乙", "示例丙"], "relation": "different", "left": "", "right": "", "left_summary": "", "right_summary": ""}]
（示例乙与示例丙虽然都是会议口吻、还共享"会议"一词，但议题分别是销售目标与志愿者排班——核心议题不同，判 different。）
每个对象必须包含字段：
pair: [片段id, 片段id]，保持给定顺序
relation: 上述四种关系之一
left: 当 relation 为 same_continuous 或 same_gap 时，按内容先后填在前片段的 id，否则填空字符串
right: 同上，填在后片段的 id
left_summary: 仅当 relation 为 same_gap 时必填：left 片段的原文摘录，不超过40字，必须逐字截取该片段原文（不得翻译、不得改写、不得概括）；其他情况填空字符串
right_summary: 同上，针对 right 片段"""

# CJK function characters stripped before bigramming; Latin stopword list below.
STOP_CJK_CHARS = "的了是在我你他她它们这那和与或并对于把被不没也很就都还又说再呢吗吧啊呀哦嗯嘛之其等把"
STOP_LATIN_WORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "is", "are", "was",
    "were", "be", "been", "being", "this", "that", "these", "those", "it",
    "its", "as", "at", "by", "for", "with", "from", "we", "you", "they", "he",
    "she", "i", "not", "no", "do", "does", "did", "have", "has", "had", "will",
    "would", "can", "could", "should", "may", "might", "must", "about", "into",
    "over", "then", "than", "so", "if", "but", "our", "your", "their", "there",
    "here", "what", "when", "how", "why", "who", "am", "me", "him", "her",
    "us", "them",
}


# ---------------------------------------------------------------- lexicon

def _cjk_bigram_keywords(text: str) -> set:
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    filtered = "".join(ch for ch in cjk if ch not in STOP_CJK_CHARS)
    return {filtered[i:i + 2] for i in range(max(0, len(filtered) - 1))}


def text_words(text: str) -> set:
    """Raw lexical features (v1 semantics): CJK bigrams + Latin tokens."""
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    out = {cjk[i:i + 2] for i in range(max(0, len(cjk) - 1))}
    out.update(re.findall(r"[A-Za-z0-9']+", text.lower()))
    return out


def keywords(text: str) -> set:
    """Stopword-filtered low-frequency-ish keyword features."""
    out = _cjk_bigram_keywords(text)
    for token in re.findall(r"[A-Za-z0-9']+", text.lower()):
        if len(token) >= 2 and token not in STOP_LATIN_WORDS:
            out.add(token)
    return out


def jaccard(a: set, b: set) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def prune_ubiquitous(fragments: list) -> None:
    """Drop keywords present in (almost) every fragment: they carry no signal."""
    total = len(fragments)
    if total < 4:
        return
    df: dict = defaultdict(int)
    for frag in fragments:
        for term in frag["keyword_set"]:
            df[term] += 1
    cap = max(1, int(total * 0.75))
    for frag in fragments:
        frag["keyword_set"] = {t for t in frag["keyword_set"] if df[t] <= cap}


# ---------------------------------------------------------------- collect

def read_fragment_run(run_dir: Path):
    """Return (fragment_dict, None) or (None, skip_reason). Read-only."""
    records = run_dir / "literal_records.jsonl"
    if not records.is_file():
        return None, "missing_literal_records"
    try:
        rows = [json.loads(line) for line in
                records.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return None, "broken_jsonl"
    texts = []
    end = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get("clean_literal") or "").strip() or str(row.get("raw_text") or "").strip()
        if text:
            texts.append(text)
        try:
            end = max(end, float(row.get("end_seconds") or 0))
        except (TypeError, ValueError):
            pass
    if not texts:
        return None, "empty_transcript"
    source = ""
    source_dir = run_dir / "source"
    if source_dir.is_dir():
        for path in sorted(source_dir.iterdir()):
            if path.is_file():
                source = path.name
                break
    return {
        "id": run_dir.name,
        "source": source,
        "sentence_count": len(texts),
        "duration_seconds": round(end, 3),
        "head": " ".join(texts[:HEAD_TAIL_SENTENCES])[:TEXT_CAP],
        "tail": " ".join(texts[-HEAD_TAIL_SENTENCES:])[-TEXT_CAP:],
        # private, sentence-level windows for directional scoring (never exported)
        "head_sents": texts[:HEAD_TAIL_SENTENCES],
        "tail_sents": texts[-HEAD_TAIL_SENTENCES:],
        "keyword_set": keywords(" ".join(texts)),
    }, None


def collect_fragments(runs_dir: Path):
    fragments, skipped = [], []
    if not runs_dir.is_dir():
        return fragments, skipped
    for path in sorted(runs_dir.iterdir(), key=lambda p: p.name):
        if not (path.is_dir() and path.name.startswith("fragment-")):
            continue
        fragment, reason = read_fragment_run(path)
        if fragment is None:
            skipped.append({"id": path.name, "reason": reason})
        else:
            fragments.append(fragment)
    prune_ubiquitous(fragments)
    for frag in fragments:
        frag["keywords"] = sorted(frag["keyword_set"])[:40]
    return fragments, skipped


# ---------------------------------------------------------------- note hints

def load_references(ref_dir: Path):
    """Note files are ordering hints only; order preserved file->line."""
    references, segments = [], []
    if not ref_dir.is_dir():
        return references, segments
    for path in sorted(p for p in ref_dir.iterdir() if p.is_file()):
        references.append(path.name)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if line:
                segments.append((path.name, line))
    return references, segments


def attach_note_hints(fragments: list, segments: list) -> None:
    """Substring hit beats keyword overlap; hints never create group edges."""
    seg_words = [text_words(segment) for _, segment in segments]
    for frag in fragments:
        best_score, best_index, best_source = 0.0, None, None
        for text in (frag["head"], frag["tail"]):
            probe = text.strip()
            if not probe:
                continue
            for idx, (_name, segment) in enumerate(segments):
                score = 0.0
                if len(probe) >= 6 and probe[:24] in segment:
                    score = 1.0
                else:
                    score = jaccard(text_words(probe), seg_words[idx])
                if score > best_score:
                    best_score, best_index, best_source = score, idx, _name
        frag["note_hint_score"] = round(best_score, 3)
        frag["note_hint_index"] = best_index if best_score >= NOTE_HINT_THRESHOLD else None
        frag["note_hint_source"] = best_source if best_score >= NOTE_HINT_THRESHOLD else None


def order_key(frag: dict):
    """Stable ordering key: note position first, then fragment id."""
    hint = frag.get("note_hint_index")
    return (hint if hint is not None else float("inf"), frag["id"])


# ---------------------------------------------------------------- pair scores

def _best_cross_jaccard(left_sents: list, right_sents: list) -> float:
    """Best tail-sentence x head-sentence Jaccard: robust to window filler."""
    return max((jaccard(text_words(a), text_words(b))
                for a in left_sents for b in right_sents), default=0.0)


def pair_scores(fragments: list) -> dict:
    """score[(a, b)] with a < b: 0.6*best directional head/tail Jaccard + 0.4*overall."""
    windows = {f["id"]: (f["tail_sents"], f["head_sents"]) for f in fragments}
    scores = {}
    for i, left in enumerate(fragments):
        for right in fragments[i + 1:]:
            tail_a, head_a = windows[left["id"]]
            tail_b, head_b = windows[right["id"]]
            fwd_ab = _best_cross_jaccard(tail_a, head_b)
            fwd_ba = _best_cross_jaccard(tail_b, head_a)
            overall = jaccard(left["keyword_set"], right["keyword_set"])
            shared_sample = sorted(left["keyword_set"] & right["keyword_set"])[:5]
            scores[(left["id"], right["id"])] = {
                "fwd_ab": fwd_ab, "fwd_ba": fwd_ba,
                "overall": overall, "shared": len(left["keyword_set"] & right["keyword_set"]),
                "shared_sample": shared_sample,
                "score": 0.6 * max(fwd_ab, fwd_ba) + 0.4 * overall,
            }
    return scores


def content_fingerprint(frag: dict) -> str:
    """Order-stable content fingerprint: derived from WHAT is said (keyword set
    + head text), never from IDs/upload order. Used to canonicalise judging
    order so upload sequence cannot influence LLM batch composition
    (stability plan 2026-10-05 建议5a, root cause of run1/run5-6 instability)."""
    kw = ",".join(sorted(frag.get("keyword_set") or []))
    head = (frag.get("head") or "")[:120]
    tail = (frag.get("tail") or "")[:120]
    return hashlib.sha1((kw + "|" + head + "|" + tail).encode("utf-8")).hexdigest()


def candidate_pairs(fragments: list, scores: dict) -> list:
    """All-pairs prefilter, but each fragment keeps at most MAX_NEIGHBORS partners.

    Both the SELECTION (MAX_NEIGHBORS cap) and the final ORDER are canonicalised
    by content fingerprint — the tie-break that decides which partner is kept
    when scores are equal (common for zero-score cross-meeting pairs in real
    transcripts) must also be content-derived, not ID-derived. The same corpus
    uploaded under different names therefore produces the same pair set AND the
    same judging sequence — upload order can no longer shift outcomes."""
    fp = {f["id"]: content_fingerprint(f) for f in fragments}
    neighbours: dict = defaultdict(list)
    for (a, b), payload in scores.items():
        neighbours[a].append((payload["score"], b))
        neighbours[b].append((payload["score"], a))
    keep = set()
    for fid, entries in neighbours.items():
        entries.sort(key=lambda item: (-item[0], fp.get(item[1], ""), item[1]))
        for score, other in entries[:MAX_NEIGHBORS]:
            keep.add((fid, other) if fid < other else (other, fid))
    # Canonical ORDER: sort by the SORTED fingerprint tuple so a content-pair
    # lands at the same position regardless of which of its two fragments held
    # the smaller ID under a given naming scheme.
    return sorted(keep, key=lambda p: (tuple(sorted((fp.get(p[0], ""), fp.get(p[1], "")))),
                                       p[0], p[1]))


def fallback_judge(a: str, b: str, s: dict):
    """Conservative lexical-only judgment used when the LLM is unavailable."""
    if s["fwd_ab"] >= CONTINUOUS_THRESHOLD and s["fwd_ab"] - s["fwd_ba"] >= DIRECTION_MARGIN:
        return "same_continuous", a, b
    if s["fwd_ba"] >= CONTINUOUS_THRESHOLD and s["fwd_ba"] - s["fwd_ab"] >= DIRECTION_MARGIN:
        return "same_continuous", b, a
    if s["overall"] >= SAME_MEETING_THRESHOLD and s["shared"] >= MIN_SHARED_KEYWORDS:
        if s["fwd_ab"] >= WEAK_DIRECTION and s["fwd_ab"] >= s["fwd_ba"]:
            return "same_gap", a, b
        if s["fwd_ba"] >= WEAK_DIRECTION:
            return "same_gap", b, a
        return "same_reorder", a, b
    return "different", a, b


# ---------------------------------------------------------------- LLM

def ollama_generate(url: str, model: str, prompt: str, timeout: float) -> str:
    """Single module-level HTTP call (kept mockable for tests). stdlib only."""
    request = urllib.request.Request(
        url.rstrip("/") + "/api/generate",
        data=json.dumps({"model": model, "prompt": prompt, "stream": False,
                         "options": {"temperature": 0}}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return str(payload.get("response", ""))


def classify_llm_error(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "llm_timeout"
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            return "llm_timeout"
        return "llm_connection"
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return "llm_bad_json"
    return "llm_connection"


def parse_json_array(text: str) -> list:
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("no JSON array in response")
    payload = json.loads(text[start:end + 1])
    if not isinstance(payload, list):
        raise ValueError("response is not a JSON array")
    return payload


def judgment_from_item(item, known_ids: set):
    """Validate one LLM entry -> ((a, b), judgment). Raises ValueError."""
    if not isinstance(item, dict):
        raise ValueError("entry is not an object")
    pair = item.get("pair")
    if not isinstance(pair, list) or len(pair) != 2:
        raise ValueError("bad pair")
    a, b = str(pair[0]), str(pair[1])
    if a not in known_ids or b not in known_ids or a == b:
        raise ValueError("unknown pair")
    relation = item.get("relation")
    if relation not in RELATIONS:
        raise ValueError("bad relation")
    left, right = str(item.get("left") or ""), str(item.get("right") or "")
    if relation in ("same_continuous", "same_gap"):
        if {left, right} != {a, b}:
            raise ValueError("left/right must be the pair ids")
    else:
        left, right = a, b  # pair given order; no adjacency claimed
    left_summary = right_summary = ""
    if relation == "same_gap":
        left_summary = str(item.get("left_summary") or "").strip()[:SUMMARY_MAX]
        right_summary = str(item.get("right_summary") or "").strip()[:SUMMARY_MAX]
    return (a, b), {"relation": relation, "left": left or a, "right": right or b,
                    "left_summary": left_summary, "right_summary": right_summary}


def key_of_unknown(item):
    """Best-effort sorted pair key from an invalid LLM entry (else None)."""
    if not isinstance(item, dict):
        return None
    pair = item.get("pair")
    if isinstance(pair, list) and len(pair) == 2:
        a, b = str(pair[0]), str(pair[1])
        return (a, b) if a <= b else (b, a)
    return None


def build_prompt(frag_by_id: dict, chunk: list, scores: dict | None = None) -> str:
    lines = [PROMPT_HEAD, "", "片段原文："]
    # Canonical in-prompt ordering: content fingerprint first, ID only as
    # tie-breaker — same corpus, same prompt bytes, regardless of upload order.
    fp = {fid: content_fingerprint(frag) for fid, frag in frag_by_id.items()}
    for fid in sorted({fid for pair in chunk for fid in pair},
                      key=lambda f: (fp.get(f, ""), f)):
        frag = frag_by_id[fid]
        lines.append(f"- {fid} 头部：{frag['head']}")
        lines.append(f"- {fid} 尾部：{frag['tail']}")
    lines.append("待判定片段对（附本地词面证据，仅供参考）：")
    for a0, b0 in chunk:
        s = (scores or {}).get((a0, b0)) or (scores or {}).get((b0, a0))
        a, b = a0, b0
        fwd_lr = s.get("fwd_ab", 0.0) if s else 0.0
        fwd_rl = s.get("fwd_ba", 0.0) if s else 0.0
        # Canonical within-pair orientation: smaller fingerprint on the left.
        # When this flips the ID order, the directional scores swap too, so the
        # (left→right, right→left) pair stays content-stable under renaming.
        if fp.get(a, "") > fp.get(b, ""):
            a, b = b, a
            fwd_lr, fwd_rl = fwd_rl, fwd_lr
        line = f'- ["{a}", "{b}"]'
        if s:
            shared = sorted(s.get("shared_sample") or [])
            hint = f'共享关键词数 {s.get("shared", 0)}'
            if shared:
                hint += f"（如 {'、'.join(shared[:5])}）"
            line += f" // 词面证据: {hint}，尾首衔接分 fwd={fwd_lr:.2f}/{fwd_rl:.2f}"
        lines.append(line)
    return "\n".join(lines)


def judge_pairs_with_llm(frag_by_id: dict, pairs: list, url: str, model: str,
                         timeout: float, degraded: list, scores: dict | None = None) -> dict:
    """Batched four-way judgment; one retry per batch, then conservative fallback."""
    judgments = {}
    for start in range(0, len(pairs), LLM_BATCH):
        chunk = pairs[start:start + LLM_BATCH]
        prompt = build_prompt(frag_by_id, chunk, scores)
        entries, invalid, error = None, set(), None
        for _attempt in range(2):  # single retry on any failure
            entries, invalid, error = {}, set(), None
            try:
                text = ollama_generate(url, model, prompt, timeout)
                items = parse_json_array(text)
                for item in items:
                    try:
                        key, judgment = judgment_from_item(item, set(frag_by_id))
                        entries.setdefault(key, judgment)
                    except ValueError:
                        invalid.add(key_of_unknown(item))
                break
            except Exception as exc:  # urllib, timeout, JSON -> retry once
                error = exc
                entries = None
        if entries is None:
            reason = classify_llm_error(error) if error else "llm_bad_json"
            for a, b in chunk:
                degraded.append({"left": a, "right": b, "reason": reason})
            continue
        for a, b in chunk:
            judgment = entries.get((a, b))
            if judgment is None:
                judgment = entries.get((b, a))  # tolerate swapped pair order
            if judgment is not None:
                judgments[(a, b)] = judgment
            elif (a, b) in invalid or (b, a) in invalid:
                degraded.append({"left": a, "right": b, "reason": "llm_invalid_entry"})
            else:
                degraded.append({"left": a, "right": b, "reason": "llm_no_entry"})

    # Rescue pass: degraded pairs get one single-pair retry each — short
    # outputs are what small local models handle most reliably, and one
    # request per pair is bounded by MAX_NEIGHBORS×N pairs in the worst case.
    stuck = {(d["left"], d["right"]) for d in degraded}
    rescue = [p for p in pairs if p in stuck and p not in judgments]
    for a, b in rescue:
        prompt = build_prompt(frag_by_id, [(a, b)], scores)
        try:
            items = parse_json_array(ollama_generate(url, model, prompt, timeout))
            for item in items:
                key, judgment = judgment_from_item(item, set(frag_by_id))
                if key in ((a, b), (b, a)):
                    judgment["source"] = "llm"
                    judgments[(a, b)] = judgment
                    break
        except Exception:
            continue  # keep the batch-loop degradation reason
    degraded[:] = [d for d in degraded if (d["left"], d["right"]) not in judgments]
    return judgments


# ---------------------------------------------------------------- assembly

def order_group_with_llm(frag_by_id: dict, ids: list, url: str, model: str,
                          timeout: float) -> list | None:
    """Second-stage group ordering: a dedicated sequence prompt.

    Pair-direction edges are often contradictory (A before B in one pair,
    after in another); ordering a KNOWN-same-meeting set is an easier task
    than pair adjudication, and discourse anchors (opening/closing lines,
    item progression) carry strong signal. Returns the ordered id list, or
    None to keep the topological order."""
    lines = ["把同一场会议的片段按内容先后排序。只输出一个 JSON 数组（按先后顺序的片段 id 列表），不要输出其他文字。",
             "排序线索：开场语（如 Good morning / Welcome / Let us start）在前；收尾语（如 To conclude / Final reminders / Thank you）在后；议程递进（第一项→下一项→总结）。"]
    for fid in ids:
        frag = frag_by_id.get(fid)
        if not frag:
            return None
        lines.append(f"- {fid} 头部：{frag['head']}")
        lines.append(f"- {fid} 尾部：{frag['tail']}")
    try:
        text = ollama_generate(url, model, "\n".join(lines), timeout)
        items = parse_json_array(text)
        if not isinstance(items, list) or sorted(str(x) for x in items) != sorted(ids):
            return None
        return [str(x) for x in items]
    except Exception:
        return None


def union_find_parent(ids: list) -> dict:
    parent = {fid: fid for fid in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    return parent, find


def topo_order(ids: list, edges: list, key) -> list:
    """Kahn's algorithm; deterministic tie-break by key. None on cycle."""
    id_set = set(ids)
    edges = sorted({(u, v) for u, v in edges if u in id_set and v in id_set and u != v})
    successor = defaultdict(list)
    indegree = {fid: 0 for fid in ids}
    for u, v in edges:
        successor[u].append(v)
        indegree[v] += 1
    available = [fid for fid in ids if indegree[fid] == 0]
    order = []
    while available:
        available.sort(key=key)
        current = available.pop(0)
        order.append(current)
        for nxt in successor[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                available.append(nxt)
    return order if len(order) == len(ids) else None


def assemble(fragments: list, relations: dict, degraded_keys: set) -> tuple:
    """relations[(a,b)] -> judgment dict. Returns (groups, junctions)."""
    by_id = {f["id"]: f for f in fragments}
    parent, find = union_find_parent([f["id"] for f in fragments])

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for (a, b), judgment in relations.items():
        if judgment["relation"] in GROUPING_RELATIONS:
            union(a, b)
    members_of = defaultdict(list)
    for fid in by_id:
        members_of[find(fid)].append(fid)

    groups, junctions = [], []
    for members in members_of.values():
        member_set = set(members)
        cont_edges, gap_edges, group_junctions = [], [], []
        degraded_inside = False
        for (a, b), judgment in relations.items():
            if not (a in member_set and b in member_set):
                continue
            relation = judgment["relation"]
            if relation == "different":
                continue
            entry = {"left": judgment["left"], "right": judgment["right"],
                     "relation": relation, "gap": relation == "same_gap",
                     "left_summary": judgment["left_summary"] if relation == "same_gap" else "",
                     "right_summary": judgment["right_summary"] if relation == "same_gap" else "",
                     "source": judgment["source"]}
            group_junctions.append(entry)
            if relation == "same_continuous":
                cont_edges.append((judgment["left"], judgment["right"]))
            elif relation == "same_gap":
                gap_edges.append((judgment["left"], judgment["right"]))
            if judgment["source"] == "fallback" and (a, b) in degraded_keys:
                degraded_inside = True
        key = lambda fid: order_key(by_id[fid])  # noqa: E731
        order = topo_order(members, cont_edges + gap_edges, key)
        cycle = False
        if order is None:
            cycle = True
            order = topo_order(members, cont_edges, key)
        if order is None:
            order = sorted(members, key=key)
        hints = [by_id[fid].get("note_hint_index") for fid in order
                 if by_id[fid].get("note_hint_index") is not None]
        confidence = confidence_of(order, relations, len(members), cycle,
                                   degraded_inside, judgments_used=any(
                                       j["source"] == "llm" for j in group_junctions))
        position = {fid: idx for idx, fid in enumerate(order)}
        group_junctions.sort(key=lambda j: (position[j["left"]], position[j["right"]]))
        junctions.extend(group_junctions)
        groups.append({"members": order, "cycle": cycle,
                       "confidence": confidence,
                       "note_order_hint": min(hints) if hints else None,
                       "note_order_source": next(
                           (by_id[fid].get("note_hint_source") for fid in order
                            if by_id[fid].get("note_hint_index") is not None), None)})
    return groups, junctions


def confidence_of(order: list, relations: dict, size: int, cycle: bool,
                  degraded_inside: bool, judgments_used: bool) -> str:
    if size <= 1:
        return "low"
    if cycle or degraded_inside:
        return "low"
    if not judgments_used:
        return "medium"
    for left, right in zip(order, order[1:]):
        judgment = relations.get((left, right) if (left, right) in relations else (right, left))
        if not judgment or judgment["relation"] != "same_continuous" \
                or judgment["left"] != left or judgment["right"] != right:
            return "medium"
    return "high"


# ---------------------------------------------------------------- output

def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def export_fragment(frag: dict) -> dict:
    return {key: frag[key] for key in
            ("id", "source", "sentence_count", "duration_seconds", "head", "tail",
             "keywords", "note_hint_index", "note_hint_source", "note_hint_score")}


def build_plan(args, fragments: list, skipped: list, references: list,
               relations: dict, degraded_keys: set, degraded: list,
               llm_used: bool, llm_note, extra_notice: str = "",
               frag_by_id: dict | None = None) -> dict:
    groups, junctions = assemble(fragments, relations, degraded_keys)
    if llm_used and frag_by_id:
        # 二级排序通道：分组已定后，每组单独做一次纯排序（判对边常互相
        # 矛盾，拓扑序只是稳态不是真相；排序任务本身比判向容易）。
        for group in groups:
            if len(group["members"]) < 2:
                continue
            ordered = order_group_with_llm(
                frag_by_id, group["members"], args.ollama_url,
                args.ollama_model, args.pair_timeout)
            if ordered:
                group["members"] = ordered
    groups.sort(key=lambda g: (g["note_order_hint"] is None,
                               g["note_order_hint"] if g["note_order_hint"] is not None else 0,
                               g["members"][0] if g["members"] else ""))
    exported_groups = []
    for index, group in enumerate(groups, start=1):
        exported_groups.append({
            "group_id": f"g{index:02d}",
            "fragment_ids": group["members"],
            "confidence": group["confidence"],
            "note_order_source": group["note_order_source"],
            "note_order_hint": group["note_order_hint"],
            "cycle_conflict": group["cycle"],
        })
    hinted = [g for g in exported_groups if g["note_order_hint"] is not None]
    if hinted and len(hinted) == len(exported_groups):
        basis = "note"
    elif hinted:
        basis = "mixed"
    else:
        basis = "alphabetical"
    position = {gid: i for i, gid in enumerate(
        fid for g in exported_groups for fid in g["fragment_ids"])}
    junctions.sort(key=lambda j: (position[j["left"]], position[j["right"]]))
    return {
        "generated_ts": time.time(),
        "kind": "fragment_assembly_plan",
        "status": "draft",
        "llm_used": llm_used,
        "llm_model": args.ollama_model,
        "llm_skipped_reason": llm_note,
        "fragments": [export_fragment(f) for f in sorted(fragments, key=lambda f: f["id"])],
        "groups": exported_groups,
        "junctions": junctions,
        "references": references,
        "skipped": skipped,
        "degraded_pairs": degraded,
        "group_order_basis": basis,
        "notice": NOTICE if not extra_notice else f"{NOTICE}{extra_notice}",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="碎片装配计划生成器 v2（语义级）")
    parser.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--references", default=None,
                        help="笔记参考目录；缺省为 <runs-dir>/.fragment-references")
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--ollama-model", default=DEFAULT_OLLAMA_MODEL)
    parser.add_argument("--no-llm", action="store_true",
                        help="跳过 LLM 精判，直接用词重叠兜底")
    parser.add_argument("--pair-timeout", type=float, default=DEFAULT_PAIR_TIMEOUT)
    parser.add_argument("--probe-timeout", type=float, default=90.0,
                        help="LLM 探活超时（秒）。keep_alive:0 下 5GB 模型冷加载远超批量调用耗时，探活须单独放宽。")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    runs_dir = Path(args.runs_dir)
    fragments, skipped = collect_fragments(runs_dir)
    references_path = Path(args.references) if args.references else runs_dir / ".fragment-references"
    references, segments = load_references(references_path)
    attach_note_hints(fragments, segments)

    frag_by_id = {f["id"]: f for f in fragments}
    scores = pair_scores(fragments)
    pairs = candidate_pairs(fragments, scores)

    degraded, relations = [], {}
    llm_used, llm_note = False, None
    llm_all_different_hint = ""
    if args.no_llm:
        llm_note = "disabled_by_flag"
    elif not pairs:
        llm_note = "no_candidate_pairs"
    else:
        try:
            # Probe doubles as a model warm-up: keep_alive:0 (iron rule 1)
            # means every real call starts with a multi-GB cold load, so the
            # probe budget must be far larger than the per-pair timeout.
            ollama_generate(args.ollama_url, args.ollama_model,
                            PROBE_PROMPT, args.probe_timeout)
            llm_used = True
        except Exception:
            llm_note = "ollama_unreachable"
    if llm_used:
        judgments = judge_pairs_with_llm(frag_by_id, pairs, args.ollama_url,
                                         args.ollama_model, args.pair_timeout, degraded,
                                         scores)
        for a, b in pairs:
            judgment = judgments.get((a, b))
            if judgment is not None:
                judgment["source"] = "llm"
                relations[(a, b)] = judgment
    for pair in pairs:
        if pair in relations:
            continue
        relation, left, right = fallback_judge(pair[0], pair[1], scores[pair])
        relations[pair] = {"relation": relation, "left": left, "right": right,
                           "left_summary": "", "right_summary": "",
                           "source": "fallback"}

    # 诚实校准提示：LLM 在场却把所有片段对都判为不同会议时，用户需要知道
    # 这可能是模型保守，而不是事实；工作台支持手动合并。
    if llm_used and pairs and relations and \
            all(r.get("relation") == "different" for r in relations.values()):
        llm_all_different_hint = (
            "所有片段对均被判为不同会议；片段较短时模型可能偏保守。"
            "若您知道其中某些片段属于同一会议，可在装配工作台中手动调整。")

    degraded_keys = {(d["left"], d["right"]) for d in degraded}
    plan = build_plan(args, fragments, skipped, references, relations,
                      degraded_keys, degraded, llm_used, llm_note,
                      extra_notice=llm_all_different_hint, frag_by_id=frag_by_id)
    if not fragments:
        # Never clobber an existing plan with an empty one: wrong --runs-dir
        # must be visible as exit code 2, not as data loss.
        print(f"fragment_assembly: no fragment inputs under {runs_dir}", file=sys.stderr)
        return 2
    try:
        atomic_write_json(Path(args.output), plan)
    except OSError as exc:
        print(f"fragment_assembly: cannot write output {args.output}: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
