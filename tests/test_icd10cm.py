import os
import shutil
import zipfile
from pathlib import Path

from conftest import FIXTURES
from medterms.loaders import icd10cm
from medterms.loaders.files import find_files

SRC = Path(FIXTURES) / "icd10cm"


def concept(db, code):
    rows = db.query(
        "SELECT concept_id, name, domain, concept_class, is_billable, valid_start, valid_end "
        "FROM concept WHERE vocabulary_id = 'ICD10CM' AND code = ?",
        (code,),
    )
    return rows[0] if rows else None


def codes_for(db, term):
    from medterms.normalize import normalize_term

    rows = db.query(
        "SELECT c.code FROM concept_synonym s JOIN concept c ON c.concept_id = s.concept_id "
        "WHERE s.term_normalized = ? ORDER BY c.code",
        (normalize_term(term),),
    )
    return [r[0] for r in rows]


def parent(db, code):
    rows = db.query(
        "SELECT p.code FROM concept c "
        "JOIN concept_relationship r ON r.concept_id_1 = c.concept_id AND r.relationship_id = 'is_a' "
        "JOIN concept p ON p.concept_id = r.concept_id_2 "
        "WHERE c.vocabulary_id = 'ICD10CM' AND c.code = ?",
        (code,),
    )
    return rows[0][0] if rows else None


def test_order_file_layout():
    release = icd10cm.Release(year=2027)
    icd10cm.parse_order_file((SRC / "icd10cm-order-2027.txt").read_text(), release)
    gad = release.codes["F41.1"]
    assert (gad.name, gad.is_billable, gad.domain) == ("Generalized anxiety disorder", True, "condition")
    assert release.codes["F41"].is_billable is False
    assert release.codes["F41"].concept_class == "category"
    assert release.codes["R45.0"].domain == "symptom"
    assert release.parents["F41.1"] == "F41"


def test_find_files_in_nested_zips(tmp_path):
    inner = tmp_path / "icd10cm-table-index-2027.zip"
    with zipfile.ZipFile(inner, "w") as zf:
        zf.write(SRC / "icd10cm-tabular-2027.xml", "Table and Index/icd10cm-tabular-2027.xml")
        zf.write(SRC / "icd10cm-index-2027.xml", "Table and Index/icd10cm-index-2027.xml")
    outer = tmp_path / "release.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.write(inner, inner.name)
        zf.write(SRC / "icd10cm-order-2027.txt", "Code Descriptions/icd10cm-order-2027.txt")
    assert set(find_files([outer], icd10cm.FILE_PATTERNS)) == {"order", "tabular", "index"}


def test_resolve_incomplete_index_codes():
    known = {"I21": 1, "I21.9": 2, "S72.0": 3}
    assert icd10cm.resolve_index_code("I21.9", known) == 2
    assert icd10cm.resolve_index_code("I21.-", known) == 1
    assert icd10cm.resolve_index_code("S72.00-", known) == 3
    assert icd10cm.resolve_index_code("Z99.9", known) is None


def test_load(db):
    stats = icd10cm.load(db, icd10cm.read_release([SRC]))
    assert stats["billable"] == 7
    assert stats["index_unresolved"] == 0

    cid, name, domain, cls, billable, start, end = concept(db, "F41.1")
    assert (name, domain, cls, billable, start, end) == ("Generalized anxiety disorder", "condition", "code", 1, None, None)
    assert concept(db, "CH05")[1].startswith("Mental, Behavioral")

    # Hierarchy: code -> category -> block -> chapter
    assert parent(db, "F41.1") == "F41"
    assert parent(db, "F41") == "F40-F48"
    assert parent(db, "F40-F48") == "CH05"

    # Preferred names, inclusion terms and index entries all resolve
    assert codes_for(db, "generalized anxiety disorder") == ["F41.1"]
    assert codes_for(db, "Anxiety state") == ["F41.1"]
    assert codes_for(db, "Anxiety, generalized") == ["F41.1"]
    assert codes_for(db, "Anxiety") == ["F41.9"]
    assert codes_for(db, "Attack, panic") == ["F41.0"]
    assert codes_for(db, "Attack, attacks, panic") == ["F41.0"]
    assert codes_for(db, "Anxiety, depression") == ["F41.8"]  # <nemod> text dropped
    assert codes_for(db, "Infarct, myocardium, unspecified site") == ["I21"]  # I21.- -> category
    assert codes_for(db, "Attack, heart") == []  # 'see' cross-references are not codes


def test_reload_is_idempotent_and_retires_dropped_codes(db, tmp_path):
    icd10cm.load(db, icd10cm.read_release([SRC]))
    gad_id = concept(db, "F41.1")[0]
    counts = db.query("SELECT (SELECT COUNT(*) FROM concept), (SELECT COUNT(*) FROM concept_synonym), "
                      "(SELECT COUNT(*) FROM concept_relationship)")[0]

    icd10cm.load(db, icd10cm.read_release([SRC]))
    assert db.query("SELECT (SELECT COUNT(*) FROM concept), (SELECT COUNT(*) FROM concept_synonym), "
                    "(SELECT COUNT(*) FROM concept_relationship)")[0] == counts

    # Next year: F41.3 dropped, F41.2 added, names of the rest unchanged
    new_dir = tmp_path / "fy2028"
    new_dir.mkdir()
    lines = (SRC / "icd10cm-order-2027.txt").read_text().splitlines()
    lines = [l for l in lines if " F413 " not in l]
    lines.append(f"{99:05d} {'F412':<7} 1 {'Mixed anxiety and depressive disorder':<60} Mixed anxiety and depressive disorder")
    (new_dir / "icd10cm-order-2028.txt").write_text("\n".join(lines) + "\n")
    shutil.copy(SRC / "icd10cm-tabular-2027.xml", new_dir / "icd10cm-tabular-2028.xml")

    stats = icd10cm.load(db, icd10cm.read_release([new_dir]))
    assert (stats["new"], stats["retired"]) == (1, 1)
    assert concept(db, "F41.1")[0] == gad_id  # ids are stable across releases
    assert str(concept(db, "F41.3")[6]) == "2027-09-30"
    assert str(concept(db, "F41.2")[5]) == "2027-10-01"
    assert concept(db, "F41.1")[6] is None
    assert db.query("SELECT version FROM vocabulary WHERE vocabulary_id = 'ICD10CM'")[0][0] == "FY2028"
