import gzip
import shutil
from pathlib import Path

import pytest
from conftest import FIXTURES
from medterms.loaders import icd10cm, umls
from medterms.normalize import normalize_term

UMLS = Path(FIXTURES) / "umls"
ICD = Path(FIXTURES) / "icd10cm"


def to_icd10cm(db, term):
    """Lay or clinical term -> ICD-10-CM codes through the UMLS concept."""
    rows = db.query(
        "SELECT DISTINCT icd.code FROM concept_synonym s "
        "JOIN concept cui ON cui.concept_id = s.concept_id AND cui.vocabulary_id = 'UMLS' "
        "JOIN concept_relationship r ON r.concept_id_1 = cui.concept_id AND r.relationship_id = 'umls_cui_of' "
        "JOIN concept icd ON icd.concept_id = r.concept_id_2 AND icd.vocabulary_id = 'ICD10CM' "
        "WHERE s.term_normalized = ? ORDER BY icd.code",
        (normalize_term(term),),
    )
    return [r[0] for r in rows]


def concept(db, vocab, code):
    rows = db.query("SELECT concept_id, name, domain, valid_end FROM concept WHERE vocabulary_id = ? AND code = ?",
                    (vocab, code))
    return rows[0] if rows else None


def synonyms(db, vocab, code):
    return sorted(db.query(
        "SELECT s.term, s.term_type FROM concept_synonym s JOIN concept c ON c.concept_id = s.concept_id "
        "WHERE c.vocabulary_id = ? AND c.code = ?", (vocab, code)))


@pytest.fixture
def loaded(db):
    icd10cm.load(db, icd10cm.read_release([ICD]))
    stats = umls.load(db, UMLS)
    return db, stats


def test_concepts_and_codes(loaded):
    db, stats = loaded
    assert stats["cuis"] == 5
    assert stats["icd10cm_not_loaded"] == 1  # Z99.99 isn't in the ICD-10-CM fixture
    assert db.query("SELECT version FROM vocabulary WHERE vocabulary_id = 'UMLS'")[0][0] == "2026AA"

    assert concept(db, "UMLS", "C0003467")[1:3] == ("Anxiety", "condition")  # T048 beats T184
    assert concept(db, "UMLS", "C0000970")[2] == "drug"
    assert concept(db, "SNOMED", "48694002")[1] == "Anxiety"  # PT preferred over FN
    assert concept(db, "RXNORM", "161")[1:3] == ("acetaminophen", "drug")
    assert concept(db, "MESH", "D001007")[1] == "Anxiety"
    assert concept(db, "ICD10CM", "F41.1")[1] == "Generalized anxiety disorder"  # not renamed
    assert concept(db, "ICD10CM", "Z99.99") is None  # never created


def test_synonym_types(loaded):
    db, _ = loaded
    terms = synonyms(db, "UMLS", "C0003467")
    assert ("feeling anxious", "lay") in terms
    assert ("Anxiety (finding)", "clinical") in terms
    assert not any(t in ("Angst", "Anxiété") for t, _ in terms)  # suppressed / non-English skipped
    assert ("GAD", "abbreviation") in synonyms(db, "UMLS", "C0270549")
    assert ("Tylenol", "brand") in synonyms(db, "UMLS", "C0000970")


def test_lay_terms_reach_icd10cm(loaded):
    db, _ = loaded
    assert to_icd10cm(db, "heart attack") == ["I21.9"]
    assert to_icd10cm(db, "constant worrying") == ["F41.1"]
    assert to_icd10cm(db, "GAD") == ["F41.1"]


def test_snomed_to_icd10cm_via_cui(loaded):
    db, _ = loaded
    rows = db.query(
        "SELECT icd.code FROM concept sct "
        "JOIN concept_relationship a ON a.concept_id_1 = sct.concept_id AND a.relationship_id = 'has_umls_cui' "
        "JOIN concept_relationship b ON b.concept_id_1 = a.concept_id_2 AND b.relationship_id = 'umls_cui_of' "
        "JOIN concept icd ON icd.concept_id = b.concept_id_2 AND icd.vocabulary_id = 'ICD10CM' "
        "WHERE sct.vocabulary_id = 'SNOMED' AND sct.code = '21897009'")
    assert [r[0] for r in rows] == ["F41.1"]


def test_reload_keeps_ids_and_retires(loaded, tmp_path):
    db, _ = loaded
    cid = concept(db, "UMLS", "C0270549")[0]
    count = db.query("SELECT COUNT(*) FROM concept_synonym")[0][0]

    # Next release: gzipped, split in two, acetaminophen dropped
    meta = tmp_path / "2026AB" / "META"
    meta.mkdir(parents=True)
    lines = [l for l in (UMLS / "2026AA/META/MRCONSO.RRF").read_text().splitlines(keepends=True) if "C0000970" not in l]
    for suffix, part in (("aa", lines[:5]), ("ab", lines[5:])):
        with gzip.open(meta / f"MRCONSO.RRF.{suffix}.gz", "wt") as f:
            f.writelines(part)
    shutil.copy(UMLS / "2026AA/META/MRSTY.RRF", meta)

    stats = umls.load(db, tmp_path)
    assert stats["retired"] == 3  # CUI + RxNorm IN + BN
    assert concept(db, "UMLS", "C0270549")[0] == cid
    assert concept(db, "UMLS", "C0000970")[3] is not None
    assert concept(db, "UMLS", "C0003467")[3] is None
    assert db.query("SELECT COUNT(*) FROM concept_synonym")[0][0] == count - 3
    assert db.query("SELECT version FROM vocabulary WHERE vocabulary_id = 'UMLS'")[0][0] == "2026AB"
