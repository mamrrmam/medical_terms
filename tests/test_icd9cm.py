from pathlib import Path

from conftest import FIXTURES
from medterms.loaders import icd9cm, icd10cm

ICD9 = Path(FIXTURES) / "icd9cm"
ICD10 = Path(FIXTURES) / "icd10cm"


def test_dotted():
    assert icd9cm.dotted("0010") == "001.0"
    assert icd9cm.dotted("30002") == "300.02"
    assert icd9cm.dotted("V700") == "V70.0"
    assert icd9cm.dotted("E8000") == "E800.0"
    assert icd9cm.dotted("493") == "493"


def test_parse_descriptions_rejects_junk():
    codes, rejected = icd9cm.parse_descriptions("CODE DESCRIPTION\n30002 Generalized anxiety disorder  \n\n")
    assert codes == {"300.02": "Generalized anxiety disorder"}
    assert rejected == 1


def gem_targets(db, icd9_code):
    return db.query(
        "SELECT t.code, r.relationship_id FROM concept s "
        "JOIN concept_relationship r ON r.concept_id_1 = s.concept_id AND r.source = 'CMS_GEM' "
        "JOIN concept t ON t.concept_id = r.concept_id_2 "
        "WHERE s.vocabulary_id = 'ICD9CM' AND s.code = ?", (icd9_code,))


def test_load_with_gems(db):
    icd10cm.load(db, icd10cm.read_release([ICD10]))
    stats = icd9cm.load(db, [ICD9])
    assert stats == {"codes": 7, "rejected_lines": 0, "maps_to": 4, "maps_to_approx": 1,
                     "no_map": 1, "icd10cm_not_loaded": 1}  # Z00.00 isn't in the ICD-10-CM fixture

    name, domain = db.query("SELECT name, domain FROM concept WHERE vocabulary_id = 'ICD9CM' AND code = '300.02'")[0]
    assert (name, domain) == ("Generalized anxiety disorder", "condition")
    assert gem_targets(db, "300.02") == [("F41.1", "maps_to")]
    assert gem_targets(db, "410.90") == [("I21.9", "maps_to_approx")]
    assert db.query("SELECT version FROM vocabulary WHERE vocabulary_id = 'ICD9CM'")[0][0] == "v32"

    # Nova Scotia's payer row points at ICD-9-CM for claim diagnoses
    assert db.query("SELECT diagnosis_vocabulary_id, unit_name FROM payer WHERE payer_id = 'NS_MSI'")[0] == ("ICD9CM", "MSU")

    # Reload is idempotent
    before = db.query("SELECT COUNT(*) FROM concept_relationship")[0][0]
    icd9cm.load(db, [ICD9])
    assert db.query("SELECT COUNT(*) FROM concept_relationship")[0][0] == before
