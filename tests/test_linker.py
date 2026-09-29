"""Name-matching links from UMLS concepts to ICD-10-CM, ICD-9-CM and NS fee codes."""

from medterms import linker
from medterms.lookup import evaluate, find_codes
from medterms.normalize import normalize_term

NEXT = [1000]


def add(db, vocab, code, name, domain="condition", synonyms=(), billable=1):
    cid = NEXT[0] = NEXT[0] + 1
    db.execute("INSERT INTO concept (concept_id, vocabulary_id, code, name, domain, is_billable) VALUES (?, ?, ?, ?, ?, ?)",
               (cid, vocab, code, name, domain, billable))
    terms = {(name, "preferred" if vocab != "UMLS" else "clinical"), *synonyms}
    for term, term_type in terms:
        db.execute("INSERT INTO concept_synonym (concept_id, term, term_normalized, term_type, source) VALUES (?, ?, ?, ?, 'test')",
                   (cid, term, normalize_term(term), term_type))
    return cid


def targets(db, cui_id, vocab):
    rows = db.query(
        "SELECT t.code, r.confidence, r.source FROM concept_relationship r JOIN concept t ON t.concept_id = r.concept_id_2 "
        "WHERE r.concept_id_1 = ? AND r.relationship_id = 'maps_to_approx' AND t.vocabulary_id = ? ORDER BY r.confidence DESC, t.code",
        (cui_id, vocab))
    return [(code, float(conf), source) for code, conf, source in rows]


def build(db):
    db.init_schema()
    db.executemany("INSERT INTO vocabulary (vocabulary_id, name) VALUES (?, ?) ON CONFLICT (vocabulary_id) DO NOTHING",
                   [("UMLS", "UMLS"), ("ICD10CM", "ICD-10-CM"), ("ICD9CM", "ICD-9-CM")])
    ids = {
        "mi": add(db, "UMLS", "C0027051", "Myocardial Infarction", synonyms=[("heart attack", "lay"), ("MI", "abbreviation")]),
        "zoster": add(db, "UMLS", "C0019360", "Herpes zoster", synonyms=[("shingles", "lay")]),
        "cystitis": add(db, "UMLS", "C0600041", "Infective cystitis", synonyms=[("bladder infection", "lay")]),
        "cva": add(db, "UMLS", "C0038454", "Cerebrovascular accident", synonyms=[("stroke", "lay")]),
        "disc": add(db, "UMLS", "C0021818", "Intervertebral Disk Displacement", synonyms=[("slipped disc", "lay")]),
        "append": add(db, "UMLS", "C0003611", "Appendectomy", domain="procedure"),
    }
    add(db, "ICD10CM", "I21.9", "Acute myocardial infarction, unspecified",
        synonyms=[("Myocardial infarction (acute) NOS", "clinical")])
    add(db, "ICD10CM", "I21.A9", "Other myocardial infarction type")
    add(db, "ICD10CM", "B02.9", "Zoster without complications", synonyms=[("Herpes, zoster", "index")])
    add(db, "ICD10CM", "N30.90", "Cystitis, unspecified without hematuria", synonyms=[("Cystitis", "index")])
    add(db, "ICD10CM", "O23.10", "Infections of bladder in pregnancy, unspecified trimester")
    add(db, "ICD10CM", "V89.9", "Person injured in unspecified vehicle accident", synonyms=[("Accident", "index")])
    add(db, "ICD10CM", "M51.26", "Other intervertebral disc displacement, lumbar region",
        synonyms=[("Displacement, intervertebral disc NEC, lumbar region", "index")])
    add(db, "ICD10CM", "K35.80", "Unspecified acute appendicitis")
    i9 = add(db, "ICD9CM", "410.90", "Acute myocardial infarction of unspecified site, episode of care unspecified")
    i10 = db.query("SELECT concept_id FROM concept WHERE code = 'I21.9'")[0][0]
    db.executemany("INSERT INTO concept_relationship (concept_id_1, concept_id_2, relationship_id, source) VALUES (?, ?, ?, 'CMS_GEM')",
                   [(i9, i10, "maps_to_approx"), (i10, i9, "approx_mapped_from")])
    add(db, "NS_MSI", "59.0", "Appendectomy (when claimed with other abdominal surgery, a pathology report is required)",
        domain="procedure")
    db.commit()
    return ids


def test_name_matching(db):
    ids = build(db)
    stats = linker.link(db)
    assert stats["concepts_linked"] == 5

    # "(acute) NOS" dropped; the unspecified code beats "Other ... type", which isn't filler
    assert targets(db, ids["mi"], "ICD10CM") == [("I21.9", 0.855, "MATCH/words")]
    assert targets(db, ids["zoster"], "ICD10CM") == [("B02.9", 0.8, "MATCH/name")]  # index "Herpes, zoster"
    # disc/disk spelling, and a contained match with a region word added
    assert [t[0] for t in targets(db, ids["disc"], "ICD10CM")] == ["M51.26"]
    # broader match to a disease word; the pregnancy code is not a contained match
    assert [t[0] for t in targets(db, ids["cystitis"], "ICD10CM")] == ["N30.90"]
    # "cerebrovascular accident" is not an "accident"
    assert targets(db, ids["cva"], "ICD10CM") == []
    # procedure concepts reach procedure codes only; the ICD-10 appendicitis code is a condition
    assert [t[0] for t in targets(db, ids["append"], "NS_MSI")] == ["59.0"]
    assert targets(db, ids["append"], "ICD10CM") == []
    # through the GEM to the ICD-9-CM code NS claims use
    assert targets(db, ids["mi"], "ICD9CM") == [("410.90", 0.598, "MATCH/gem")]

    # rerunning replaces the links rather than adding to them
    count = db.query("SELECT COUNT(*) FROM concept_relationship WHERE source LIKE 'MATCH/%'")[0][0]
    linker.link(db)
    assert db.query("SELECT COUNT(*) FROM concept_relationship WHERE source LIKE 'MATCH/%'")[0][0] == count


def test_lookup_and_evaluate(db, tmp_path, capsys):
    build(db)
    linker.link(db)
    assert [m.code for m in find_codes(db, "heart attack", "ICD10CM")] == ["I21.9"]
    assert [m.code for m in find_codes(db, "heart attack", "ICD9CM")] == ["410.90"]
    assert find_codes(db, "heart attack", "ICD9CM")[0].confidence == 0.598

    terms = tmp_path / "terms.csv"
    terms.write_text("term,icd10,icd9,ns\n# comment row\nheart attack,I21,410,\nshingles,B02,,\n"
                     "stroke,I63,,\nappendectomy,,,*\n")
    summary = evaluate(db, terms)
    assert summary == {"icd10": 2 / 3, "icd9": 1.0, "ns": 1.0}
    assert "misses: stroke" in capsys.readouterr().out


def test_word_key():
    assert linker.word_key("Myocardial infarction (acute) NOS") == {"myocardial", "infarction"}
    assert linker.word_key("Adenoidectomy without tonsillectomy") == {"adenoidectomy"}
    assert linker.word_key("Haemorrhage of intervertebral disks") == {"hemorrhage", "intervertebral", "disc"}
    assert linker.word_key("Other myocardial infarction type") == {"other", "myocardial", "infarction", "type"}
    assert linker.word_key("NOS") is None
