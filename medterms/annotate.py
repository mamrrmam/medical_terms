"""Find clinical concepts in running text (transcripts, dictation, notes) and link them to codes.

    from medterms.annotate import Annotator
    annotator = Annotator(Database("sqlite:///terms.db"))
    for mention in annotator.annotate("she had a heart attack last spring"):
        print(mention.text, mention.best.name, mention.best.codes["ICD10CM"][0].code)

Matching is by dictionary: the text is split into words, normalized the same way as every
synonym in the database, and scanned left to right for the longest run of words that is a
known term (up to `max_words`). Nothing is guessed from context yet.

Before the dictionary, the curated lay lexicon (medterms/lexicon.py) is tried at each position:
everyday wording and body-part patterns such as "can't put weight on it", "broke his wrist" and
"my knee hurts", mapped to terms or codes. A lexicon match wins over a dictionary match of the
same length; its best candidate has term_type "curated".

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
  * matches across punctuation ("leg, numbness" is not "leg numbness")
  * side synonyms of a single word that is the proper name of something non-clinical: "blood"
    is listed as a synonym of leukemia, but it only counts for a concept named "Blood ..."

Terms are also indexed without their bracketed parts ("Myocardial infarction (acute) NOS").
Wording variations tried when the text doesn't match as written: a plural last word ("heart
attacks"), number words ("type two diabetes" -> "type 2 diabetes") and articles ("blood in the
stool" -> "blood in stool").

Codes shown for a candidate are its links with confidence of at least `min_confidence` (0.4),
billable codes ahead of categories unless a category is clearly the better match, and
"unspecified" codes first among equals. Manifestation codes ("... in diseases classified
elsewhere"), which ICD doesn't allow as a primary diagnosis, count half.

Ambiguity: when the top candidates mean different things ("MS": multiple sclerosis or mitral
stenosis), cue words from lexicon_data/senses.csv anywhere else in the text pick the sense
("numbness", "vision" vs "murmur", "valve"); failing that, the candidate whose ICD-10 chapter the
other mentions share wins; failing that, `mention.ambiguous` is True (annotate(ambiguous="drop")
leaves such mentions out). The sense chosen is recorded in `cues`.

Context, in the style of ConText (NegEx extended), from cue words before or after a mention in
the same clause:

  assertion    "negated" ("no fever", "denies ...", "MI was ruled out"), "hypothetical" ("call if
               you get a fever", "risk of"), "uncertain" ("possible", "rule out", "suspected")
  experiencer  "family" ("mom had a stroke", "family history of", "runs in the family"); words
               for children don't count, since the parent is often describing the patient
  temporality  "history" ("history of", "hx", "s/p", "prior", "years ago", "in 2019")

For a relative's or a past condition, the best candidate's ICD codes switch to the specific
"Family history of ...", "Personal history of ..." or "Old ..." code: first from the curated table
lexicon_data/context_codes.csv (a heart attack is under "ischemic heart disease"), else by name (stroke in a
parent: Z82.3 / V17.1; history of breast cancer: Z85.3 / V10.3; history of MI: I25.2 / 412), with
the condition's own codes kept in `condition_codes`. A relative's condition without a specific
code gets the generic family history code (Z84.89 / V19.8). A past condition without a specific
personal history code keeps its codes: "history of hypertension" means it is still there.

The negation rules in detail: `assertion` is "negated" when a negation cue governs the mention ("no fever", "denies shortness
of breath, palpitations, or syncope", "MI was ruled out"), in the style of NegEx: a cue before
the mention reaches forward to the end of its clause (a full stop, semicolon, colon or "but"),
at most 12 words; a cue right after it ("... ruled out", "... negative") reaches back 3 words.


Every candidate has a `key` such as "umls:C0027051" or "icd10cm:I21.9", and `node_id`
gives the 16-byte blake3 hash Brain uses for content-addressed nodes (pip install blake3).
"""

import csv
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from medterms.db import Database
from medterms.lexicon import CODE_SPEC, Lexicon, LexiconMatch
from medterms.lookup import DEFAULT_CONFIDENCE, MANIFESTATION, MAPPING_RELATIONSHIPS, primary_sense
from medterms.normalize import normalize_term

VOCABULARIES = ("UMLS", "ICD10CM", "ICD9CM", "NS_MSI")
CODE_VOCABULARIES = ("ICD10CM", "ICD9CM", "NS_MSI")
DOMAINS = ("condition", "symptom", "procedure", "observation")

TERM_WEIGHT = {"preferred": 1.0, "clinical": 1.0, "lay": 0.9, "index": 0.8, "abbreviation": 0.7, "brand": 0.7}
DOMAIN_WEIGHT = {"condition": 1.0, "symptom": 1.0, "procedure": 0.95, "observation": 0.85,
                 "drug": 0.8, "measurement": 0.8, "device": 0.7, "anatomy": 0.6, "other": 0.5}
