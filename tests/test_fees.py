import pytest
from conftest import FIXTURES
from medterms.loaders import fees, icd9cm

SAMPLE = f"{FIXTURES}/fees/ns_msi_sample.csv"


def test_load_fees(db):
    assert fees.load_fees(db, "NS_MSI", SAMPLE) == {"codes": 2, "fees": 3, "conflicts": 0}
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
    with pytest.raises(ValueError, match="no units, amount, anaesthesia_units or fee_note"):
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
    assert fees.load_fees(db, "ON_OHIP", csv, effective="2026-07-01") == {"codes": 1, "fees": 1, "conflicts": 0}
    assert db.query("SELECT vocabulary_id FROM concept WHERE code = 'A007A'")[0][0] == "ON_OHIP"


def test_load_extracted_ns_rows(db, tmp_path):
    """Rows as `medterms extract` writes them: one code under several sections and modifiers."""
    csv = tmp_path / "ns.csv"
    csv.write_text(
        "code,description,units,amount,anaesthesia_units,fee_note,modifier,section,category,heading,effective_start,page\n"
        "03.03,Home Visit (0800 - 1700),21.3,,,,\"LO=HOME, PT=FTPT, (RF=REFD)\",Non-Specialty Specific Services,VIST,HOME,2026-07-01,216\n"
        "03.03,Home Visit (0800 - 1700),21.3,,,units 21.3+MU,\"LO=HOME, PT=FTPT, RO=DETE, (RF=REFD)\",Non-Specialty Specific Services,VIST,HOME,2026-07-01,216\n"
        "03.03,Subsequent Visits,13,,,,\"LO=OFFC, RP=SUBS\",Anaesthesia,VIST,OFFICE,2026-07-01,221\n"
        "03.03,Subsequent Visits,16,,,,\"LO=OFFC, RP=SUBS\",Neurology,VIST,OFFICE,2026-07-01,300\n"
        "03.03,Subsequent Visits,99,,,,\"LO=OFFC, RP=SUBS\",Neurology,VIST,OFFICE,2026-07-01,301\n"
        "87.98,Delivery NEC,,,,units Time Only,AN=DFED,Anaesthesia,ANAE,DELIVERY NEC,2026-07-01,227\n"
        "39.1,Incision of palate,20,,4,anaesthesia_units 4+T,,Surgery,MISG,INCISION OF PALATE,2026-07-01,470\n"
        "C1001,Community On-Call,,150,,,,Family Practice,ADON,,2026-07-01,300\n"
    )
    assert fees.load_fees(db, "NS_MSI", csv) == {"codes": 4, "fees": 7, "conflicts": 1}
    rows = db.query(
        "SELECT f.section, f.modifier, f.units, f.fee_note FROM fee_schedule f JOIN concept c ON c.concept_id = f.concept_id "
        "WHERE c.code = '03.03' ORDER BY f.section, f.modifier")
    assert [(s, m, float(u), n) for s, m, u, n in rows] == [
        ("Anaesthesia", "LO=OFFC, RP=SUBS", 13.0, None),
        ("Neurology", "LO=OFFC, RP=SUBS", 16.0, None),  # the conflicting second row is not loaded
        ("Non-Specialty Specific Services", "LO=HOME, PT=FTPT, (RF=REFD)", 21.3, None),
        ("Non-Specialty Specific Services", "LO=HOME, PT=FTPT, RO=DETE, (RF=REFD)", 21.3, "units 21.3+MU"),
    ]
    (units, anaes, category), = db.query(
        "SELECT f.units, f.anaesthesia_units, f.category FROM fee_schedule f JOIN concept c ON c.concept_id = f.concept_id "
        "WHERE c.code = '39.1'")
    assert (float(units), float(anaes), category) == (20.0, 4.0, "MISG")
    assert db.query("SELECT units, fee_note FROM fee_schedule WHERE modifier = 'AN=DFED'") == [(None, "units Time Only")]
    # the most common description names the code; the others are searchable synonyms
    assert db.query("SELECT name FROM concept WHERE vocabulary_id = 'NS_MSI' AND code = '03.03'") == [("Subsequent Visits",)]
    assert db.query(
        "SELECT c.code FROM concept_synonym s JOIN concept c ON c.concept_id = s.concept_id "
        "WHERE s.term_normalized = 'home visit 0800 1700'") == [("03.03",)]


def test_load_units(db, tmp_path):
    csv = tmp_path / "units.csv"
    csv.write_text("unit_name,effective_start,effective_end,amount_per_unit\n"
                   "MSU,2025-04-01,2026-03-31,2.90\nMSU,2026-04-01,2027-03-31,2.96\nAU,2026-04-01,2027-03-31,27.93\n")
    assert fees.load_units(db, "NS_MSI", csv) == {"unit_values": 3}
    fees.load_units(db, "NS_MSI", csv)
    rows = db.query("SELECT unit_name, effective_start, amount_per_unit FROM unit_value ORDER BY unit_name, effective_start")
    assert [(u, str(d), float(a)) for u, d, a in rows] == [
        ("AU", "2026-04-01", 27.93), ("MSU", "2025-04-01", 2.90), ("MSU", "2026-04-01", 2.96)]
