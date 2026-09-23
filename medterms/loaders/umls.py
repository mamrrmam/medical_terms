"""Load the UMLS Metathesaurus (NLM) into the concept tables.

Needs a UMLS license (https://uts.nlm.nih.gov/uts/signup-login). Download the
"UMLS Metathesaurus Full Subset" or run MetamorphoSys, then point the loader at the
release directory (or its META/ subdirectory). It reads:

  MRCONSO.RRF   concept names, one row per atom             (required)
  MRSTY.RRF     semantic types, used to set concept.domain   (optional)
  MRFILES.RRF   column layout of the other files             (optional; defaults below)

Split or gzipped files (MRCONSO.RRF.aa.gz, ...) are read in name order.

What gets written:
  vocabulary 'UMLS'     one concept per CUI that has an atom in the selected sources;
                        every English string from those sources becomes a synonym of it
                        (CHV and MEDLINEPLUS strings as term_type 'lay')
  SNOMED, RXNORM,       one concept per source code, linked to its CUI with
  MESH, LOINC           'has_umls_cui' / 'umls_cui_of'
  ICD10CM               existing concepts (from the ICD-10-CM loader) get linked to their
                        CUIs; ICD-10-CM codes are never created or renamed here

So "heart attack" (a CHV string on a CUI) reaches ICD-10-CM through the CUI's
'umls_cui_of' links, and SNOMED codes reach ICD-10-CM through a shared CUI.

UMLS content may not be redistributed: keep the database out of this repository.
"""

import datetime
import gzip
import re
from pathlib import Path

from medterms.db import Database
from medterms.normalize import normalize_term

VOCAB = "UMLS"
SOURCE = "UMLS"

DEFAULT_SABS = ["MTH", "SNOMEDCT_US", "ICD10CM", "RXNORM", "MSH", "LNC", "CHV", "MEDLINEPLUS"]

# Sources whose codes become concepts of their own. Sources not listed only contribute strings.
CODE_VOCABS = {
    "SNOMEDCT_US": ("SNOMED", "SNOMED CT US Edition"),
    "RXNORM": ("RXNORM", "RxNorm"),
    "MSH": ("MESH", "Medical Subject Headings"),
    "LNC": ("LOINC", "LOINC"),
    "ICD10CM": ("ICD10CM", "ICD-10-CM"),
}
LINK_ONLY = {"ICD10CM"}  # owned by the ICD-10-CM loader

# Term types to prefer as a code's name, best first.
PREFERRED_TTY = {
    "SNOMEDCT_US": ["PT", "FN"],
    "RXNORM": ["IN", "PIN", "MIN", "SCD", "SBD", "BN", "GPCK", "BPCK", "SCDF", "SBDF"],
    "MSH": ["MH", "NM"],
    "LNC": ["LC", "LN"],
    "ICD10CM": ["PT"],
}
LAY_SABS = {"CHV", "MEDLINEPLUS"}
ABBREVIATION_TTYS = {"AB", "ACR", "AA"}
BRAND_TTYS = {"BN", "SBD", "SBDC", "SBDF", "SBDG", "BPCK"}

# Semantic type (TUI) -> domain. When a CUI has several, the earliest domain in DOMAIN_ORDER wins.
TUI_DOMAIN = {
    **dict.fromkeys(["T019", "T020", "T037", "T046", "T047", "T048", "T049", "T050", "T190", "T191"], "condition"),
    "T184": "symptom",
    **dict.fromkeys(["T121", "T195", "T200", "T125", "T129", "T131", "T127", "T109", "T197"], "drug"),
    **dict.fromkeys(["T058", "T060", "T061"], "procedure"),
    **dict.fromkeys(["T059", "T034", "T201"], "measurement"),
    **dict.fromkeys(["T074", "T075", "T203"], "device"),
    **dict.fromkeys(["T017", "T018", "T021", "T022", "T023", "T024", "T025", "T026", "T029", "T030", "T031"], "anatomy"),
    "T033": "observation",
}
DOMAIN_ORDER = ["condition", "symptom", "drug", "procedure", "measurement", "device", "anatomy", "observation", "other"]
VOCAB_DOMAIN = {"RXNORM": "drug", "LOINC": "measurement"}

DEFAULT_COLUMNS = {
    "MRCONSO.RRF": "CUI,LAT,TS,LUI,STT,SUI,ISPREF,AUI,SAUI,SCUI,SDUI,SAB,TTY,CODE,STR,SRL,SUPPRESS,CVF",
    "MRSTY.RRF": "CUI,TUI,STN,STY,ATUI,CVF",
}

