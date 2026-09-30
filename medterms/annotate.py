"""Find clinical concepts in running text (transcripts, dictation, notes) and link them to codes.

    from medterms.annotate import Annotator
    annotator = Annotator(Database("sqlite:///terms.db"))
    for mention in annotator.annotate("she had a heart attack last spring"):
        print(mention.text, mention.best.name, mention.best.codes["ICD10CM"][0].code)

Matching is by dictionary: the text is split into words, normalized the same way as every
synonym in the database, and scanned left to right for the longest run of words that is a
known term (up to `max_words`). Nothing is guessed from context yet.

Each mention keeps several candidate concepts, best first, because the same words can name
different things ("cold": common cold or feeling cold). A candidate is scored by how its
term was recorded (clinical and preferred names over lay terms, index entries and
abbreviations), by what kind of concept it is (conditions and symptoms first), and by
whether it links to any code. Its `codes` are the best ICD-10-CM, ICD-9-CM and NS fee codes
reachable through the same links `medterms lookup` follows.

Kept out on purpose:
  * concepts outside `domains` (default: conditions, symptoms, procedures, findings), which
    drops "patient", "daughter", "evening" and the like
  * single stopwords and common conversational words that happen to be UMLS strings ("but",
    "said", "well")
  * mentions whose candidates reach no code, unless one is a condition or symptom (drops
    "arm" -> Protocol Treatment Arm, "reports" -> Reporting)
  * "X and Y" as one combined concept when X and Y are both terms: "diarrhea and vomiting"
    becomes two mentions, so each keeps its own codes
  * abbreviations, and any single word under three letters, unless written in capitals in the
    text ("MI" yes, "mi" and "me" no)

`assertion` is reserved for negation / history / family context ("no chest pain", "mom had
a stroke"); it is always None for now.

Every candidate has a `key` such as "umls:C0027051" or "icd10cm:I21.9", and `node_id`
gives the 16-byte blake3 hash Brain uses for content-addressed nodes (pip install blake3).
"""

import csv
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from medterms.db import Database
from medterms.lookup import DEFAULT_CONFIDENCE, MAPPING_RELATIONSHIPS
from medterms.normalize import normalize_term

VOCABULARIES = ("UMLS", "ICD10CM", "ICD9CM", "NS_MSI")
CODE_VOCABULARIES = ("ICD10CM", "ICD9CM", "NS_MSI")
DOMAINS = ("condition", "symptom", "procedure", "observation")

TERM_WEIGHT = {"preferred": 1.0, "clinical": 1.0, "lay": 0.9, "index": 0.8, "abbreviation": 0.7, "brand": 0.7}
DOMAIN_WEIGHT = {"condition": 1.0, "symptom": 1.0, "procedure": 0.95, "observation": 0.85,
                 "drug": 0.8, "measurement": 0.8, "device": 0.7, "anatomy": 0.6, "other": 0.5}
UNLINKED = 0.8   # a concept that reaches no code is still worth showing, but after those that do
KEEP_UNLINKED = {"condition", "symptom"}   # other kinds of concept must reach a code to count

TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")

# Single words never taken as a mention on their own. Stopwords, plus words common in
# conversation that are also (usually abbreviation) strings in UMLS.
STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each either else ever every few for from
further get got had has have having he her here hers herself him himself his how i if in into is it its
itself just let like may me might mine more most much must my myself no nor not now of off on once only
or other ought our ours ourselves out over own per same she should so some such than that the their
theirs them themselves then there these they this those through to too under until up upon us very was
we were what when where which while who whom why will with within without would yes yet you your yours
yourself yourselves
okay ok um uh er ah oh hmm yeah yep nope mhm alright right well sure maybe really actually basically
said say says saying told tell tells asked ask think thought know knew guess mean means meant
see seen saw seeing look looks looked go goes going went gone come comes came coming take takes took
make made give gave put keep kept thing things stuff kind sort lot lots bit little big good bad better
best worse great fine nice day days week weeks month months year years time times today tonight
yesterday tomorrow morning evening night ago last next first second third one two three four five
""".split())

MIN_SINGLE_WORD = 3   # shorter single words ("MI", "MS") only when written in capitals


@dataclass
class CodeLink:
    vocabulary: str
    code: str
    name: str
    billable: bool
    confidence: float


@dataclass
class Candidate:
    concept_id: int
    vocabulary: str
    code: str
    name: str
    domain: str
    matched_term: str
    term_type: str
    score: float
    codes: dict[str, list[CodeLink]] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Canonical content string for this concept: 'umls:C0027051', 'icd10cm:I21.9'."""
        return f"{self.vocabulary.lower()}:{self.code}"

    @property
    def node_id(self) -> bytes:
        """Brain-compatible node id: the first 16 bytes of blake3(key)."""
        import blake3

        return blake3.blake3(self.key.encode()).digest()[:16]


