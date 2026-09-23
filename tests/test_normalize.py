from medterms.normalize import normalize_term


def test_normalize_term():
    assert normalize_term("Alzheimer's  Disease") == "alzheimers disease"
    assert normalize_term("Anxiety, generalized") == "anxiety generalized"
    assert normalize_term("Ménière's") == "menieres"
    assert normalize_term("  --  ") == ""