SECONDARY_SENSE = 0.95   # the term is only a synonym of this concept, not its name ("hip fracture" on Fracture of pelvis)
UNLINKED = 0.8   # a concept that reaches no code is still worth showing, but after those that do
SUPPRESS = "suppress"
TIE = 0.1   # candidates this close in score count as tied
KEEP_UNLINKED = {"condition", "symptom"}   # other kinds of concept must reach a code to count ...
# ... unless their name has a clinical word in it ("Mastectomy", "Chemotherapy"), which "Reporting",
# "Placement - action" and "Protocol Treatment Arm" don't
CLINICAL_WORD = re.compile(
    r"(itis|osis|emia|oma|pathy|algia|plegia|trophy|ectasia|iasis|rrhea|rrhage|cele|ectomy|otomy|ostomy|"
    r"plasty|scopy|graphy|therapy|pexy|rrhaphy|centesis|transplant\w*|biops\w+|dialysis|vaccin\w*|immuni[sz]ation)$")
NOT_CLINICAL = {"diagnosis", "prognosis", "osmosis", "hypothesis", "emphasis", "thesis"}
MIN_CODE_CONFIDENCE = 0.4   # right answers from contained/broader matches score 0.43-0.5; wrong ones lower
NON_BILLABLE = 0.1        # a category ranks behind a billable code unless it is this much more confident

BREAK = re.compile(r"[,;.:!?()\[\]\n]")         # a match never spans these
CLAUSE_END = re.compile(r"[;.:!?\n]")             # negation scope ends here (commas don't end it: lists)
NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
                "eight": "8", "nine": "9", "ten": "10"}
ARTICLES = {"the", "a", "an"}
BRACKETED = re.compile(r"\([^)]*\)|\[[^]]*\]")

NEGATION_BEFORE = {"no", "not", "dont", "doesnt", "didnt", "isnt", "wasnt", "arent", "werent", "hasnt", "havent",
                   "hadnt", "denies", "denied", "deny", "denying", "without", "negative for", "free of",
                   "never had", "no history of", "no evidence of", "no sign of", "no signs of", "absence of",
                   "ruled out", "no complaints of", "nor", "never"}
NEGATION_AFTER = {"ruled out", "was ruled out", "is ruled out", "negative", "was negative", "is negative",
                  "absent", "not present"}
PSEUDO_NEGATION = {"not only", "no change", "no increase", "no further", "not ruled out", "not rule out",
                   "gram negative", "no longer sure", "cannot be ruled out", "cant be ruled out", "can not be ruled out"}
HYPOTHETICAL_BEFORE = {"if", "in case", "in case of", "watch for", "monitor for", "look out for", "return if",
                       "call if", "come back if", "in the event of", "risk of", "at risk for", "at risk of",
                       "to prevent", "prevent", "prevention of", "to avoid"}
UNCERTAIN_BEFORE = {"possible", "possibly", "probable", "probably", "likely", "suspected", "suspect", "suspicious for",
                    "suspicion of", "concern for", "concerning for", "questionable", "rule out", "rules out",
                    "to rule out", "r o", "may have", "might have", "could be", "maybe", "query", "consider",
                    "differential includes", "versus", "vs"}
UNCERTAIN_AFTER = {"not ruled out", "cannot be ruled out", "cant be ruled out", "can not be ruled out", "not excluded",
                   "cannot be excluded", "is possible", "is suspected", "is likely", "suspected", "likely", "possible"}
# Relatives. Children (son, daughter, baby, kid) are left out on purpose: in a pediatric visit the
# parent is describing the patient ("my son has a peanut allergy").
FAMILY_BEFORE = {"mom", "mother", "mothers", "mum", "dad", "father", "fathers", "parents", "parent", "brother",
                 "brothers", "sister", "sisters", "sibling", "siblings", "grandmother", "grandfather", "grandma",
                 "grandpa", "granny", "grandparents", "aunt", "aunts", "uncle", "uncles", "cousin", "cousins",
                 "family history of", "family history", "family hx", "fhx", "fh", "relatives", "relative",
                 "family member", "family members"}
FAMILY_AFTER = {"runs in the family", "run in the family", "runs in her family", "runs in his family",
                "runs in my family", "in the family", "in her family", "in his family", "in my family"}
HISTORY_BEFORE = {"history of", "hx of", "hx", "h o", "pmh", "pmhx", "past medical history", "past medical history of",
                  "past history of", "personal history of", "previous", "previously", "prior", "status post", "s p",
                  "remote history of", "had a history of"}
HISTORY_AFTER = {"years ago", "year ago", "months ago", "in the past", "as a child", "as a kid", "as a teenager",
                 "when she was young", "when he was young", "when i was young", "resolved", "has resolved",
                 "in remission"}
# A contrast or a change of subject ends a cue's scope: "no fever but a cough", "possible kidney
# infection, she has flank pain", "her mom had a stroke and she has asthma". Lists keep it going:
# "denies shortness of breath, palpitations, or syncope".
SCOPE_END_WORDS = {"but", "however", "although", "though", "except", "yet", "aside", "apart",
                   "she", "he", "i", "patient", "pt", "we"}
FAMILY_SCOPE_END = SCOPE_END_WORDS
NEGATION_REACH, NEGATION_REACH_AFTER = 12, 3
YEAR = re.compile(r"^(19|20)\d\d$")