MAX_LEN = 1000
MAX_CODE_LEN = 50
BATCH = 50_000


# ---------------------------------------------------------------------------
# Reading RRF files
# ---------------------------------------------------------------------------

def find_meta(path: Path) -> Path:
    hits = sorted(path.rglob("MRCONSO.RRF*")) if path.is_dir() else [path]
    if not hits:
        raise FileNotFoundError(f"no MRCONSO.RRF under {path}")
    return hits[0].parent


def release_version(path: Path) -> str:
    match = re.search(r"(\d{4}A[AB])", str(path.resolve()))
    return match.group(1) if match else "unknown"


def column_layout(meta: Path) -> dict[str, list[str]]:
    layout = {name: cols.split(",") for name, cols in DEFAULT_COLUMNS.items()}
    mrfiles = meta / "MRFILES.RRF"
    if mrfiles.exists():
        for fields in _rows(mrfiles):
            # MRFILES: FIL|DES|FMT|CLS|RWS|BTS
            layout[fields[0].rsplit("/", 1)[-1]] = fields[2].split(",")
    return layout


def _rows(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="\n") as f:
        for line in f:
            yield line.rstrip("\r\n").split("|")


def read_rrf(meta: Path, name: str, layout: dict[str, list[str]]):
    """Yield dicts for every row of `name`, across split/gzipped parts."""
    parts = sorted(p for p in meta.glob(name + "*") if p.name == name or p.name.startswith(name + "."))
    if not parts:
        return
    cols = layout[name]
    for part in parts:
        for fields in _rows(part):
            yield dict(zip(cols, fields))


def read_domains(meta: Path, layout) -> dict[str, str]:
    rank = {d: i for i, d in enumerate(DOMAIN_ORDER)}
    domains: dict[str, str] = {}
    for row in read_rrf(meta, "MRSTY.RRF", layout):
        domain = TUI_DOMAIN.get(row["TUI"], "other")
        current = domains.get(row["CUI"])
        if current is None or rank[domain] < rank[current]:
            domains[row["CUI"]] = domain
    return domains


def read_atoms(meta: Path, layout, sabs: set[str]):
    """Yield (cui, [atoms]) for English, unsuppressed atoms from the selected sources.

    MRCONSO is sorted by CUI, so atoms are grouped by consecutive CUI. If a CUI shows up
    again later (unsorted input) it is yielded again; the writers upsert, so that's safe.
    """
    group, cui = [], None
    for row in read_rrf(meta, "MRCONSO.RRF", layout):
        if row["LAT"] != "ENG" or row["SUPPRESS"] != "N" or row["SAB"] not in sabs:
            continue
        if row["CUI"] != cui and group:
            yield cui, group
            group = []
        cui = row["CUI"]
        group.append(row)
    if group:
        yield cui, group


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def term_type(atom) -> str:
    if atom["SAB"] in LAY_SABS:
        return "lay"
    if atom["TTY"] in ABBREVIATION_TTYS:
        return "abbreviation"
    if atom["SAB"] == "RXNORM" and atom["TTY"] in BRAND_TTYS:
        return "brand"
    return "clinical"


def cui_name(atoms) -> str:
    for a in atoms:
        if a["TS"] == "P" and a["STT"] == "PF" and a["ISPREF"] == "Y" and a["SAB"] == "MTH":
            return a["STR"]
    for a in atoms:
        if a["TS"] == "P" and a["STT"] == "PF" and a["ISPREF"] == "Y":
            return a["STR"]
    return atoms[0]["STR"]


def code_name(sab: str, atoms) -> str:
    order = PREFERRED_TTY.get(sab, [])
    return min(atoms, key=lambda a: order.index(a["TTY"]) if a["TTY"] in order else len(order))["STR"]


def ensure_vocabularies(db: Database, version: str, vocabs: set[str]):
    rows = [(VOCAB, "UMLS Metathesaurus", version)]
    rows += [(vid, name, version) for sab, (vid, name) in CODE_VOCABS.items() if vid in vocabs and sab not in LINK_ONLY]
    db.executemany(
        "INSERT INTO vocabulary (vocabulary_id, name, version, publisher, source_url, license, redistributable) "
        "VALUES (?, ?, ?, 'NLM (via UMLS)', 'https://www.nlm.nih.gov/research/umls/', 'UMLS license', 0) "
        "ON CONFLICT (vocabulary_id) DO UPDATE SET version = excluded.version",
        rows,
    )


