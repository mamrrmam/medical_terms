"""Load an ICD-10-CM release from CDC/NCHS into the concept tables.

Download the fiscal-year release from https://www.cdc.gov/nchs/icd/icd-10-cm/files.html
(or ftp.cdc.gov/pub/Health_Statistics/NCHS/Publications/ICD10CM/<year>/). Point the loader at
the downloaded zip files or at a directory holding them (extracted or not). It uses:

  icd10cm[-_]order[-_]YYYY.txt     codes, billable flag and descriptions   (required)
  icd10cm[-_]tabular[-_]YYYY.xml   chapters, blocks and inclusion terms      (optional)
  icd10cm[-_]index[-_]YYYY.xml     Alphabetic Index entries                  (optional)

What gets written:
  concept               one row per code, plus chapters and blocks from the tabular file
  concept_synonym       long description ('preferred'), inclusion terms ('clinical'),
                        index entries ('index')
  concept_relationship  'is_a' links from each code to its parent code / block / chapter

Reloading a newer release keeps concept_ids for existing codes, updates their names,
and sets valid_end on codes that were dropped from the new release.
"""

import datetime
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from medterms.db import Database
from medterms.loaders.files import decode, find_files
from medterms.normalize import normalize_term

VOCAB = "ICD10CM"
SRC_CODES = "ICD10CM"
SRC_TABULAR = "ICD10CM_TABULAR"
SRC_INDEX = "ICD10CM_INDEX"

FILE_PATTERNS = {
    "order": re.compile(r"icd10cm[-_]order[-_](\d{4})\.txt$", re.I),
    "tabular": re.compile(r"icd10cm[-_]tabular[-_](\d{4})\.xml$", re.I),
    "index": re.compile(r"icd10cm[-_]index[-_](\d{4})\.xml$", re.I),
}

MAX_TERM_LEN = 1000  # concept_synonym.term is VARCHAR(1000)


@dataclass
class Code:
    code: str  # dotted, e.g. 'F41.1'
    name: str
    is_billable: bool
    concept_class: str = "code"
    domain: str = "condition"


@dataclass
class Release:
    year: int
    codes: dict[str, Code] = field(default_factory=dict)  # dotted code -> Code
    parents: dict[str, str] = field(default_factory=dict)  # code -> parent code
    inclusion_terms: list[tuple[str, str]] = field(default_factory=list)  # (code, term)
    index_terms: list[tuple[str, str]] = field(default_factory=list)  # (code as printed, term)
    has_tabular: bool = False


def dotted(code: str) -> str:
    """'F411' -> 'F41.1'; three-character categories have no dot."""
    code = code.strip().replace(".", "")
    return code if len(code) <= 3 else f"{code[:3]}.{code[3:]}"


def domain_for(code: str) -> str:
    first = code[:1].upper()
    if first == "R":
        return "symptom"  # Ch. 18: symptoms, signs and abnormal findings
    if first in "VWXYZ":
        return "observation"  # external causes and factors influencing health status
    return "condition"


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_order_file(text: str, release: Release) -> None:
    """Fixed-width 'Code Descriptions in Tabular Order' file.

    cols 1-5 order number, 7-13 code (no dot), 15 flag (1 = valid for claims,
    0 = header), 17-76 short description, 78+ long description.
    """
    for line in text.splitlines():
        if len(line) < 16 or not line[:5].strip().isdigit():
            continue
        raw_code = line[6:13].strip()
        billable = line[14] == "1"
        long_desc = line[77:].strip() or line[16:76].strip()
        code = dotted(raw_code)
        release.codes[code] = Code(
            code=code,
            name=long_desc,
            is_billable=billable,
            concept_class="category" if len(raw_code) == 3 else ("code" if billable else "subcategory"),
            domain=domain_for(code),
        )

    # Parent = longest shorter code in the release that this code starts with.
    undotted = {c.replace(".", ""): c for c in release.codes}
    for code in release.codes:
        raw = code.replace(".", "")
        for n in range(len(raw) - 1, 2, -1):
            if raw[:n] in undotted:
                release.parents[code] = undotted[raw[:n]]
                break


def _text(el) -> str:
    return " ".join((el.text or "").split()) if el is not None else ""