# (field, value, cues before, cues after, reach before, words ending the scope), in priority order:
# a mention gets the first assertion that applies, and experiencer / temporality independently.
CONTEXTS = [
    ("assertion", "negated", NEGATION_BEFORE, NEGATION_AFTER, NEGATION_REACH, SCOPE_END_WORDS),
    ("assertion", "hypothetical", HYPOTHETICAL_BEFORE, set(), 10, SCOPE_END_WORDS),
    ("assertion", "uncertain", UNCERTAIN_BEFORE, UNCERTAIN_AFTER, 8, SCOPE_END_WORDS),
    ("experiencer", "family", FAMILY_BEFORE, FAMILY_AFTER, 10, FAMILY_SCOPE_END),
    ("temporality", "history", HISTORY_BEFORE, HISTORY_AFTER, NEGATION_REACH, SCOPE_END_WORDS),
]
FAMILY_FALLBACK = {"ICD10CM": "Z84.89", "ICD9CM": "V19.8"}
GENERIC_WORDS = {"disease", "disorder", "condition", "syndrome", "infection", "injury", "pain", "other", "neoplasm",
                 "malignant", "abnormality", "problem"}
ALL_CUES = set().union(*(before | after for _, _, before, after, _, _ in CONTEXTS))   # family history of other specified conditions

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
    assertion: str | None = None     # "negated", "hypothetical" or "uncertain"
    experiencer: str | None = None   # "family" when it's a relative's condition
    temporality: str | None = None   # "history" when it's past rather than current
    cues: list[str] = field(default_factory=list)   # the context cues found, e.g. ["family: mom"]
    ambiguous: bool = False          # top candidates mean different things and nothing in the text decided
    condition_codes: dict[str, list[CodeLink]] | None = None   # the condition's own codes, when history /
                                                               # family codes replaced them in best.codes

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
                 max_candidates: int = 5, lexicon: Lexicon | None | bool = True):
        """lexicon: True for the bundled one, a Lexicon, or None/False for none."""
        self.db = db
        self.lexicon = Lexicon.load() if lexicon is True else (lexicon or None)
        self._resolved: dict[str, tuple | None] = {}
        self._history_memo: dict[tuple, dict] = {}
        self.domains = set(domains)
        self.max_words = max_words
        self.max_candidates = max_candidates
        self._codes_cache: dict[tuple, dict[str, list[CodeLink]]] = {}

        vocab_list = list(vocabularies)
        marks = ",".join("?" * len(vocab_list))
        dom_marks = ",".join("?" * len(self.domains))
        self.concepts: dict[int, tuple[str, str, str, str]] = {}
        for cid, vocab, code, name, domain in db.query(
                f"SELECT concept_id, vocabulary_id, code, name, domain FROM concept "
                f"WHERE vocabulary_id IN ({marks}) AND domain IN ({dom_marks}) AND valid_end IS NULL "
                f"AND COALESCE(concept_class, '') NOT IN ('chapter', 'block')",   # ranges (M15-M19) aren't codes
                [*vocab_list, *self.domains]):
            self.concepts[cid] = (vocab, code, name, domain)

        # normalized term -> [(concept_id, term_type, term as recorded)]
        self.terms: dict[str, list[tuple[int, str, str]]] = {}
        for cid, term, norm, term_type in db.query(
                f"SELECT s.concept_id, s.term, s.term_normalized, s.term_type FROM concept_synonym s "
                f"JOIN concept c ON c.concept_id = s.concept_id "
                f"WHERE c.vocabulary_id IN ({marks}) AND c.domain IN ({dom_marks}) AND c.valid_end IS NULL "
                f"AND COALESCE(c.concept_class, '') NOT IN ('chapter', 'block')",
                [*vocab_list, *self.domains]):
            if not self._usable(norm):
                continue
            keys = {norm}
            if BRACKETED.search(term):   # "Myocardial infarction (acute) NOS" -> "myocardial infarction nos"
                stripped = normalize_term(BRACKETED.sub(" ", term))
                if self._usable(stripped):
                    keys.add(stripped)
            words = norm.split()
            if len(words) > 2 and ARTICLES & set(words):
                keys.add(" ".join(w for w in words if w not in ARTICLES))
            for key in keys:
                entries = self.terms.setdefault(key, [])
                if not any(e[0] == cid and e[1] == term_type for e in entries):
                    entries.append((cid, term_type, term))

        # Single words that are the proper name of something outside `domains` (blood the body
        # substance, arm, patient). They only count for a concept named by that word: UMLS lists
        # "blood" as a synonym of leukemia.
        self.generic_words = {norm for (norm,) in db.query(
            f"SELECT DISTINCT s.term_normalized FROM concept_synonym s JOIN concept c ON c.concept_id = s.concept_id "
            f"WHERE c.vocabulary_id = 'UMLS' AND c.domain NOT IN ({dom_marks}) "
            f"AND s.term_type IN ('preferred', 'clinical') AND s.term_normalized NOT LIKE '% %'",
            list(self.domains))}

        # words a dictionary term can start with (a cheap check before trying any window)
        self.first_words = {key.split(" ", 1)[0] for key in self.terms}

        # concepts that reach at least one code
        code_marks = ",".join("?" * len(CODE_VOCABULARIES))
        hops = ",".join("?" * len(MAPPING_RELATIONSHIPS))
        self.linked = {cid for (cid,) in db.query(
            f"SELECT DISTINCT r.concept_id_1 FROM concept_relationship r JOIN concept t ON t.concept_id = r.concept_id_2 "
            f"WHERE r.relationship_id IN ({hops}) AND t.vocabulary_id IN ({code_marks})",
            [*MAPPING_RELATIONSHIPS, *CODE_VOCABULARIES])}
        self.linked |= {cid for cid, (vocab, *_rest) in self.concepts.items() if vocab in CODE_VOCABULARIES}
        self.code_index = {(vocab, code): cid for cid, (vocab, code, *_rest) in self.concepts.items()}

    @staticmethod
    def _usable(norm: str) -> bool:
        if not norm or norm.isdigit():
            return False
        if " " not in norm:
            return len(norm) >= 2 and norm not in STOPWORDS
        return not all(w in STOPWORDS for w in norm.split())

    # -- matching ------------------------------------------------------------------------

    def annotate(self, text: str, with_codes: bool = True, codes_per_vocabulary: int = 3,
                 min_confidence: float = MIN_CODE_CONFIDENCE, ambiguous: str = "keep") -> list[Mention]:
        """Mentions in `text`, in order. ambiguous: "keep" leaves unresolved ambiguous mentions in,
        flagged, with their best guess first; "drop" leaves them out."""
        if ambiguous not in ("keep", "drop"):
            raise ValueError("ambiguous must be 'keep' or 'drop'")
        tokens = self._tokens(text)
        mentions: list[tuple[int, int, Mention]] = []   # (first token, last token, mention)
        i = 0
        while i < len(tokens):
            lex = self.lexicon.match(tokens, i, text) if self.lexicon else None
            found = None
            word = tokens[i][2]
            can_start = (word in self.first_words or NUMBER_WORDS.get(word) in self.first_words
                         or word in ARTICLES or _singular(word) in self.first_words)
            for n in range(min(self.max_words, len(tokens) - i) if can_start else 0, 0, -1):
                if any(tokens[k][3] for k in range(i + 1, i + n)):
                    continue   # would span punctuation
                words = [tok[2] for tok in tokens[i:i + n]]
                surface = text[tokens[i][0]:tokens[i + n - 1][1]]
                entries = self._lookup(words, surface)
                if entries:
                    found = (n, entries)
                    break
            if lex and (found is None or lex.length >= found[0]):
                resolved = self._resolve_match(lex)
                if resolved == SUPPRESS:
                    i += lex.length
                    continue
                if resolved:
                    candidates = self._choose(resolved, codes_per_vocabulary, min_confidence)
                    if not with_codes:
                        for cand in candidates:
                            cand.codes = {}
                    start, end = tokens[i][0], tokens[i + lex.length - 1][1]
                    mentions.append((i, i + lex.length - 1, Mention(start, end, text[start:end], candidates)))
                    i += lex.length
                    continue
            if found is None:
                i += 1
                continue
            n, entries = found
            split = self._conjunction_split([tok[2] for tok in tokens[i:i + n]])
            if split:
                n = split
                entries = self._lookup([tok[2] for tok in tokens[i:i + n]], text[tokens[i][0]:tokens[i + n - 1][1]])
            candidates = self._candidates(entries)
            if candidates and not any(self._keep(c) for c in candidates):
                candidates = []
            if candidates and _is_cue([tok[2] for tok in tokens[i:i + n]]):
                candidates = []   # "family history of" is a context cue, not a finding
            if candidates:
                if with_codes:
                    for cand in candidates:
                        cand.codes = self.codes(cand.concept_id, codes_per_vocabulary, min_confidence)
                start, end = tokens[i][0], tokens[i + n - 1][1]
                mentions.append((i, i + n - 1, Mention(start, end, text[start:end], candidates)))
            i += n
        _mark_context(tokens, mentions)
        self._disambiguate(tokens, mentions, codes_per_vocabulary, min_confidence, with_codes)
        if with_codes:
            for _, _, mention in mentions:
                self._context_codes(mention, codes_per_vocabulary)
        return [m for _, _, m in mentions if not (ambiguous == "drop" and m.ambiguous)]

    @staticmethod
    def _tokens(text: str) -> list[tuple[int, int, str, bool, bool]]:
        """(start, end, normalized word, punctuation before it, clause ended before it)."""
        out = []
        prev_end = 0
        for m in TOKEN.finditer(text):
            norm = normalize_term(m.group(0))
            if not norm:
                continue
            gap = text[prev_end:m.start()]
            out.append((m.start(), m.end(), norm, bool(BREAK.search(gap)), bool(CLAUSE_END.search(gap))))
            prev_end = m.end()
        return out

    def _lookup(self, words: list[str], surface: str):
        """Entries for these words as written, else with number words as digits, else without articles."""
        key = " ".join(words)
        if words[0] in ARTICLES:
            return self._entries(key, surface)   # a match doesn't start on "an" ("an ear infection")
        variants = [key, " ".join(NUMBER_WORDS.get(w, w) for w in words)]
        if len(words) > 2:
            variants += [" ".join(w for w in v.split() if w not in ARTICLES) for v in variants]
        for variant in dict.fromkeys(variants):
            if variant and (entries := self._entries(variant, surface)):
                return entries
        return None

    def _keep(self, cand: "Candidate") -> bool:
        if cand.concept_id in self.linked or cand.domain in KEEP_UNLINKED:
            return True
        return any(CLINICAL_WORD.search(w) and w not in NOT_CLINICAL
                   for w in normalize_term(f"{cand.name} {cand.matched_term}").split())

    # -- ambiguity -------------------------------------------------------------------------

    def _disambiguate(self, tokens, mentions, per_vocabulary: int, min_confidence: float, with_codes: bool) -> None:
        """Pick a sense for mentions whose top candidates mean different things: first by the
        cue words for the term in senses.csv appearing elsewhere in the text, then by which
        candidate's ICD-10 chapter the other mentions share; otherwise flag the mention."""
        from medterms.linker import word_key

        def ngrams(skip: range) -> set[str]:
            words = [tok[2] for k, tok in enumerate(tokens) if k not in skip]
            return {" ".join(words[i:i + n]) for n in (1, 2, 3) for i in range(len(words) - n + 1)}

        def distinct(a: "Candidate", b: "Candidate") -> bool:
            return word_key(a.name) != word_key(b.name) and _chapter(a) != _chapter(b)

        settled = [m for _, _, m in mentions]
        for first, last, mention in mentions:
            senses = self.lexicon.senses.get(normalize_term(mention.text)) if self.lexicon else None
            options = mention.candidates
            if senses:
                context = ngrams(range(first, last + 1))
                scored = []
                for target, cues in senses:
                    if target not in self._resolved:
                        self._resolved[target] = self._resolve(target)
                    resolved = self._resolved[target]
                    if resolved and resolved != SUPPRESS:
                        scored.append(([c for c in cues if c in context], resolved))
                hits = sorted(scored, key=lambda s: -len(s[0]))
                if hits and hits[0][0] and (len(hits) == 1 or len(hits[0][0]) > len(hits[1][0])):
                    self._put_first(mention, hits[0][1], per_vocabulary, min_confidence, with_codes)
                    mention.cues.append(f"sense: {', '.join(hits[0][0])}")
                    continue
                options = [self._copy_first(resolved, per_vocabulary, min_confidence, with_codes)
                           for _, resolved in scored]
            elif not (len(options) > 1 and options[1].score >= options[0].score - TIE and distinct(options[0], options[1])):
                continue
            # no sense cues: which option shares an ICD-10 chapter with the other mentions?
            others = [_chapter(m.best) for m in settled if m is not mention and not m.ambiguous]
            support = sorted(((sum(1 for ch in others if ch and ch == _chapter(c)), i) for i, c in enumerate(options[:2])),
                             reverse=True)
            if len(support) > 1 and support[0][0] > support[1][0]:
                winner = options[support[0][1]]
                mention.candidates = [winner, *[c for c in mention.candidates if c.concept_id != winner.concept_id]]
                mention.cues.append(f"sense: context ({_chapter(winner)})")
            else:
                mention.ambiguous = True

    def _copy_first(self, resolved, per_vocabulary, min_confidence, with_codes) -> "Candidate":
        candidates, extras = resolved
        cand = Candidate(**{**candidates[0].__dict__, "codes": {}})
        if with_codes:
            cand.codes = self._with_extra_codes(self.codes(cand.concept_id, per_vocabulary, min_confidence), extras)
        return cand

    def _put_first(self, mention, resolved, per_vocabulary, min_confidence, with_codes) -> None:
        cand = self._copy_first(resolved, per_vocabulary, min_confidence, with_codes)
        cand.matched_term = mention.text
        mention.candidates = [cand, *[c for c in mention.candidates if c.concept_id != cand.concept_id]][:self.max_candidates]

    # -- history / family codes ------------------------------------------------------------

    HISTORY_TITLE = re.compile(r"^(family history of|personal history of|old) (.+)$", re.I)
    TITLE_TAIL = re.compile(r"\s*(\band other\b|\bor other\b|, not elsewhere classified|, NEC\b).*$", re.I)

    def _history_index(self):
        """kind ('family' / 'history') -> [(word key of the condition, vocab, code, name, billable)]."""
        if getattr(self, "_history_codes", None) is None:
            from medterms.linker import word_key

            index = {"family": [], "history": []}
            for vocab, code, name, billable in self.db.query(
                    "SELECT vocabulary_id, code, name, is_billable FROM concept WHERE vocabulary_id IN ('ICD10CM', 'ICD9CM') "
                    "AND (name LIKE 'Family history of %' OR name LIKE 'Personal history of %' OR name LIKE 'Old %') "
                    "AND valid_end IS NULL"):
                m = self.HISTORY_TITLE.match(name)
                condition = self.TITLE_TAIL.sub("", m.group(2))
                key = word_key(condition)
                if key:
                    kind = "family" if m.group(1).lower().startswith("family") else "history"
                    index[kind].append((key, vocab, code, name, bool(billable)))
            self._history_codes = index
        return self._history_codes

    def _curated_context_codes(self) -> list[tuple[str, str, dict[str, str]]]:
        """(kind, ICD-10-CM prefix, {vocab: code}) rows from lexicon_data/context_codes.csv."""
        if getattr(self, "_context_rows", None) is None:
            from importlib import resources

            path = Path(str(resources.files("medterms").joinpath("lexicon_data", "context_codes.csv")))
            with open(path, newline="", encoding="utf-8") as f:
                lines = [line for line in f if line.strip() and not line.startswith("#")]
            self._context_rows = [(row["kind"], row["condition"].strip(),
                                   {v: row[v.lower()].strip() for v in ("ICD10CM", "ICD9CM") if row.get(v.lower(), "").strip()})
                                  for row in csv.DictReader(lines)]
        return self._context_rows

    def _context_codes(self, mention: "Mention", per_vocabulary: int) -> None:
        """Swap in family / personal history codes for a relative's or a past condition."""
        if mention.assertion == "negated":
            return
        kind = "family" if mention.experiencer == "family" else "history" if mention.temporality == "history" else None
        if kind is None:
            return
        from medterms.linker import word_key

        best = mention.best
        sources = [best.name, best.matched_term, mention.text,
                   *(links[0].name for links in best.codes.values() if links)]
        keys = [k for k in (word_key(s) for s in sources) if k]
        def fits(key, source, tier):
            if tier == 0:
                return key == source
            if tier == 1:   # one word more or less, sharing two words or one specific word ("diabetes")
                common = key & source
                return (len(common) >= 2 or (len(common) == 1 and not common & GENERIC_WORDS)) and (
                    (key < source and len(source - key) <= 1) or (source < key and len(key - source) <= 1))
            # the history title contains the condition's name: "cerebral infarction" in
            # "Personal history of transient ischemic attack (TIA), and cerebral infarction ..."
            return len(source) >= 2 and source < key

        found: dict[str, list[CodeLink]] = {}
        own = [links[0].code for links in (best.codes.get("ICD10CM") or [],) if links]
        curated = [row for row in self._curated_context_codes()
                   if row[0] == kind and own and own[0].startswith(row[1])]
        if curated:
            _, _, codes = max(curated, key=lambda row: len(row[1]))
            for vocab, code in codes.items():
                if (cid := self._code_concept(f"{vocab.lower()}:{code}")) is not None:
                    found[vocab] = [CodeLink(vocab, code, self.concepts[cid][2], True, 1.0)]
        # by name, remembered per condition: the same conditions come up again and again
        memo_key = (kind, tuple(sorted(tuple(sorted(k)) for k in keys)))
        if memo_key not in self._history_memo:
            by_name: dict[str, list[CodeLink]] = {}
            for tier, confidence in ((0, 1.0), (1, 0.8), (2, 0.7)):
                for key, vocab, code, name, billable in self._history_index()[kind]:
                    if vocab not in by_name and any(fits(key, source, tier) for source in keys):
                        by_name[vocab] = [CodeLink(vocab, code, name, billable, confidence)]
            self._history_memo[memo_key] = by_name
        for vocab, links in self._history_memo[memo_key].items():
            found.setdefault(vocab, links)
        if kind == "family":
            for vocab, code in FAMILY_FALLBACK.items():
                if vocab not in found and (cid := self._code_concept(f"{vocab.lower()}:{code}")) is not None:
                    found[vocab] = [CodeLink(vocab, code, self.concepts[cid][2], True, 0.5)]
        if not found:
            return   # "history of hypertension": no personal history code, so it's the chronic condition itself
        mention.condition_codes = best.codes
        best.codes = {**{v: links for v, links in best.codes.items() if v not in CODE_VOCABULARIES or v == "NS_MSI"},
                      **found}

    # -- lexicon ---------------------------------------------------------------------------

    def _resolve_match(self, match: LexiconMatch):
        """SUPPRESS, or [(candidates, extra code concept ids)] for every target that resolves, in order."""
        options = []
        for target in match.targets():
            if target not in self._resolved:
                self._resolved[target] = self._resolve(target)
            result = self._resolved[target]
            if result == SUPPRESS:
                return SUPPRESS
            if result is not None:
                candidates, extras = result
                copies = [Candidate(**{**c.__dict__, "codes": {}}) for c in candidates]
                copies[0].matched_term = match.pattern.text
                options.append((copies, extras))
        return options

    def _choose(self, options, per_vocabulary: int, min_confidence: float) -> list["Candidate"]:
        """Put the option whose best candidate reaches the most code sets first; the other
        options' best candidates follow as alternatives."""
        scored = []
        for index, (candidates, extras) in enumerate(options):
            for cand in candidates:
                cand.codes = self.codes(cand.concept_id, per_vocabulary, min_confidence)
            candidates[0].codes = self._with_extra_codes(candidates[0].codes, extras)
            reach = sum(1 for links in candidates[0].codes.values() if links)
            scored.append((-reach, index, candidates))
        scored.sort(key=lambda s: (s[0], s[1]))
        chosen = list(scored[0][2])
        for _, _, candidates in scored[1:]:
            if all(c.concept_id != candidates[0].concept_id for c in chosen):
                chosen.append(candidates[0])
        return chosen[:self.max_candidates]

    def _resolve(self, target: str):
        if target == "-":
            return SUPPRESS
        primary, *extra_specs = [part.strip() for part in target.split("+")]
        extras = [cid for spec in extra_specs if (cid := self._code_concept(spec)) is not None]
        if CODE_SPEC.match(primary):
            cid = self._code_concept(primary)
            if cid is None:
                return None
            vocab, code, name, domain = self.concepts[cid]
            candidates = [Candidate(cid, vocab, code, name, domain, primary, "curated", 1.0)]
        else:
            words = normalize_term(primary).split()
            entries = self._lookup(words, primary.lower()) if words else None
            if not entries:
                return None
            candidates = self._candidates(entries)
            if not candidates:
                return None
        candidates[0].term_type, candidates[0].score = "curated", 1.0
        return candidates, extras

    def _code_concept(self, spec: str) -> int | None:
        """Concept id for 'vocab:code', loading it even if outside `domains`."""
        vocab, _, code = spec.partition(":")
        key = (vocab.upper(), code)
        if key not in self.code_index:
            rows = self.db.query("SELECT concept_id, name, domain FROM concept WHERE vocabulary_id = ? AND code = ?", key)
            if not rows:
                return None
            cid, name, domain = rows[0]
            self.concepts[cid] = (key[0], code, name, domain)
            self.code_index[key] = cid
        return self.code_index[key]

    def _with_extra_codes(self, codes: dict[str, list["CodeLink"]], extras: list[int]):
        """Add the extra concepts' own codes to the front of their vocabularies."""
        if not extras:
            return codes
        codes = {v: list(links) for v, links in codes.items()}
        for cid in reversed(extras):
            vocab, code, name, _ = self.concepts[cid]
            billable = bool(self.db.query("SELECT is_billable FROM concept WHERE concept_id = ?", (cid,))[0][0])
            links = [l for l in codes.get(vocab, []) if l.code != code]
            codes[vocab] = [CodeLink(vocab, code, name, billable, 1.0), *links]
        return codes

    def check_lexicon(self) -> list[str]:
        """Lexicon problems against this database: patterns whose targets never resolve, and for
        body-part patterns, how many parts they work for. ("fracture of ear" not resolving is fine.)"""
        report = []
        if not self.lexicon:
            return report
        seen = set()
        for p in self.lexicon.patterns:
            if (p.text, p.targets) in seen:
                continue
            seen.add((p.text, p.targets))
            if "{" not in p.targets:
                if not self._resolve_match(LexiconMatch(1, p, None)):
                    report.append(f"UNRESOLVED {p.text!r}: none of {p.targets!r} is a known term or code")
                continue
            words = sorted({part.part: word for word, part in self.lexicon.parts.items()}.items())
            working = [part for part, word in words
                       if self._resolve_match(LexiconMatch(1, p, self.lexicon.parts[word]))]
            status = "UNRESOLVED" if not working else "ok"
            report.append(f"{status} {p.text!r}: {len(working)}/{len(words)} parts ({', '.join(working)})")
        for kind, prefix, codes in self._curated_context_codes():
            for vocab, code in codes.items():
                if self._code_concept(f"{vocab.lower()}:{code}") is None:
                    report.append(f"UNRESOLVED context_codes.csv {kind} {prefix}: {vocab} {code} is not in the database")
        return report

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
        if " " not in key and key in self.generic_words:
            # only concepts actually named by the word, not ones listing it as a side synonym
            entries = [e for e in entries if normalize_term(self.concepts[e[0]][2]) == key]
        return entries or None

    def _candidates(self, entries) -> list[Candidate]:
        best: dict[int, Candidate] = {}
        for cid, term_type, term in entries:
            vocab, code, name, domain = self.concepts[cid]
            score = TERM_WEIGHT.get(term_type, 0.8) * DOMAIN_WEIGHT.get(domain, 0.5)
            if cid not in self.linked:
                score *= UNLINKED
            if vocab in CODE_VOCABULARIES and MANIFESTATION.search(name):
                score *= 0.5
            if not primary_sense(name, term):
                score *= SECONDARY_SENSE
            if cid not in best or score > best[cid].score:
                best[cid] = Candidate(cid, vocab, code, name, domain, term, term_type, round(score, 3))
        ranked = sorted(best.values(), key=lambda c: (-c.score, c.vocabulary != "UMLS", c.code))
        return ranked[:self.max_candidates]

    # -- codes ---------------------------------------------------------------------------

    def codes(self, concept_id: int, per_vocabulary: int = 3,
              min_confidence: float = MIN_CODE_CONFIDENCE) -> dict[str, list[CodeLink]]:
        """Best codes per vocabulary for a concept: its own code if it is one, then its links
        (not chapters or blocks) with at least `min_confidence`."""
        cache_key = (concept_id, per_vocabulary, min_confidence)
        if cache_key in self._codes_cache:
            return self._codes_cache[cache_key]
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
            rows = [(own[0], own[1], own[2], billable, 0.5 if MANIFESTATION.search(own[2]) else 1.0), *rows]
        best: dict[tuple[str, str], CodeLink] = {}
        for vocab, code, name, billable, confidence in rows:
            link = CodeLink(vocab, code, name, bool(billable), round(float(confidence), 3))
            if (vocab, code) not in best or link.confidence > best[(vocab, code)].confidence:
                best[(vocab, code)] = link
        out: dict[str, list[CodeLink]] = {}
        ranked = sorted((l for l in best.values() if l.confidence >= min_confidence),
                        key=lambda l: (-(l.confidence - (0 if l.billable else NON_BILLABLE)),
                                       "unspecified" not in l.name.lower(), l.code))
        for link in ranked:
            if len(out.setdefault(link.vocabulary, [])) < per_vocabulary:
                out[link.vocabulary].append(link)
        self._codes_cache[cache_key] = out
        return out


