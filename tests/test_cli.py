from conftest import FIXTURES
from medterms.cli import main


def test_load_and_lookup(tmp_path, capsys):
    db = f"sqlite:///{tmp_path / 'cli.db'}"
    main(["--db", db, "load", "icd10cm", f"{FIXTURES}/icd10cm"])
    main(["--db", db, "load", "umls", f"{FIXTURES}/umls"])
    main(["--db", db, "load", "icd9cm", f"{FIXTURES}/icd9cm"])
    capsys.readouterr()

    main(["--db", db, "lookup", "heart attack", "--to", "ICD10CM"])
    assert "I21.9" in capsys.readouterr().out

    main(["--db", db, "lookup", "anxiety state"])
    out = capsys.readouterr().out
    assert "F41.1" in out and "300.00" in out
