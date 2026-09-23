import re
import unicodedata


def normalize_term(term: str) -> str:
    """Lookup key for a term: accents stripped, lowercase, apostrophes dropped,
    other punctuation turned into spaces, whitespace collapsed.

    "Alzheimer's  Disease" -> "alzheimers disease"; "Anxiety, generalized" -> "anxiety generalized"
    """
    text = unicodedata.normalize("NFKD", term)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"['’]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()
