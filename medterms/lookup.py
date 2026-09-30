"""Find codes for a term, and measure how well a list of lay terms reaches the expected codes."""

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from medterms.db import Database
from medterms.normalize import normalize_term

# Hops followed from a term's concept to codes: UMLS concept -> its codes, curated lay mappings,
# cross-vocabulary maps such as the ICD-9-CM GEMs (both directions, so an ICD-10-CM index term
# reaches the ICD-9-CM codes NS claims use), and name-matched links from `medterms link`.
MAPPING_RELATIONSHIPS = ("umls_cui_of", "may_be", "maps_to", "maps_to_approx", "mapped_from", "approx_mapped_from")
# Confidence for links stored without one: code-to-code maps are less certain than a concept's own codes.
# Manifestation codes ("... in diseases classified elsewhere") can't be a primary diagnosis: they count half.
MANIFESTATION = re.compile(r"\bin (?:diseases|conditions) classified elsewhere\b", re.I)
DEFAULT_CONFIDENCE = ("CASE WHEN r.relationship_id IN ('maps_to', 'mapped_from') THEN 0.8 "
                      "WHEN r.relationship_id IN ('maps_to_approx', 'approx_mapped_from') THEN 0.7 ELSE 1 END")


@dataclass
class Match:
    vocabulary: str
    code: str
    name: str
    term: str          # the synonym that matched
    term_type: str
    billable: int
    confidence: float  # 1 for codes named by the term itself or linked by UMLS / curation


def find_terms(db: Database, term: str, limit: int = 20) -> list[Match]:
    """Concepts in any vocabulary with this term as a synonym (exact, then prefix)."""
    norm = normalize_term(term)
    rows = db.query(
        "SELECT c.vocabulary_id, c.code, c.name, s.term, s.term_type, c.is_billable "
        "FROM concept_synonym s JOIN concept c ON c.concept_id = s.concept_id "
        "WHERE s.term_normalized = ? OR s.term_normalized LIKE ? "
        "ORDER BY CASE WHEN s.term_normalized = ? THEN 0 ELSE 1 END, c.is_billable DESC, c.code LIMIT ?",
        (norm, norm + " %", norm, limit))
    return [Match(*row, 1.0) for row in rows]


def find_codes(db: Database, term: str, vocabulary: str, limit: int = 20) -> list[Match]:
    """Codes in `vocabulary` for a term, best first: exact term matches before prefix matches,
    then by link confidence (manifestation codes count half), then billable codes, then
    "unspecified" codes first."""
    norm = normalize_term(term)
    match = "(s.term_normalized = ? OR s.term_normalized LIKE ?)"
    hops = ", ".join("?" * len(MAPPING_RELATIONSHIPS))
    sql = (
        "SELECT t.vocabulary_id, t.code, t.name, s.term, s.term_type, t.is_billable, "
        f"CASE WHEN s.term_normalized = ? THEN 0 ELSE 1 END, COALESCE(r.confidence, {DEFAULT_CONFIDENCE}) "
        "FROM concept_synonym s JOIN concept c ON c.concept_id = s.concept_id "
        f"JOIN concept_relationship r ON r.concept_id_1 = c.concept_id AND r.relationship_id IN ({hops}) "
        "JOIN concept t ON t.concept_id = r.concept_id_2 AND t.vocabulary_id = ? "
        f"WHERE {match} "
        "UNION ALL "
        "SELECT c.vocabulary_id, c.code, c.name, s.term, s.term_type, c.is_billable, "
        "CASE WHEN s.term_normalized = ? THEN 0 ELSE 1 END, 1 "
        "FROM concept_synonym s JOIN concept c ON c.concept_id = s.concept_id AND c.vocabulary_id = ? "
        f"WHERE {match} LIMIT 5000"
    )
    params = [norm, *MAPPING_RELATIONSHIPS, vocabulary, norm, norm + " %", norm, vocabulary, norm, norm + " %"]
    best: dict[str, tuple[tuple, Match]] = {}
    for vocab, code, name, matched, term_type, billable, exact, confidence in db.query(sql, params):
        confidence = float(confidence) * (0.5 if MANIFESTATION.search(name) else 1.0)
        rank = (exact, -confidence, -billable, "unspecified" not in name.lower())
        if code not in best or rank < best[code][0]:
            best[code] = (rank, Match(vocab, code, name, matched, term_type, billable, confidence))
    return [m for _, m in sorted(best.values(), key=lambda b: (b[0], b[1].code))][:limit]


def evaluate(db: Database, path: Path, top: int = 3, verbose: bool = False) -> dict[str, float]:
    """Read CSV rows (term, icd10, icd9, ns) and report how often the expected codes come up.

    icd10 / icd9 hold expected code prefixes, alternatives separated by '|' ('J20|J40');
    a hit is a code in the top `top` results that starts with one of them. ns is '*' when
    the term should reach some NS fee code. Empty cells aren't scored.
    """
    columns = {"icd10": "ICD10CM", "icd9": "ICD9CM", "ns": "NS_MSI"}
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("term") and not r["term"].startswith("#")]
    scored = {c: [0, 0, 0] for c in columns}  # expected, reached anything, hit in top N
    misses: dict[str, list[str]] = {c: [] for c in columns}
    for row in rows:
        for column, vocab in columns.items():
            expected = (row.get(column) or "").strip()
            if not expected:
                continue
            results = find_codes(db, row["term"], vocab, limit=max(top, 5))
            scored[column][0] += 1
            scored[column][1] += bool(results)
            prefixes = expected.split("|")
            hit = bool(results) if expected == "*" else any(
                m.code.startswith(p) for m in results[:top] for p in prefixes)
            scored[column][2] += hit
            if not hit:
                misses[column].append(row["term"])
            if verbose:
                shown = ", ".join(f"{m.code} {m.name[:40]} ({m.confidence:.2f})" for m in results[:top]) or "-"
                print(f"{'ok  ' if hit else 'MISS'} {vocab:8} {row['term']:28} want {expected:12} got {shown}")
    summary = {}
    print(f"{len(rows)} terms")
    for column, (expected, reached, hits) in scored.items():
        if not expected:
            continue
        summary[column] = hits / expected
        print(f"{columns[column]:8} expected {expected:3}  reached a code {reached:3} ({reached / expected:.0%})  "
              f"expected code in top {top}: {hits:3} ({hits / expected:.0%})")
        if misses[column]:
            print(f"         misses: {', '.join(misses[column])}")
    return summary
