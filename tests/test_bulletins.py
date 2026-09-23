"""Bulletin extractor tests, on lines laid out like NS MSI Physician's Bulletins (no PDF needed)."""

from test_extract import line

from medterms.extract import bulletins as b

FOOTER = "CONTACT: MSI_Assessment@medavie.bluecross.ca 902-496-7011 PHYSICIAN’S BULLETIN {} 2"

LINES = [
    # July 2026 issue: a new interim fee with its modifier and +MU on the next line
    line(2, 10, (36, "NEW INTERIM FEES")),
    line(2, 20, (36, "Effective July 17, 2026 the following health service code is available for billing:")),
    line(2, 30, (42, "Category Code Description Base")),
    line(2, 40, (42, "CONS"), (94, "03.08B"), (140, "Focused Assessment – Family Physician"), (524, "17 MSU")),
    line(2, 50, (140, "RF=REFD"), (524, "+MU")),
    line(2, 60, (140, "Description")),
    line(2, 70, (140, "A Focused Assessment is a face to face, in-person visit rendered to a patient.")),
    line(2, 900, (36, FOOTER.format("July 17, 2026"))),
    # January 2026: modifiers before the fee, specialty list after it; a placeholder code
    line(5, 10, (36, "FEE UPDATES")),
    line(5, 20, (36, "Effective January 16, 2026, the following fees apply:")),
    line(5, 30, (36, "03.03 RP=SUBS 17 MSU SP=NEUR, SP=INMD, SP=PHMD")),
    line(5, 40, (42, "DEFT"), (94, "TBD"), (140, "Advance Care Planning Discussion 15 MSU")),
    line(5, 900, (36, FOOTER.format("JANUARY 16, 2026"))),
    # July 2025: a manual-style table with dot leaders, several modifier rows for one code
    line(7, 10, (36, "FEE UPDATES")),
    line(7, 20, (36, "Effective May 23, 2025 the following codes were updated:")),
    line(7, 30, (42, "VIST"), (94, "03.04")),
    line(7, 40, (94, "LO=HOSP, FN=INPT, SP=NEUR. .................... 30")),
    line(7, 50, (94, "LO=HOSP, FN=INPT, SP=INMD ..................... 30")),
    line(7, 60, (36, "Effective January 1, 2025, the Orthopedic rota F2010 has been terminated and replaced")),
    line(7, 70, (36, "F1010 Facility On-Call Category 1 Orthopedics $350 $500 Valley Regional")),
    line(7, 900, (36, FOOTER.format("July 21, 2025"))),
    # 2022: a termination; 2021: an old/new fee-increase table without an Effective sentence date
    line(9, 10, (36, "Effective May 27, 2022, the following health service code has been terminated:")),
    line(9, 20, (42, "PSYCH"), (94, "08.5A"), (140, "Clinical Psychiatry 63.11 MSU")),
    line(9, 30, (36, "FEE CODE INCREASES")),
    line(9, 40, (36, "(New Value is the value effective April 1, 2021)")),
    line(9, 50, (36, "03.03V Medical Abortion/Termination of early pregnancy 62.63 67.03")),
    line(9, 60, (36, "Routine Psychiatric Visit (08.5B) 42.68 43.41")),       # no leading code: not a row
    line(9, 900, (36, FOOTER.format("March 19th, 2021"))),
]


def test_extract_bulletins():
    result = b.extract(LINES, b.BulletinProfile.load("ns_msi_bulletins"))
    rows = {(r["code"], r["modifier"]): r for r in result.rows}

    new = rows[("03.08B", "RF=REFD")]
    assert (new["action"], new["effective_start"], new["issue_date"], new["announcement"]) == (
        "new", "2026-07-17", "2026-07-17", "NEW INTERIM FEES")
    assert (new["units"], new["fee_note"], new["category"]) == ("17", "17 MSU +MU", "CONS")
    assert new["description"] == "Focused Assessment – Family Physician"

    visit = rows[("03.03", "RP=SUBS, SP=NEUR, SP=INMD, SP=PHMD")]
    assert (visit["units"], visit["action"], visit["issue_date"]) == ("17", "updated", "2026-01-16")

    assert {(k, r["units"]) for k, r in rows.items() if k[0] == "03.04"} == {
        (("03.04", "LO=HOSP, FN=INPT, SP=NEUR"), "30"), (("03.04", "LO=HOSP, FN=INPT, SP=INMD"), "30")}

    # the sentence terminates F2010, not the F1010 row that replaces it
    oncall = rows[("F1010", "")]
    assert (oncall["action"], oncall["amount"], oncall["effective_start"]) == ("updated", "350", "2025-01-01")
    assert oncall["fee_note"].startswith("$350 $500")

    assert rows[("08.5A", "")]["action"] == "terminated"
    increase = rows[("03.03V", "")]
    assert (increase["units"], increase["old_units"], increase["effective_start"]) == ("67.03", "62.63", "2021-04-01")

    assert [l.text for l in result.leftover] == ["DEFT TBD Advance Care Planning Discussion 15 MSU"]
    assert len(result.rows) == 7


def test_parse_date():
    assert b.parse_date("July 17, 2026") == "2026-07-17"
    assert b.parse_date("March 19th, 2021") == "2021-03-19"
    assert b.parse_date("JULY 22, 2022") == "2022-07-22"
    assert b.parse_date("Sept. 1 2023") == "2023-09-01"
    assert b.parse_date("Someday 1, 2023") is None


def test_parse_value():
    assert b.parse_value("Arthroscopic Repair (Hip) 473 MSU 4 + T") == (
        "Arthroscopic Repair (Hip)", {"units": "473", "anaesthesia_units": "4", "fee_note": "473 MSU 4 + T"})
    assert b.parse_value("Community on Call $150/night") == (
        "Community on Call", {"amount": "150", "fee_note": "$150/night"})
    assert b.parse_value("Colposcopy 11.21 12.00") == ("Colposcopy", {"units": "12.00", "old_units": "11.21"})
    assert b.parse_value("No fee here") is None


def test_write_report_lists_codes_missing_from_schedule(tmp_path):
    profile = b.BulletinProfile.load("ns_msi_bulletins")
    result = b.extract(LINES, profile)
    b.write_csv(result, tmp_path / "b.csv")
    b.write_report(result, profile, "bulletins.pdf", tmp_path / "b.md", known_codes={"03.03", "03.04"})
    report = (tmp_path / "b.md").read_text()
    assert "`03.03V`, `03.08B`, `08.5A`, `F1010`" in report
    assert (tmp_path / "b.csv").read_text().splitlines()[0] == ",".join(b.OUTPUT)
