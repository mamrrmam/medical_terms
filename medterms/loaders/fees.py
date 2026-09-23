"""Load a payer's fee schedule, or a payer-specific code list, from a normalized CSV.

Provinces publish fee schedules in different shapes (PDF manuals, vendor "fee master"
files, spreadsheets). Each gets converted to one of these CSV layouts first, so the
database side is the same for every payer.

Fee schedule CSV (`medterms load fees --payer ON_OHIP fees.csv`):

  code             fee code as the payer prints it: A007A, 03.03, 00100     (required)
  description      the payer's description                                  (required)
  units            fee in the payer's units (e.g. NS MSU)                   (one of units, amount,
  amount           fee in dollars                                            anaesthesia_units
  anaesthesia_units  NS base anaesthetic units                               or fee_note)
  fee_note         fee as printed when not a plain number ('units 62+MU')
  modifier         '' for the base fee, else e.g. RO=HDIN                   (optional)
  section          schedule section / specialty, '' for the whole schedule  (optional)
  category         payer's service category, e.g. NS VIST                   (optional)
  heading          group heading the row is printed under                   (optional)
  locality         '' for the whole jurisdiction                            (optional)
  effective_start  YYYY-MM-DD                                               (required, or --effective)
  effective_end    YYYY-MM-DD                                               (optional)

`medterms extract` writes this layout from PDF schedules. Codes become concepts in the
payer's fee vocabulary (payer.fee_vocabulary_id), named with their most common description;
every other description becomes a 'clinical' synonym so lookups find it. Each row becomes a
fee_schedule row keyed by code, section, modifier, locality and effective_start; when two rows
share a key but differ, the first is kept and the rest are counted as `conflicts`.

Unit value CSV (`medterms load units --payer NS_MSI units.csv`): unit_name (MSU, AU),
effective_start, effective_end, amount_per_unit.

Code list CSV (`medterms load codes --vocabulary ON_OHIP_DX dx.csv`), for lists such as
Ontario's 3-digit OHIP diagnostic codes:

  code, description             (required)
  maps_to                       code in --maps-to-vocabulary this corresponds to (optional)

`maps_to` links are stored as 'maps_to_approx', since payer lists rarely match a
standard code set one to one.
"""

import csv
import datetime
from collections import Counter
from pathlib import Path

from medterms.db import Database
from medterms.normalize import normalize_term

MAX_LEN = 1000


def _read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k.strip().lower(): (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(f)]


def _date(value: str, field: str, line: int) -> str | None:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f"line {line}: {field} {value!r} is not YYYY-MM-DD") from None


def _number(value: str, field: str, line: int) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace("$", "").replace(",", ""))
    except ValueError:
        raise ValueError(f"line {line}: {field} {value!r} is not a number") from None


def upsert_codes(db: Database, vocabulary_id: str, codes: dict[str, str], domain: str, concept_class: str,
                 source: str, synonyms: dict[str, set[str]] | None = None) -> dict[str, int]:
    """Create or rename concepts for {code: description}, with optional extra 'clinical'
    names per code; return {code: concept_id}."""
    existing = dict(db.query("SELECT code, concept_id FROM concept WHERE vocabulary_id = ?", (vocabulary_id,)))
    next_id = db.next_concept_id()
    ids, rows = {}, []
    for code, name in codes.items():
        cid = existing.get(code)
        if cid is None:
            cid, next_id = next_id, next_id + 1
        ids[code] = cid
        rows.append((cid, vocabulary_id, code, name[:MAX_LEN], domain, concept_class))
    db.executemany(
        "INSERT INTO concept (concept_id, vocabulary_id, code, name, domain, concept_class, is_billable) "
        "VALUES (?, ?, ?, ?, ?, ?, 1) "
        "ON CONFLICT (vocabulary_id, code) DO UPDATE SET name = excluded.name, valid_end = NULL",
        rows,
    )
    db.execute("DELETE FROM concept_synonym WHERE source = ?", (source,))
    terms = {}
    for code, name in codes.items():
        for term, term_type in [(name, "preferred"), *((s, "clinical") for s in (synonyms or {}).get(code, ()))]:
            norm = normalize_term(term)
            if norm and (term_type == "preferred" or norm != normalize_term(name)):
                terms.setdefault((ids[code], norm, term_type), (ids[code], term[:MAX_LEN], norm[:MAX_LEN], term_type, source))
    db.executemany(
        "INSERT INTO concept_synonym (concept_id, term, term_normalized, term_type, source) VALUES (?, ?, ?, ?, ?)",
        list(terms.values()),
    )
    return ids