def parse_tabular(root: ET.Element, release: Release) -> None:
    """Chapters, blocks (sections) and inclusion terms from the Tabular List XML."""
    for chapter in root.iter("chapter"):
        ch_num = _text(chapter.find("name"))
        ch_code = f"CH{int(ch_num):02d}" if ch_num.isdigit() else f"CH{ch_num}"
        release.codes.setdefault(ch_code, Code(ch_code, _text(chapter.find("desc")), False, "chapter", "other"))

        for section in chapter.findall("section"):
            block = section.get("id", "").strip()
            if not block:
                continue
            release.codes.setdefault(
                block, Code(block, _text(section.find("desc")), False, "block", domain_for(block))
            )
            release.parents.setdefault(block, ch_code)
            for diag in section.findall("diag"):  # top-level diags are the 3-character categories
                cat = dotted(_text(diag.find("name")))
                if cat in release.codes:
                    release.parents.setdefault(cat, block)

    for diag in root.iter("diag"):
        code = dotted(_text(diag.find("name")))
        inclusion = diag.find("inclusionTerm")
        if inclusion is not None:
            for note in inclusion.findall("note"):
                release.inclusion_terms.append((code, _text(note)))


def _index_title(title_el) -> str:
    """Title text without the <nemod> nonessential modifiers in parentheses."""
    parts = [title_el.text or ""]
    for child in title_el:
        if child.tag != "nemod":
            parts.append("".join(child.itertext()))
        parts.append(child.tail or "")
    return " ".join("".join(parts).split())


def parse_index(root: ET.Element, release: Release) -> None:
    """Alphabetic Index: each mainTerm/term with a <code> becomes a synonym for that code.

    Sub-terms are joined to their parents index-style: 'Anxiety, generalized'. Titles that list
    spelling variants ('Attack, attacks') also get a form using only the first variant
    ('Attack, panic' next to 'Attack, attacks, panic').
    """

    def walk(el, full_path, short_path):
        title_el = el.find("title")
        if title_el is None:
            return
        title = _index_title(title_el)
        if not title:
            return
        full = full_path + [title]
        short = short_path + [_first_variant(title)]
        for code_el in el.findall("code"):
            code = _text(code_el)
            release.index_terms.append((code, ", ".join(full)))
            if short != full:
                release.index_terms.append((code, ", ".join(short)))
        for child in el.findall("term"):
            walk(child, full, short)

    for main in root.iter("mainTerm"):
        walk(main, [], [])


def _first_variant(title: str) -> str:
    """'Attack, attacks' -> 'Attack'; 'Infarct, infarction' -> 'Infarct'. Other titles unchanged."""
    parts = [p.strip() for p in title.split(",")]
    stem = parts[0].lower()[:4]
    if len(parts) > 1 and len(stem) == 4 and all(p.lower().startswith(stem) for p in parts[1:]):
        return parts[0]
    return title


def read_release(paths: list[Path], year: int | None = None) -> Release:
    files = find_files(paths, FILE_PATTERNS)
    if "order" not in files:
        raise FileNotFoundError("no icd10cm order file (icd10cm-order-YYYY.txt) found in " + ", ".join(map(str, paths)))
    name, data = files["order"]
    release = Release(year=year or int(FILE_PATTERNS["order"].search(name).group(1)))
    parse_order_file(decode(data), release)
    if "tabular" in files:
        parse_tabular(ET.fromstring(files["tabular"][1]), release)
        release.has_tabular = True
    if "index" in files:
        parse_index(ET.fromstring(files["index"][1]), release)
    return release


# ---------------------------------------------------------------------------
# Writing to the database
# ---------------------------------------------------------------------------

def resolve_index_code(printed: str, known: dict[str, int]) -> int | None:
    """Index codes may be incomplete ('S72.00-', 'F41.-'); fall back to the nearest known ancestor."""
    raw = printed.strip().rstrip("-").rstrip(".").replace(".", "")
    while len(raw) >= 3:
        cid = known.get(dotted(raw))
        if cid is not None:
            return cid
        raw = raw[:-1]
    return None