_FIRST_WORDS: dict[int, set[str]] = {}


def _phrase_at(tokens, i: int, phrases: set[str], max_words: int = 4) -> int:
    """Length in words of the longest phrase from `phrases` starting at token i (0 if none);
    a phrase can't run across punctuation."""
    first = _FIRST_WORDS.get(id(phrases))
    if first is None:
        first = _FIRST_WORDS[id(phrases)] = {p.split(" ", 1)[0] for p in phrases}
    if tokens[i][2] not in first:
        return 0
    for n in range(min(max_words, len(tokens) - i), 0, -1):
        if any(tokens[k][3] for k in range(i + 1, i + n)):
            continue
        if " ".join(tok[2] for tok in tokens[i:i + n]) in phrases:
            return n
    return 0


def _chapter(cand: "Candidate") -> str | None:
    """ICD-10-CM chapter of a candidate's best ICD-10-CM code ("9" for I21.9), "procedure" for a
    procedure with no ICD-10-CM code, else None."""
    links = cand.codes.get("ICD10CM") or ([] if cand.vocabulary != "ICD10CM" else
                                          [CodeLink("ICD10CM", cand.code, cand.name, True, 1.0)])
    if not links:
        return "procedure" if cand.domain == "procedure" else None
    code = links[0].code
    letter, digits = code[0], code[1:3]
    number = int(digits) if digits.isdigit() else 0
    if letter == "D":
        return "2" if number < 50 else "3"
    if letter == "H":
        return "7" if number < 60 else "8"
    return {"A": "1", "B": "1", "C": "2", "E": "4", "F": "5", "G": "6", "I": "9", "J": "10", "K": "11", "L": "12",
            "M": "13", "N": "14", "O": "15", "P": "16", "Q": "17", "R": "18", "S": "19", "T": "19", "V": "20",
            "W": "20", "X": "20", "Y": "20", "Z": "21"}.get(letter)


