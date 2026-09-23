"""PDF extractor tests, on positioned lines laid out like the NS Physician's Manual (no PDF needed)."""

from medterms.extract import fees as ex
from medterms.extract.pdftext import Line, Word, parse_pages

FOOTER = "Section 8: NS MSI Schedule of Benefits – {} - {}"


def line(page, top, *parts):
    """parts: (x0, text) pairs; each text is split into words laid out from x0."""
    words = []
    for x0, text in parts:
        x = x0
        for token in text.split(" "):
            words.append(Word(x, x + 5 * len(token), token))
            x += 5 * len(token) + 3
    return Line(page, top, words)


def row(page, top, category, code, description, units=None, anaes=None):
    parts = [(57, category), (106, code), (149, description + " ....")]
    if units:
        parts.append((488, units))
    if anaes:
        parts.append((542, anaes))
    return line(page, top, *parts)


def mod(page, top, text, units=None, anaes=None):
    parts = [(149, text + " ....")]
    if units:
        parts.append((488, units))
    if anaes:
        parts.append((542, anaes))
    return line(page, top, *parts)


LINES = [
    # page 1: non-specialty services
    line(1, 10, (364, "PHYSICIAN’S MANUAL 2026")),
    line(1, 20, (57, "HOME")),
    line(1, 30, (57, "VIST"), (106, "03.04"), (149, "Initial Visit")),
    mod(1, 40, "LO=HOME (RF=REFD)", "23"),
    mod(1, 50, "LO=HOME, RO=DETE (RF=REFD)", "23+MU"),
    line(1, 60, (57, "VIST"), (106, "03.03"), (149, "Detox Centre (0801 - 1200) Sat., Sun., Holidays")),
    line(1, 70, (149, "LO=HOSP, FN=DTOX, DA=RGE1, PT=FTPT, TI=AMNN, US=UNOF")),   # modifiers wrap...
    mod(1, 80, "(RF=REFD)", "28.3"),                                             # ...onto the fee line
    line(1, 90, (57, "VIST"), (106, "03.03C"), (149, "Palliative Care Support Visit")),
    mod(1, 100, "RO=PCSV", "30 per 30 min"),
    line(1, 110, (149, "(15 units per 15 min. thereafter)")),                    # note on the modifier row
    line(1, 120, (57, "The service date of electronic claims should be the date the patient had the")),
    line(1, 900, (219, FOOTER.format("Non-Specialty Specific Services", 1))),
    # page 2: anaesthesia section
    line(2, 20, (263, "ANAESTHESIA")),
    line(2, 30, (282, "(SP=ANAE)")),
    line(2, 40, (57, "DELIVERY NEC")),
    row(2, 50, "ANAE", "87.98", "Delivery NEC", anaes="4+T"),
    mod(2, 60, "AN=DFED", anaes="Time Only"),
    line(2, 70, (57, "SURGICAL PROCEDURES NOS")),
    row(2, 80, "ADON", "99.09A", "Morbid obesity surgical add on", "32.9", "4.6"),
    row(2, 90, "ADON", "AHSP1", "After Hours Service Premium", "35%"),
    line(2, 100, (57, "OTHER REPAIR AND PLASTIC OPERATIONS ON TRACHEA AND")),     # heading wraps
    line(2, 110, (57, "LARYNX")),
    line(2, 120, (57, "MASG"), (106, "39.39C"), (149, "Local excision for carcinoma of floor of mouth,")),
    line(2, 130, (149, "buccal mucosa ...."), (488, "100"), (542, "6+T")),        # fee on the wrapped line
    row(2, 140, "ADON", "C1001", "Community On-Call", "$150"),
    line(2, 150, (57, "VIST"), (106, "03.99"), (149, "Service with no fee printed")),
    mod(2, 160, "SP= GENP, US-PR50", "15"),                                       # typos normalised
    line(2, 900, (302, FOOTER.format("Anaesthesia", 6))),
]

PROFILE_TOML = r"""
payer = "NS_MSI"
effective_start = "2026-07-01"
record = '(?:(?P<category>[A-Z]{4})\s+(?P<code>[A-Z0-9][A-Z0-9.]*)|R\d+)(?=\s|$)'
code_max_x = 70
modifier = '\(?[A-Z]{2}\s?[=-]\s?[A-Z0-9]{4}\*?\)?'
heading_max_x = 70
heading = "[A-Z(][A-Z0-9 ,&/()'’.:=+\\-–]{2,}"
page_section = 'Section 8: NS MSI Schedule of Benefits\s*[–-]\s*(?P<section>.+?)\s*-\s*\d+$'
skip = ['^PHYSICIAN’S MANUAL 2026$', '^(\(SP=[A-Z]{4}\)\s*)+$']

[columns]
units = [480, 536]
anaesthesia_units = [537, 600]
"""


def profile(tmp_path):
    path = tmp_path / "ns.toml"
    path.write_text(PROFILE_TOML, encoding="utf-8")
    return ex.Profile.load(path)


def rows_by(result):
    return {(r["code"], r["modifier"]): r for r in result.rows}