def load_fees(db: Database, payer_id: str, path: Path, effective: str | None = None) -> dict[str, int]:
    db.init_schema()
    payer = db.query("SELECT fee_vocabulary_id FROM payer WHERE payer_id = ?", (payer_id,))
    if not payer or not payer[0][0]:
        known = ", ".join(p for (p,) in db.query("SELECT payer_id FROM payer ORDER BY payer_id"))
        raise ValueError(f"unknown payer {payer_id!r} (or it has no fee vocabulary); known payers: {known}")
    vocabulary_id = payer[0][0]

    rows = _read_csv(path)
    missing = {"code", "description"} - set(rows[0] if rows else {})
    if missing:
        raise ValueError(f"{path}: missing column(s) {', '.join(sorted(missing))}")

    names: dict[str, Counter] = {}
    fees: dict[tuple, tuple] = {}
    conflicts = 0
    for line, row in enumerate(rows, start=2):
        code = row["code"]
        if not code:
            continue
        names.setdefault(code, Counter())[row["description"]] += 1
        units = _number(row.get("units", ""), "units", line)
        amount = _number(row.get("amount", ""), "amount", line)
        anaesthesia = _number(row.get("anaesthesia_units", ""), "anaesthesia_units", line)
        note = row.get("fee_note", "")
        start = _date(row.get("effective_start", ""), "effective_start", line) or effective
        if start is None:
            raise ValueError(f"line {line}: no effective_start and no --effective given")
        if units is None and amount is None and anaesthesia is None and not note:
            raise ValueError(f"line {line}: {code} has no units, amount, anaesthesia_units or fee_note")
        key = (code, row.get("section", ""), row.get("modifier", ""), row.get("locality", ""), start)
        value = (_date(row.get("effective_end", ""), "effective_end", line), row.get("category") or None,
                 row.get("heading") or None, row["description"][:MAX_LEN] or None, units, amount, anaesthesia,
                 note[:255] or None)
        if key in fees:
            conflicts += fees[key][4:] != value[4:]
            continue
        fees[key] = value

    codes = {code: counts.most_common(1)[0][0] for code, counts in names.items()}
    ids = upsert_codes(db, vocabulary_id, codes, "procedure", "fee_code", f"{payer_id}_FEES",
                       synonyms={code: set(counts) for code, counts in names.items()})
    db.executemany(
        "INSERT INTO fee_schedule (payer_id, concept_id, section, modifier, locality, effective_start, effective_end, "
        "category, heading, description, units, amount, anaesthesia_units, fee_note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (payer_id, concept_id, section, modifier, locality, effective_start) DO UPDATE SET "
        "effective_end = excluded.effective_end, category = excluded.category, heading = excluded.heading, "
        "description = excluded.description, units = excluded.units, amount = excluded.amount, "
        "anaesthesia_units = excluded.anaesthesia_units, fee_note = excluded.fee_note",
        [(payer_id, ids[code], section, modifier, locality, start, *value)
         for (code, section, modifier, locality, start), value in fees.items()],
    )
    db.commit()
    return {"codes": len(codes), "fees": len(fees), "conflicts": conflicts}


def load_units(db: Database, payer_id: str, path: Path) -> dict[str, int]:
    """Dollar value of a payer's fee units over time (e.g. NS MSU and AU)."""
    db.init_schema()
    if not db.query("SELECT 1 FROM payer WHERE payer_id = ?", (payer_id,)):
        raise ValueError(f"unknown payer {payer_id!r}")
    rows = []
    for line, row in enumerate(_read_csv(path), start=2):
        start = _date(row.get("effective_start", ""), "effective_start", line)
        amount = _number(row.get("amount_per_unit", ""), "amount_per_unit", line)
        if start is None or amount is None:
            raise ValueError(f"line {line}: effective_start and amount_per_unit are required")
        rows.append((payer_id, row.get("unit_name", ""), start, _date(row.get("effective_end", ""), "effective_end", line),
                     amount))
    db.executemany(
        "INSERT INTO unit_value (payer_id, unit_name, effective_start, effective_end, amount_per_unit) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (payer_id, unit_name, effective_start) DO UPDATE SET "
        "effective_end = excluded.effective_end, amount_per_unit = excluded.amount_per_unit",
        rows,
    )
    db.commit()
    return {"unit_values": len(rows)}


def load_codes(db: Database, vocabulary_id: str, path: Path, maps_to_vocabulary: str | None = None,
               domain: str = "condition") -> dict[str, int]:
    db.init_schema()
    if not db.query("SELECT 1 FROM vocabulary WHERE vocabulary_id = ?", (vocabulary_id,)):
        raise ValueError(f"unknown vocabulary {vocabulary_id!r}; add it to the vocabulary table first")
    rows = _read_csv(path)
    codes = {r["code"]: r["description"] for r in rows if r.get("code")}
    source = f"{vocabulary_id}_LIST"
    ids = upsert_codes(db, vocabulary_id, codes, domain, "code", source)

    stats = {"codes": len(codes), "maps_to": 0, "target_not_loaded": 0}
    if maps_to_vocabulary:
        targets = dict(db.query("SELECT code, concept_id FROM concept WHERE vocabulary_id = ?", (maps_to_vocabulary,)))
        db.execute("DELETE FROM concept_relationship WHERE source = ?", (source,))
        links = {}
        for r in rows:
            dst = targets.get(r.get("maps_to", ""))
            if r.get("maps_to") and dst is None:
                stats["target_not_loaded"] += 1
            if r.get("code") and dst is not None:
                src = ids[r["code"]]
                links[(src, dst)] = (src, dst, "maps_to_approx", source)
                links[(dst, src)] = (dst, src, "approx_mapped_from", source)
        db.executemany(
            "INSERT INTO concept_relationship (concept_id_1, concept_id_2, relationship_id, source) VALUES (?, ?, ?, ?)",
            list(links.values()),
        )
        stats["maps_to"] = len(links) // 2
    db.commit()
    return stats