def load(db: Database, release: Release) -> dict[str, int]:
    db.init_schema()
    fy_start = datetime.date(release.year - 1, 10, 1)  # FY2027 takes effect 2026-10-01

    db.execute(
        "INSERT INTO vocabulary (vocabulary_id, name, version, publisher, source_url, license, redistributable) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (vocabulary_id) DO UPDATE SET version = excluded.version",
        (VOCAB, "ICD-10-CM", f"FY{release.year}", "CDC / NCHS",
         "https://www.cdc.gov/nchs/icd/icd-10-cm/", "public domain", 1),
    )

    existing = {}
    structural = set()  # chapters and blocks only come from the tabular file
    for code, cid, concept_class in db.query(
        "SELECT code, concept_id, concept_class FROM concept WHERE vocabulary_id = ?", (VOCAB,)
    ):
        existing[code] = cid
        if concept_class in ("chapter", "block"):
            structural.add(code)
    first_load = not existing
    next_id = db.next_concept_id()
    ids: dict[str, int] = {}
    rows = []
    for code, c in release.codes.items():
        cid = existing.get(code)
        is_new = cid is None
        if is_new:
            cid, next_id = next_id, next_id + 1
        ids[code] = cid
        valid_start = None if first_load or not is_new else fy_start.isoformat()
        rows.append((cid, VOCAB, code, c.name, c.domain, c.concept_class, int(c.is_billable), valid_start))
    db.executemany(
        "INSERT INTO concept (concept_id, vocabulary_id, code, name, domain, concept_class, is_billable, valid_start, valid_end) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL) "
        "ON CONFLICT (vocabulary_id, code) DO UPDATE SET name = excluded.name, domain = excluded.domain, "
        "concept_class = excluded.concept_class, is_billable = excluded.is_billable, valid_end = NULL",
        rows,
    )

    retired = [
        code for code in existing
        if code not in release.codes and (release.has_tabular or code not in structural)
    ]
    db.executemany(
        "UPDATE concept SET valid_end = ? WHERE vocabulary_id = ? AND code = ? AND valid_end IS NULL",
        [((fy_start - datetime.timedelta(days=1)).isoformat(), VOCAB, code) for code in retired],
    )

    # Hierarchy
    db.execute("DELETE FROM concept_relationship WHERE source = ? AND relationship_id IN ('is_a', 'subsumes')", (SRC_CODES,))
    rel_rows = []
    for child, parent in release.parents.items():
        if child in ids and parent in ids:
            rel_rows.append((ids[child], ids[parent], "is_a", SRC_CODES))
            rel_rows.append((ids[parent], ids[child], "subsumes", SRC_CODES))
    db.executemany(
        "INSERT INTO concept_relationship (concept_id_1, concept_id_2, relationship_id, source) VALUES (?, ?, ?, ?)",
        rel_rows,
    )

    # Synonyms, de-duplicated on the table's primary key
    db.execute(
        "DELETE FROM concept_synonym WHERE source IN (?, ?, ?)", (SRC_CODES, SRC_TABULAR, SRC_INDEX)
    )
    synonyms: dict[tuple[int, str, str], tuple] = {}

    def add(cid, term, term_type, source):
        norm = normalize_term(term)
        if norm and len(term) <= MAX_TERM_LEN:
            synonyms.setdefault((cid, norm, term_type), (cid, term, norm, term_type, source))

    for code, c in release.codes.items():
        add(ids[code], c.name, "preferred", SRC_CODES)
    for code, term in release.inclusion_terms:
        if code in ids:
            add(ids[code], term, "clinical", SRC_TABULAR)
    unresolved = 0
    for printed, term in release.index_terms:
        cid = resolve_index_code(printed, ids)
        if cid is None:
            unresolved += 1
        else:
            add(cid, term, "index", SRC_INDEX)
    db.executemany(
        "INSERT INTO concept_synonym (concept_id, term, term_normalized, term_type, source) VALUES (?, ?, ?, ?, ?)",
        list(synonyms.values()),
    )
    db.commit()

    return {
        "codes": len(release.codes),
        "billable": sum(c.is_billable for c in release.codes.values()),
        "new": 0 if first_load else sum(1 for code in release.codes if code not in existing),
        "retired": len(retired),
        "is_a": len(rel_rows) // 2,
        "synonyms": len(synonyms),
        "index_unresolved": unresolved,
    }
