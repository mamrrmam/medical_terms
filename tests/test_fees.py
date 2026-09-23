import pytest
from conftest import FIXTURES
from medterms.loaders import fees, icd9cm

SAMPLE = f"{FIXTURES}/fees/ns_msi_sample.csv"


def test_load_fees(db):
    assert fees.load_fees(db, "NS_MSI", SAMPLE) == {"codes": 2, "fees": 3}
    rows = db.query(
        "SELECT c.code, f.modifier, f.units, f.effective_start FROM fee_schedule f "
        "JOIN concept c ON c.concept_id = f.concept_id WHERE f.payer_id = 'NS_MSI' ORDER BY c.code, f.modifier")
    assert [(c, m, float(u), str(d)) for c, m, u, d in rows] == [
        ("03.03", "", 10.0, "2026-04-01"),
        ("03.03", "RO=HDIN", 12.5, "2026-04-01"),
        ("03.04", "", 25.0, "2026-04-01"),
    ]
    assert db.query("SELECT vocabulary_id, domain FROM concept WHERE code = '03.04'")[0] == ("NS_MSI", "procedure")

    # Reloading updates in place
    fees.load_fees(db, "NS_MSI", SAMPLE)
    assert db.query("SELECT COUNT(*) FROM fee_schedule")[0][0] == 3


def test_load_fees_validates(db, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("code,description,units\n03.03,Visit,\n")
    with pytest.raises(ValueError, match="neither units nor amount"):
        fees.load_fees(db, "NS_MSI", bad, effective="2026-04-01")
    bad.write_text("code,description,units\n03.03,Visit,10\n")
    with pytest.raises(ValueError, match="no effective_start"):
        fees.load_fees(db, "NS_MSI", bad)
    with pytest.raises(ValueError, match="unknown payer"):
        fees.load_fees(db, "XX_NONE", bad)


def test_load_code_list_with_mapping(db, tmp_path):
    from pathlib import Path
    icd9cm.load(db, [Path(FIXTURES) / "icd9cm"])
    db.execute("INSERT INTO vocabulary (vocabulary_id, name) VALUES ('TEST_DX', 'Test payer diagnostic codes')")
    dx = tmp_path / "dx.csv"
    dx.write_text("code,description,maps_to\n300,Anxiety neurosis (sample),300.00\n999,Unmapped (sample),999.99\n")
    stats = fees.load_codes(db, "TEST_DX", dx, maps_to_vocabulary="ICD9CM")
    assert stats == {"codes": 2, "maps_to": 1, "target_not_loaded": 1}
    rows = db.query(
        "SELECT t.code FROM concept s JOIN concept_relationship r ON r.concept_id_1 = s.concept_id "
        "AND r.relationship_id = 'maps_to_approx' JOIN concept t ON t.concept_id = r.concept_id_2 "
        "WHERE s.vocabulary_id = 'TEST_DX' AND s.code = '300'")
    assert rows == [("300.00",)]


def test_load_fees_for_another_province(db, tmp_path):
    csv = tmp_path / "on.csv"
    csv.write_text("code,description,amount\nA007A,Intermediate assessment (sample),10.00\n")
    assert fees.load_fees(db, "ON_OHIP", csv, effective="2026-07-01") == {"codes": 1, "fees": 1}
    assert db.query("SELECT vocabulary_id FROM concept WHERE code = 'A007A'")[0][0] == "ON_OHIP"
