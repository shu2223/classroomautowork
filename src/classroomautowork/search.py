"""Local BM25 retrieval with CJK bigrams and immutable source locators."""

import math
import re
from collections import Counter


def tokens(text: str) -> list[str]:
    latin = re.findall(r"[a-z0-9_]{2,}", text.lower())
    cjk = re.findall(r"[\u3040-\u30ff\u3400-\u9fff]+", text)
    return latin + [word[i : i + 2] for word in cjk for i in range(max(1, len(word) - 1))]


def retrieve(chunks: list[dict], query: str, limit: int = 16) -> list[dict]:
    terms = set(tokens(query))
    if not terms or not chunks:
        return []
    bags = [Counter(tokens(x["title"] + "\n" + x["text"])) for x in chunks]
    avg = sum(sum(x.values()) for x in bags) / max(len(bags), 1) or 1
    frequency = {t: sum(t in bag for bag in bags) for t in terms}
    scored = []
    for chunk, bag in zip(chunks, bags, strict=True):
        score, length = 0.0, sum(bag.values())
        for term in terms:
            tf = bag[term]
            if tf:
                idf = math.log(1 + (len(chunks) - frequency[term] + 0.5) / (frequency[term] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / avg))
        if score:
            scored.append({**chunk, "score": round(score, 4)})
    return sorted(scored, key=lambda x: (-x["score"], x["id"]))[:limit]