def load(db: Database, path: Path, sabs: list[str] | None = None, version: str | None = None) -> dict[str, int]:
    db.init_schema()
    sabs = set(sabs or DEFAULT_SABS)
    meta = find_meta(path)
    version = version or release_version(meta)
    layout = column_layout(meta)
    domains = read_domains(meta, layout)

    created_vocabs = {CODE_VOCABS[s][0] for s in sabs if s in CODE_VOCABS and s not in LINK_ONLY}
    ensure_vocabularies(db, version, created_vocabs)

    existing: dict[tuple[str, str], int] = {}
    for vocab in created_vocabs | {VOCAB, "ICD10CM"}:
        for code, cid in db.query("SELECT code, concept_id FROM concept WHERE vocabulary_id = ?", (vocab,)):
            existing[(vocab, code)] = cid

    db.execute("DELETE FROM concept_relationship WHERE source = ?", (SOURCE,))
    db.execute("DELETE FROM concept_synonym WHERE source LIKE ?", ("UMLS/%",))

    next_id = db.next_concept_id()
    seen: set[tuple[str, str]] = set()
    concepts, synonyms, links = [], [], []
    stats = {"cuis": 0, "codes": 0, "synonyms": 0, "links": 0, "icd10cm_not_loaded": 0}

    def concept_id(vocab, code):
        nonlocal next_id
        key = (vocab, code)
        if key not in existing:
            existing[key] = next_id
            next_id += 1
        return existing[key]

    def flush(final=False):
        if len(concepts) + len(synonyms) + len(links) < BATCH and not final:
            return
        db.executemany(
            "INSERT INTO concept (concept_id, vocabulary_id, code, name, domain, concept_class, is_billable, valid_end) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, NULL) "
            "ON CONFLICT (vocabulary_id, code) DO UPDATE SET name = excluded.name, domain = excluded.domain, "
            "concept_class = excluded.concept_class, valid_end = NULL",
            concepts,
        )
        db.executemany(
            "INSERT INTO concept_synonym (concept_id, term, term_normalized, term_type, source) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (concept_id, term_normalized, term_type) DO NOTHING",
            synonyms,
        )
        db.executemany(
            "INSERT INTO concept_relationship (concept_id_1, concept_id_2, relationship_id, source) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (concept_id_1, concept_id_2, relationship_id) DO NOTHING",
            links,
        )
        stats["synonyms"] += len(synonyms)
        stats["links"] += len(links) // 2
        concepts.clear(), synonyms.clear(), links.clear()

    for cui, atoms in read_atoms(meta, layout, sabs):
        domain = domains.get(cui, "other")
        cui_id = concept_id(VOCAB, cui)
        if (VOCAB, cui) not in seen:
            seen.add((VOCAB, cui))
            stats["cuis"] += 1
        concepts.append((cui_id, VOCAB, cui, cui_name(atoms)[:MAX_LEN], domain, "cui"))

        for a in atoms:
            norm = normalize_term(a["STR"])
            if norm and len(a["STR"]) <= MAX_LEN:
                synonyms.append((cui_id, a["STR"], norm, term_type(a), f"UMLS/{a['SAB']}"))

        by_code: dict[tuple[str, str], list] = {}
        for a in atoms:
            if a["SAB"] in CODE_VOCABS and a["CODE"] and len(a["CODE"]) <= MAX_CODE_LEN:
                by_code.setdefault((a["SAB"], a["CODE"]), []).append(a)
        for (sab, code), code_atoms in by_code.items():
            vocab = CODE_VOCABS[sab][0]
            if sab in LINK_ONLY:
                code_id = existing.get((vocab, code))
                if code_id is None:
                    stats["icd10cm_not_loaded"] += 1
                    continue
            else:
                code_id = concept_id(vocab, code)
                if (vocab, code) not in seen:
                    seen.add((vocab, code))
                    stats["codes"] += 1
                    concepts.append((code_id, vocab, code, code_name(sab, code_atoms)[:MAX_LEN],
                                     VOCAB_DOMAIN.get(vocab, domain), code_atoms[0]["TTY"]))
            links.append((code_id, cui_id, "has_umls_cui", SOURCE))
            links.append((cui_id, code_id, "umls_cui_of", SOURCE))
        flush()
    flush(final=True)

    # Concepts from an earlier load that this release no longer has
    today = datetime.date.today().isoformat()
    retired = [(today, vocab, code) for (vocab, code) in existing
               if vocab in created_vocabs | {VOCAB} and (vocab, code) not in seen]
    db.executemany("UPDATE concept SET valid_end = ? WHERE vocabulary_id = ? AND code = ? AND valid_end IS NULL", retired)
    stats["retired"] = len(retired)
    db.commit()
    return stats
