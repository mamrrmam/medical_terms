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
    i21 = add(db, "ICD10CM", "I21.9", "Acute myocardial infarction, unspecified")
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
    mention = annotator.annotate("chest pain, unspecified")[0]
    assert mention.text == "chest pain, unspecified"
    assert mention.best.vocabulary == "ICD10CM" and mention.best.codes["ICD10CM"][0].code == "R07.9"


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
