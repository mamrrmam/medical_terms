"""Annotating running text: dictionary matching, filters, candidates and codes."""

import pytest
from test_linker import add

from medterms.annotate import Annotator, evaluate


def link(db, src, dst, confidence=None):
    db.execute("INSERT INTO concept_relationship (concept_id_1, concept_id_2, relationship_id, confidence, source) "
               "VALUES (?, ?, 'maps_to_approx', ?, 'test')", (src, dst, confidence))


@pytest.fixture
def annotator(db):
    db.init_schema()
    db.executemany("INSERT INTO vocabulary (vocabulary_id, name) VALUES (?, ?) ON CONFLICT (vocabulary_id) DO NOTHING",
                   [("UMLS", "UMLS"), ("ICD10CM", "ICD-10-CM"), ("ICD9CM", "ICD-9-CM")])
    mi = add(db, "UMLS", "C0027051", "Myocardial Infarction",
             synonyms=[("heart attack", "lay"), ("MI", "abbreviation")])
    mot = add(db, "UMLS", "C0870544", "Motivational Interviewing", domain="procedure",
              synonyms=[("MI", "abbreviation")])
    cp = add(db, "UMLS", "C0008031", "Chest Pain", domain="symptom")
    dia = add(db, "UMLS", "C0011991", "Diarrhea", domain="symptom")
    vom = add(db, "UMLS", "C0042963", "Vomiting", domain="symptom")
    combo = add(db, "UMLS", "C0687713", "diarrhea and vomiting", domain="symptom")
    add(db, "UMLS", "C0030705", "Patient", domain="other")                         # outside the domains
    add(db, "UMLS", "C1522541", "Protocol Treatment Arm", domain="procedure",      # reaches no code
        synonyms=[("arm", "clinical")])
    add(db, "UMLS", "C9999001", "But (abbreviation)", synonyms=[("but", "clinical")])  # a stopword
    add(db, "UMLS", "C0018681", "Headache", domain="symptom")
    i21 = add(db, "ICD10CM", "I21.9", "Acute myocardial infarction, unspecified",
              synonyms=[("Myocardial infarction (acute) NOS", "clinical")])
    t2 = add(db, "UMLS", "C0011860", "Diabetes Mellitus, Non-Insulin-Dependent", synonyms=[("type 2 diabetes", "lay")])
    bis = add(db, "UMLS", "C1321898", "Blood in stool", domain="symptom")
    add(db, "UMLS", "C0024881", "Mastectomy", domain="procedure")                   # no code, but clinical
    pye = add(db, "UMLS", "C0034186", "Pyelonephritis")
    n16 = add(db, "ICD10CM", "N16", "Renal tubulo-interstitial disorders in diseases classified elsewhere",
              synonyms=[("Pyelonephritis", "clinical")])
    n12 = add(db, "ICD10CM", "N12", "Tubulo-interstitial nephritis, not specified as acute or chronic")
    e11 = add(db, "ICD10CM", "E11.9", "Type 2 diabetes mellitus without complications")
    block = add(db, "ICD10CM", "I20-I25", "Ischemic heart diseases (I20-I25)", billable=0)
    db.execute("UPDATE concept SET concept_class = 'block' WHERE concept_id = ?", (block,))
    r07 = add(db, "ICD10CM", "R07.9", "Chest pain, unspecified")
    r19 = add(db, "ICD10CM", "R19.7", "Diarrhea, unspecified")
    r11 = add(db, "ICD10CM", "R11.10", "Vomiting, unspecified")
    i9 = add(db, "ICD9CM", "410.90", "Acute myocardial infarction of unspecified site")
    link(db, mi, i21, 0.85)
    link(db, mi, block, 0.9)
    link(db, mi, i9, 0.6)
    link(db, mot, i21, 0.1)   # so the procedure candidate counts as linked, just weakly
    link(db, cp, r07, 1.0)
    link(db, dia, r19, 1.0)
    link(db, vom, r11, 1.0)
    link(db, combo, r19, 0.9)
    link(db, t2, e11, 0.95)
    link(db, bis, r19, 0.3)       # too weak to show
    link(db, pye, n16, 0.45)      # penalised manifestation link
    link(db, pye, n12, 0.855)
    db.commit()
    return Annotator(db)