@dataclass
class Mention:
    start: int
    end: int
    text: str
    candidates: list[Candidate]
    assertion: str | None = None   # reserved: negated / historical / family; not detected yet

    @property
    def best(self) -> Candidate:
        return self.candidates[0]

    def to_dict(self) -> dict:
        d = asdict(self)
        for cand, raw in zip(self.candidates, d["candidates"]):
            raw["key"] = cand.key
        return d


class Annotator:
    """Loads the term dictionary once (a few seconds, a few hundred MB), then annotates quickly."""

    def __init__(self, db: Database, vocabularies=VOCABULARIES, domains=DOMAINS, max_words: int = 10,
                 max_candidates: int = 5):
        self.db = db
        self.domains = set(domains)
        self.max_words = max_words
        self.max_candidates = max_candidates
        self._codes_cache: dict[int, dict[str, list[CodeLink]]] = {}

        vocab_list = list(vocabularies)
        marks = ",".join("?" * len(vocab_list))
        dom_marks = ",".join("?" * len(self.domains))
        self.concepts: dict[int, tuple[str, str, str, str]] = {}
        for cid, vocab, code, name, domain in db.query(
                f"SELECT concept_id, vocabulary_id, code, name, domain FROM concept "
                f"WHERE vocabulary_id IN ({marks}) AND domain IN ({dom_marks}) AND valid_end IS NULL",
                [*vocab_list, *self.domains]):
            self.concepts[cid] = (vocab, code, name, domain)

        # normalized term -> [(concept_id, term_type, term as recorded)]
        self.terms: dict[str, list[tuple[int, str, str]]] = {}
        for cid, term, norm, term_type in db.query(
                f"SELECT s.concept_id, s.term, s.term_normalized, s.term_type FROM concept_synonym s "
                f"JOIN concept c ON c.concept_id = s.concept_id "
                f"WHERE c.vocabulary_id IN ({marks}) AND c.domain IN ({dom_marks}) AND c.valid_end IS NULL",
                [*vocab_list, *self.domains]):
            if not self._usable(norm):
                continue
            entries = self.terms.setdefault(norm, [])
            if not any(e[0] == cid and e[1] == term_type for e in entries):
                entries.append((cid, term_type, term))

        # concepts that reach at least one code
        code_marks = ",".join("?" * len(CODE_VOCABULARIES))
        hops = ",".join("?" * len(MAPPING_RELATIONSHIPS))
        self.linked = {cid for (cid,) in db.query(
            f"SELECT DISTINCT r.concept_id_1 FROM concept_relationship r JOIN concept t ON t.concept_id = r.concept_id_2 "
            f"WHERE r.relationship_id IN ({hops}) AND t.vocabulary_id IN ({code_marks})",
            [*MAPPING_RELATIONSHIPS, *CODE_VOCABULARIES])}
        self.linked |= {cid for cid, (vocab, *_rest) in self.concepts.items() if vocab in CODE_VOCABULARIES}

    @staticmethod
    def _usable(norm: str) -> bool:
        if not norm or norm.isdigit():
            return False
        if " " not in norm:
            return len(norm) >= 2 and norm not in STOPWORDS
        return not all(w in STOPWORDS for w in norm.split())

    # -- matching ------------------------------------------------------------------------

    def annotate(self, text: str, with_codes: bool = True, codes_per_vocabulary: int = 3) -> list[Mention]:
        tokens = [(m.start(), m.end(), normalize_term(m.group(0))) for m in TOKEN.finditer(text)]
        tokens = [t for t in tokens if t[2]]
        mentions = []
        i = 0
        while i < len(tokens):
            found = None
            for n in range(min(self.max_words, len(tokens) - i), 0, -1):
                words = [t[2] for t in tokens[i:i + n]]
                surface = text[tokens[i][0]:tokens[i + n - 1][1]]
                entries = self._entries(" ".join(words), surface)
                if entries:
                    found = (n, surface, entries)
                    break
            if found is None:
                i += 1
                continue
            n, surface, entries = found
            split = self._conjunction_split([t[2] for t in tokens[i:i + n]])
            if split:
                n = split
                surface = text[tokens[i][0]:tokens[i + n - 1][1]]
                entries = self._entries(" ".join(t[2] for t in tokens[i:i + n]), surface)
            candidates = self._candidates(entries)
            if candidates and not any(c.concept_id in self.linked or c.domain in KEEP_UNLINKED for c in candidates):
                candidates = []
            if candidates:
                if with_codes:
                    for cand in candidates:
                        cand.codes = self.codes(cand.concept_id, codes_per_vocabulary)
                mentions.append(Mention(tokens[i][0], tokens[i + n - 1][1], surface, candidates))
            i += n
        return mentions

    def _conjunction_split(self, words: list[str]) -> int | None:
        """Word count of the left part when a match is 'X and/or Y' with X and Y both terms."""
        for j, w in enumerate(words):
            if w in ("and", "or") and 0 < j < len(words) - 1:
                left, right = " ".join(words[:j]), " ".join(words[j + 1:])
                if self._entries(left, left) and self._entries(right, right):
                    return j
        return None

    def _entries(self, key: str, surface: str):
        entries = self.terms.get(key) or self.terms.get(_singular(key))
        if not entries:
            return None
        # abbreviations count only when written in capitals ("MI", not "mi"/"me")
        capitals = surface.isupper() and len(surface.replace(" ", "")) >= 2
        if not capitals and " " not in key and len(key) < MIN_SINGLE_WORD:
            return None
        entries = [e for e in entries if e[1] != "abbreviation" or capitals]
        return entries or None

    def _candidates(self, entries) -> list[Candidate]:
        best: dict[int, Candidate] = {}
        for cid, term_type, term in entries:
            vocab, code, name, domain = self.concepts[cid]
            score = TERM_WEIGHT.get(term_type, 0.8) * DOMAIN_WEIGHT.get(domain, 0.5)
            if cid not in self.linked:
                score *= UNLINKED
            if cid not in best or score > best[cid].score:
                best[cid] = Candidate(cid, vocab, code, name, domain, term, term_type, round(score, 3))
        ranked = sorted(best.values(), key=lambda c: (-c.score, c.vocabulary != "UMLS", c.code))
        return ranked[:self.max_candidates]

    # -- codes ---------------------------------------------------------------------------

    def codes(self, concept_id: int, per_vocabulary: int = 3) -> dict[str, list[CodeLink]]:
        """Best codes per vocabulary for a concept: its own code if it is one, then its links
        (not chapters or blocks), by confidence, billable first."""
        if concept_id in self._codes_cache:
            return self._codes_cache[concept_id]
        hops = ",".join("?" * len(MAPPING_RELATIONSHIPS))
        code_marks = ",".join("?" * len(CODE_VOCABULARIES))
        rows = self.db.query(
            f"SELECT t.vocabulary_id, t.code, t.name, t.is_billable, COALESCE(r.confidence, {DEFAULT_CONFIDENCE}) "
            f"FROM concept_relationship r JOIN concept t ON t.concept_id = r.concept_id_2 "
            f"WHERE r.concept_id_1 = ? AND r.relationship_id IN ({hops}) AND t.vocabulary_id IN ({code_marks}) "
            f"AND COALESCE(t.concept_class, '') NOT IN ('chapter', 'block')",   # ranges like I10-I1A aren't codes
            [concept_id, *MAPPING_RELATIONSHIPS, *CODE_VOCABULARIES])
        own = self.concepts.get(concept_id)
        if own and own[0] in CODE_VOCABULARIES:
            billable = self.db.query("SELECT is_billable FROM concept WHERE concept_id = ?", (concept_id,))[0][0]
            rows = [(own[0], own[1], own[2], billable, 1.0), *rows]
        best: dict[tuple[str, str], CodeLink] = {}
        for vocab, code, name, billable, confidence in rows:
            link = CodeLink(vocab, code, name, bool(billable), round(float(confidence), 3))
            if (vocab, code) not in best or link.confidence > best[(vocab, code)].confidence:
                best[(vocab, code)] = link
        out: dict[str, list[CodeLink]] = {}
        for link in sorted(best.values(), key=lambda l: (-l.confidence, not l.billable, l.code)):
            if len(out.setdefault(link.vocabulary, [])) < per_vocabulary:
                out[link.vocabulary].append(link)
        self._codes_cache[concept_id] = out
        return out


