"""Load ICD-9-CM diagnosis codes and the CMS ICD-9-CM -> ICD-10-CM General Equivalence Mappings.

Nova Scotia MSI (and several other provinces) still take ICD-9 diagnosis codes on physician
claims, so these give NS claim codes a path to ICD-10-CM, UMLS and lay terms.

Both are public domain CMS files. Download the final ICD-9-CM release (v32, FY2015,
"ICD-9-CM Diagnosis and Procedure Codes: Abbreviated and Full Code Titles") and the 2018
diagnosis GEMs, then point the loader at the zips or a directory holding them. It uses:

  CMS32_DESC_LONG_DX.txt  (or ..._SHORT_DX)  code, whitespace, description     (required)
  2018_I9gem.txt                              ICD-9 code, ICD-10 code, 5 flags   (optional)

GEM flags are approximate / no map / combination / scenario / choice list. Exact entries
become 'maps_to', approximate ones 'maps_to_approx'; 'no map' rows are skipped. Load
ICD-10-CM first: GEM targets that aren't in the database are counted, not created.
"""

import re
from pathlib import Path

from medterms.db import Database
from medterms.loaders.files import decode, find_files
from medterms.loaders.icd10cm import dotted as icd10_dotted
from medterms.normalize import normalize_term

VOCAB = "ICD9CM"
SOURCE = "CMS_ICD9CM"
SOURCE_GEM = "CMS_GEM"

FILE_PATTERNS = {
    "long": re.compile(r"CMS\d+_DESC_LONG_DX\.txt$", re.I),
    "short": re.compile(r"CMS\d+_DESC_SHORT_DX\.txt$", re.I),
    "gem": re.compile(r"\d{4}_I9gem\.txt$", re.I),
}
CODE = re.compile(r"^(\d{3,5}|V\d{2,4}|E\d{3,4})$")
LINE = re.compile(r"^(\S+)\s+(.*\S)\s*$")


def dotted(code: str) -> str:
    """'0010' -> '001.0', 'V011' -> 'V01.1', 'E8000' -> 'E800.0'."""
    split = 4 if code.startswith("E") else 3
    return code if len(code) <= split else f"{code[:split]}.{code[split:]}"


def parse_descriptions(text: str) -> tuple[dict[str, str], int]:
    codes, rejected = {}, 0
    for line in text.splitlines():
        m = LINE.match(line)
        if m and CODE.match(m.group(1)):
            codes[dotted(m.group(1))] = m.group(2)
        elif line.strip():
            rejected += 1
    return codes, rejected


def parse_gem(text: str) -> list[tuple[str, str, str]]:
    """[(icd9 dotted, icd10 dotted, flags)] from lines like '0010  A000  00000'."""
    rows = []
    for line in text.split("\n"):
        parts = line.split()
        if len(parts) == 3 and len(parts[2]) == 5 and parts[2].isdigit():
            rows.append((dotted(parts[0]), icd10_dotted(parts[1]), parts[2]))
    return rows


def load(db: Database, paths: list[Path]) -> dict[str, int]:
    db.init_schema()
    files = find_files(paths, FILE_PATTERNS)
    desc = files.get("long") or files.get("short")
    if desc is None:
        raise FileNotFoundError("no CMS##_DESC_LONG_DX.txt / _SHORT_DX.txt found in " + ", ".join(map(str, paths)))
    version = re.search(r"CMS(\d+)", desc[0], re.I).group(1)
    codes, rejected = parse_descriptions(decode(desc[1]))

    db.execute("UPDATE vocabulary SET version = ? WHERE vocabulary_id = ?", (f"v{version}", VOCAB))
    existing = dict(db.query("SELECT code, concept_id FROM concept WHERE vocabulary_id = ?", (VOCAB,)))
    next_id = db.next_concept_id()
    ids, rows = {}, []
    for code, name in codes.items():
        cid = existing.get(code)
        if cid is None:
            cid, next_id = next_id, next_id + 1
        ids[code] = cid
        domain = "observation" if code[0] in "VE" else "condition"
        rows.append((cid, VOCAB, code, name, domain, "code", 1))
    db.executemany(
        "INSERT INTO concept (concept_id, vocabulary_id, code, name, domain, concept_class, is_billable) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (vocabulary_id, code) DO UPDATE SET name = excluded.name, domain = excluded.domain",
        rows,
    )

    db.execute("DELETE FROM concept_synonym WHERE source = ?", (SOURCE,))
    db.executemany(
        "INSERT INTO concept_synonym (concept_id, term, term_normalized, term_type, source) VALUES (?, ?, ?, 'preferred', ?)",
        [(ids[c], name, normalize_term(name), SOURCE) for c, name in codes.items() if normalize_term(name)],
    )

    stats = {"codes": len(codes), "rejected_lines": rejected, "maps_to": 0, "maps_to_approx": 0,
             "no_map": 0, "icd10cm_not_loaded": 0}
    if "gem" in files:
        db.execute("DELETE FROM concept_relationship WHERE source = ?", (SOURCE_GEM,))
        icd10 = dict(db.query("SELECT code, concept_id FROM concept WHERE vocabulary_id = 'ICD10CM'"))
        links = {}
        for icd9, icd10_code, flags in parse_gem(decode(files["gem"][1])):
            if flags[1] == "1":
                stats["no_map"] += 1
                continue
            src, dst = ids.get(icd9), icd10.get(icd10_code)
            if src is None or dst is None:
                stats["icd10cm_not_loaded"] += dst is None
                continue
            approx = flags[0] == "1"
            scenario = int(flags[3]) if flags[2] == "1" else None
            rel, rev = ("maps_to_approx", "approx_mapped_from") if approx else ("maps_to", "mapped_from")
            links.setdefault((src, dst, rel), (src, dst, rel, scenario, SOURCE_GEM))
            links.setdefault((dst, src, rev), (dst, src, rev, scenario, SOURCE_GEM))
            stats[rel] += 1
        db.executemany(
            "INSERT INTO concept_relationship (concept_id_1, concept_id_2, relationship_id, map_priority, source) "
            "VALUES (?, ?, ?, ?, ?)",
            list(links.values()),
        )
    db.commit()
    return stats