def _is_cue(words: list[str]) -> bool:
    """Whether these words are a context cue ("family history of", "no family history of")."""
    phrase = " ".join(words)
    return phrase in ALL_CUES or (len(words) > 1 and words[0] in ("no", "not") and " ".join(words[1:]) in ALL_CUES)


def _mark_context(tokens, mentions) -> None:
    """ConText-style: set assertion (negated / hypothetical / uncertain), experiencer (family)
    and temporality (history) from cues before or after each mention, within its clause."""
    if not mentions:
        return
    inside = {k for first, last, _ in mentions for k in range(first, last + 1)}   # "not eating" is a mention
    for field_name, value, before, after, reach, scope_end in CONTEXTS:
        cues: list[tuple[int, str]] = []   # (token index where the scope starts, cue text)
        i = 0
        while i < len(tokens):
            if i in inside:
                i += 1
                continue
            if value == "negated" and (n := _phrase_at(tokens, i, PSEUDO_NEGATION)):
                i += n
                continue
            if n := _phrase_at(tokens, i, before):
                cues.append((i + n, " ".join(tok[2] for tok in tokens[i:i + n])))
                i += n
                continue
            i += 1
        for first, last, mention in mentions:
            if getattr(mention, field_name) is not None:
                continue   # an earlier (higher priority) value already applies
            found = None
            for scope_start, cue in cues:
                if scope_start > first or first - scope_start > reach:
                    continue
                # a clause break between cue and mention, or a scope-ending word, ends the scope
                blocked = (any(tokens[k][4] for k in range(scope_start, first + 1))
                           or any(tokens[k][2] in scope_end for k in range(scope_start, first)))
                if not blocked:
                    found = cue
            if found is None:
                for k in range(last + 1, min(last + 1 + NEGATION_REACH_AFTER + 1, len(tokens))):
                    if tokens[k][4]:
                        break
                    if n := _phrase_at(tokens, k, after):
                        found = " ".join(tok[2] for tok in tokens[k:k + n])
                        break
                    if (value == "history" and tokens[k][2] == "in" and k + 1 < len(tokens)
                            and YEAR.match(tokens[k + 1][2]) and not tokens[k + 1][3]):
                        found = f"in {tokens[k + 1][2]}"   # "had a stroke in 2019"
                        break
            if found is not None:
                setattr(mention, field_name, value)
                mention.cues.append(f"{value}: {found}")


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
            (ICD-10-CM, ICD-9-CM or NS); '*' accepts any candidate. Context goes in brackets:
            'stroke[family]=Z82.3', 'MI[history]=I25.2', 'pneumonia[uncertain]=*'; '-phrase' is
            short for 'phrase[negated]'. The mention's context must match exactly (for a negated
            phrase, only the negation is checked).
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
            tags = set()
            if phrase.startswith("-"):
                tags.add("negated")
                phrase = phrase[1:].strip()
            if m_tags := re.search(r"\[([^]]*)\]\s*$", phrase):
                tags |= {tag.strip() for tag in m_tags.group(1).split(",") if tag.strip()}
                phrase = phrase[:m_tags.start()].strip()
            expected += 1
            hit = None
            for idx, m in enumerate(mentions):
                context = {c for c in (m.assertion, m.experiencer, m.temporality) if c}
                same_context = ("negated" in context) if "negated" in tags else context == tags
                if (phrase in m.text.lower() or m.text.lower() in phrase) and same_context:
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
                context = ",".join(c for c in (m.assertion, m.experiencer, m.temporality) if c)
                flag = f" <{context}>" if context else ""
                print(f"   [{m.text}]{flag} {m.best.name} ({m.best.domain}, {m.best.score}) {codes}")
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