def _singular(key: str) -> str:
    """Fold a plural last word: 'heart attacks' -> 'heart attack', 'allergies' -> 'allergy'."""
    head, _, last = key.rpartition(" ")
    if len(last) > 4 and last.endswith("ies"):
        last = last[:-3] + "y"
    elif len(last) > 3 and last.endswith("s") and not last.endswith(("ss", "us", "is")):
        last = last[:-1]
    else:
        return key
    return f"{head} {last}".strip()


def evaluate(annotator: Annotator, path: Path, verbose: bool = False) -> dict[str, float]:
    """Score annotation against CSV rows: text, expect, forbid.

    expect: 'words=CODE|CODE; words=CODE' — each listed phrase must be found as (part of) a
            mention whose top candidates reach a code starting with one of the prefixes
            (ICD-10-CM, ICD-9-CM or NS); '*' accepts any candidate.
    forbid: 'word; word' — phrases that must not be annotated.
    Mentions matching neither list are counted and listed as unreviewed extras.
    """
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("text") and not r["text"].startswith("#")]
    expected = found = forbidden = forbidden_hits = extras = 0
    misses, bad, extra_list = [], [], []
    for row in rows:
        mentions = annotator.annotate(row["text"])
        claimed = set()
        for item in filter(None, (s.strip() for s in (row.get("expect") or "").split(";"))):
            phrase, _, want = item.partition("=")
            phrase, prefixes = phrase.strip().lower(), [p.strip() for p in want.split("|") if p.strip()]
            expected += 1
            hit = None
            for idx, m in enumerate(mentions):
                if phrase in m.text.lower() or m.text.lower() in phrase:
                    codes = [l.code for c in m.candidates[:3] for links in c.codes.values() for l in links]
                    if not prefixes or prefixes == ["*"] or any(code.startswith(p) for code in codes for p in prefixes):
                        hit = idx
                        break
            if hit is None:
                misses.append(f"{phrase} ({row['text'][:50]}…)")
            else:
                found += 1
                claimed.add(hit)
        for phrase in filter(None, (s.strip().lower() for s in (row.get("forbid") or "").split(";"))):
            forbidden += 1
            for idx, m in enumerate(mentions):
                if phrase == m.text.lower() or (phrase in m.text.lower().split()):
                    forbidden_hits += 1
                    bad.append(f"{m.text} -> {m.best.name}")
                    claimed.add(idx)
                    break
        for idx, m in enumerate(mentions):
            if idx not in claimed:
                extras += 1
                extra_list.append(f"{m.text} -> {m.best.name}")
        if verbose:
            print(row["text"])
            for m in mentions:
                codes = "; ".join(f"{v} {links[0].code}" for v, links in m.best.codes.items() if links)
                print(f"   [{m.text}] {m.best.name} ({m.best.domain}, {m.best.score}) {codes}")
    recall = found / expected if expected else 1.0
    print(f"{len(rows)} texts: expected mentions found {found}/{expected} ({recall:.0%}); "
          f"forbidden phrases annotated {forbidden_hits}/{forbidden}; other mentions {extras}")
    if misses:
        print("  missed: " + ", ".join(misses))
    if bad:
        print("  wrongly annotated: " + ", ".join(bad))
    if extra_list and verbose:
        print("  other mentions (review): " + ", ".join(extra_list))
    return {"recall": recall, "forbidden": forbidden_hits, "extras": extras}