def test_extract_ns_layout(tmp_path):
    result = ex.extract(LINES, profile(tmp_path))
    rows = rows_by(result)

    assert rows[("03.04", "LO=HOME, (RF=REFD)")]["units"] == "23"
    detention = rows[("03.04", "LO=HOME, RO=DETE, (RF=REFD)")]
    assert (detention["units"], detention["fee_note"]) == ("23", "units 23+MU")
    assert (detention["section"], detention["category"], detention["heading"]) == (
        "Non-Specialty Specific Services", "VIST", "HOME")

    wrapped = rows[("03.03", "LO=HOSP, FN=DTOX, DA=RGE1, PT=FTPT, TI=AMNN, US=UNOF, (RF=REFD)")]
    assert wrapped["units"] == "28.3"

    palliative = rows[("03.03C", "RO=PCSV")]
    assert (palliative["units"], palliative["fee_note"]) == ("30", "units 30 per 30 min")
    assert palliative["description"] == "Palliative Care Support Visit (15 units per 15 min. thereafter)"

    delivery, time_only = rows[("87.98", "")], rows[("87.98", "AN=DFED")]
    assert (delivery["anaesthesia_units"], delivery["fee_note"], delivery["section"]) == (
        "4", "anaesthesia_units 4+T", "Anaesthesia")
    assert (time_only["anaesthesia_units"], time_only["fee_note"]) == ("", "anaesthesia_units Time Only")

    assert (rows[("99.09A", "")]["units"], rows[("99.09A", "")]["anaesthesia_units"]) == ("32.9", "4.6")
    assert (rows[("AHSP1", "")]["units"], rows[("AHSP1", "")]["fee_note"]) == ("", "units 35%")
    assert (rows[("C1001", "")]["amount"], rows[("C1001", "")]["units"]) == ("150", "")

    excision = rows[("39.39C", "")]
    assert excision["description"] == "Local excision for carcinoma of floor of mouth, buccal mucosa"
    assert (excision["units"], excision["anaesthesia_units"]) == ("100", "6")
    assert excision["heading"] == "OTHER REPAIR AND PLASTIC OPERATIONS ON TRACHEA AND LARYNX"

    assert rows[("03.99", "SP=GENP, US=PR50")]["units"] == "15"
    assert all(r["effective_start"] == "2026-07-01" for r in result.rows)

    # the centred section title and the prose line are reported, not silently dropped
    assert [l.text for l in result.leftover] == [
        "The service date of electronic claims should be the date the patient had the"]
    assert result.no_fee == [] and result.duplicates == []


def test_records_without_fee_and_conflicts_are_reported(tmp_path):
    lines = [
        row(1, 10, "VIST", "03.03", "Subsequent Visits", "13"),
        row(1, 20, "VIST", "03.03", "Subsequent Visits", "16"),
        line(1, 30, (57, "VIST"), (106, "03.04"), (149, "No fee anywhere")),
    ]
    result = ex.extract(lines, profile(tmp_path))
    assert [r.code for r in result.no_fee] == ["03.04"]
    assert result.duplicates == [("03.03", "", "")]


def test_prose_reaching_the_fee_columns_is_not_a_fee(tmp_path):
    lines = [
        line(1, 10, (57, "VIST"), (106, "03.03"), (149, "Visit")),
        line(1, 20, (149, "Note: claimable once per day when the physician attends"), (488, "the"), (542, "patient")),
        mod(1, 30, "LO=OFFC", "13"),
    ]
    result = ex.extract(lines, profile(tmp_path))
    assert [(r["modifier"], r["units"]) for r in result.rows] == [("LO=OFFC", "13")]
    assert result.rows[0]["description"] == "Visit Note: claimable once per day when the physician attends the patient"


def test_extract_code_list(tmp_path):
    path = tmp_path / "mods.toml"
    path.write_text("""
payer = "NS_MSI"
kind = "codes"
record = '(?P<type>[A-Z]{2})\\s+(?P<value>[A-Z0-9]{4})(?=\\s)'
code_template = "{type}={value}"
code_max_x = 80
skip = ['^Type Value Description$']
""")
    lines = [
        line(1, 10, (74, "Type Value Description")),
        line(1, 20, (74, "AG"), (109, "OV65"), (151, "Person 65 years and older")),
        line(1, 30, (74, "DA"), (109, "DA23"), (151, "Second or third date of admission")),
        line(1, 40, (151, "or day out of ICU")),
        line(1, 50, (57, "Section 7: Appendix H - 146")),
    ]
    result = ex.extract(lines, ex.Profile.load(path))
    assert [(r["code"], r["description"]) for r in result.rows] == [
        ("AG=OV65", "Person 65 years and older"),
        ("DA=DA23", "Second or third date of admission or day out of ICU"),
    ]
    assert len(result.leftover) == 1


def test_write_csv_and_report(tmp_path):
    p = profile(tmp_path)
    result = ex.extract(LINES, p)
    ex.write_csv(result, tmp_path / "out.csv", p)
    ex.write_report(result, p, "manual.pdf", tmp_path / "out.md")
    header = (tmp_path / "out.csv").read_text().splitlines()[0]
    assert header == ",".join(ex.FEE_OUTPUT)
    assert "rows written: 11 (9 codes)" in (tmp_path / "out.md").read_text()


def test_parse_fee():
    assert ex.parse_fee("units", "62+MU") == {"units": "62", "fee_note": "units 62+MU"}
    assert ex.parse_fee("units", "1,234.50") == {"units": "1234.50"}
    assert ex.parse_fee("units", "$150") == {"amount": "150"}
    assert ex.parse_fee("units", "35%") == {"fee_note": "units 35%"}
    assert ex.parse_fee("anaesthesia_units", "Time Only") == {"fee_note": "anaesthesia_units Time Only"}


def test_bundled_ns_profiles_load():
    fees = ex.Profile.load("ns_msi_fees")
    assert fees.payer == "NS_MSI" and fees.kind == "fees" and set(fees.columns) == {"units", "anaesthesia_units"}
    assert ex.Profile.load("ns_msi_modifiers").kind == "codes"
    assert ex.Profile.load("ns_msi_explanatory").kind == "codes"


def test_parse_pages():
    assert parse_pages("3-5,9", 20) == [3, 4, 5, 9]
    assert parse_pages("18-", 20) == [18, 19, 20]
    assert parse_pages(None, 3) == [1, 2, 3]