def test_finds_lay_clinical_and_abbreviated_mentions(annotator):
    text = "Pt has had MI before; says her heart attacks start with chest pain."
    mentions = annotator.annotate(text)
    assert [(m.text, m.best.code) for m in mentions] == [
        ("MI", "C0027051"), ("heart attacks", "C0027051"), ("chest pain", "C0008031")]
    for m in mentions:
        assert text[m.start:m.end] == m.text
    mi = mentions[0]
    # both readings of "MI" kept, the condition first
    assert [c.name for c in mi.candidates] == ["Myocardial Infarction", "Motivational Interviewing"]
    # codes: best first per vocabulary, never the block range
    assert [(l.code, l.confidence) for l in mi.best.codes["ICD10CM"]] == [("I21.9", 0.85)]
    assert mi.best.codes["ICD9CM"][0].code == "410.90"
    assert mi.best.key == "umls:C0027051"
    assert mi.assertion is None


def test_filters(annotator):
    # lower-case "mi", stopword "but", out-of-domain "patient" and code-less "arm" aren't mentions
    assert annotator.annotate("the patient said mi but her arm is fine") == []
    # a known single word is still found around them
    assert [m.text for m in annotator.annotate("but the headache, patient says")] == ["headache"]


def test_conjunction_split(annotator):
    mentions = annotator.annotate("diarrhea and vomiting since yesterday")
    assert [(m.text, m.best.codes["ICD10CM"][0].code) for m in mentions] == [
        ("diarrhea", "R19.7"), ("vomiting", "R11.10")]


def test_code_concepts_match_by_their_own_names(annotator):
    mention = annotator.annotate("myocardial infarction NOS")[0]
    assert mention.text == "myocardial infarction NOS"
    assert mention.best.vocabulary == "ICD10CM" and mention.best.codes["ICD10CM"][0].code == "I21.9"


def test_matches_stop_at_punctuation(annotator):
    # "chest pain, unspecified" is chest pain plus a word; a comma never sits inside a match
    assert [m.text for m in annotator.annotate("chest pain, unspecified")] == ["chest pain"]


def test_wording_variants(annotator):
    assert [(m.text, m.best.code) for m in annotator.annotate("type two diabetes and blood in the stool")] == [
        ("type two diabetes", "C0011860"), ("blood in the stool", "C1321898")]


def test_weak_codes_hidden_and_clinical_procedures_kept(annotator):
    blood = annotator.annotate("blood in stool")[0]
    assert blood.best.codes == {}                                    # the 0.3 link is below the threshold
    assert annotator.annotate("blood in stool")[0].best.name == "Blood in stool"
    assert [m.best.name for m in annotator.annotate("had a mastectomy")] == ["Mastectomy"]


def test_manifestation_codes_count_half(annotator):
    mention = annotator.annotate("pyelonephritis")[0]
    assert mention.best.code == "C0034186"                            # the UMLS concept, not N16 itself
    assert [l.code for l in mention.best.codes["ICD10CM"]] == ["N12", "N16"]   # N16 penalised, second
    assert mention.candidates[1].code == "N16" and mention.candidates[1].score == 0.5


def test_negation(annotator):
    got = [(m.text, m.assertion) for m in annotator.annotate(
        "Denies chest pain, headache, or diarrhea. Has had MI; no headache but vomiting since Monday. "
        "Diarrhea was ruled out. Not only headache.")]
    assert got == [("chest pain", "negated"), ("headache", "negated"), ("diarrhea", "negated"),
                   ("MI", None), ("headache", "negated"), ("vomiting", None),
                   ("Diarrhea", "negated"), ("headache", None)]


def test_node_id_matches_brain_hashing(annotator):
    blake3 = pytest.importorskip("blake3")
    best = annotator.annotate("heart attack")[0].best
    assert best.node_id == blake3.blake3(b"umls:C0027051").digest()[:16]
    assert len(best.node_id) == 16


def test_to_dict_and_evaluate(annotator, tmp_path, capsys):
    d = annotator.annotate("heart attack")[0].to_dict()
    assert d["text"] == "heart attack" and d["candidates"][0]["key"] == "umls:C0027051"
    assert d["candidates"][0]["codes"]["ICD10CM"][0]["code"] == "I21.9"

    sentences = tmp_path / "s.csv"
    sentences.write_text('text,expect,forbid\n# comment\n"hx of MI, now chest pain",MI=I21|410; chest pain=R07,\n'
                         '"the patient said but",,patient; but\n"sudden headache",headache=*; stroke=I63,\n')
    result = evaluate(annotator, sentences)
    assert result == {"recall": 0.75, "forbidden": 0, "extras": 0}
    assert "missed: stroke" in capsys.readouterr().out
