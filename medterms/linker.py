"""Link UMLS concepts to diagnosis, procedure and billing codes by matching names.

UMLS attaches lay terms ("heart attack", "shingles") to general concepts (Myocardial
infarction, Herpes zoster), while billable codes sit on narrower concepts ("Acute myocardial
infarction, unspecified site"), and the Level 0 subset has no ICD-10-CM atoms at all. So most
lay concepts never reach a code through UMLS alone. This step adds approximate links:

  1. name match   a concept's names (clinical, lay and preferred; not abbreviations) against a
                  target code's names: its title, ICD-10-CM inclusion terms and Alphabetic
                  Index entries, NS fee descriptions. Exact normalized matches score highest;
                  then matches on the set of words, ignoring order, filler words ("NOS",
                  "unspecified", "of"), text in parentheses and spelling variants (disk/disc,
                  haemorrhage/hemorrhage). A target whose extra words were only "unspecified" or
                  "NOS" scores higher: it's the general code for that concept. Concepts with no
                  such match get a weaker "contained" match: all their words appear in a target
                  name with at most two more (slipped disc -> "disc displacement, lumbar region"),
                  unless the extra words change the context (pregnancy, newborn, postprocedural);
                  and a "broader" match: a target named by all but one of their words
                  (infective cystitis -> "Cystitis").
  2. via GEM      a concept linked to an ICD-9-CM diagnosis also reaches that code's ICD-10-CM
                  mappings, and a concept linked to ICD-10-CM reaches the ICD-9-CM codes that
                  map to it (so a lay term reaches the ICD-9 code NS claims need).

The 2018 GEMs only cover billable ICD-10-CM codes that existed in 2018, so first `derive_gems`
fills two holes from the ICD-10-CM hierarchy, written as source 'GEM_DERIVED':
  * a category takes the ICD-9-CM codes its billable descendants map to, when at least 20% of
    them agree (S72.00 "Fracture of unspecified part of neck of femur" -> 820.8, via S72.009A)
  * a code added after 2018 with nothing below it borrows the mappings of its "unspecified"
    sibling (F32.A "Depression, unspecified", new in 2022 -> 311 and 296.20, via F32.9)

Every link, including UMLS's own concept -> code links, is scored down when the code's title
adds context the concept doesn't have ("Complications ..., hypertension" and "Postprocedural
hypertension" for Hypertensive disease: x0.5), or when the code is a manifestation code that
ICD doesn't allow as a primary diagnosis ("... in diseases classified elsewhere": x0.5).

Links are written as 'maps_to_approx' (concept -> code) and 'approx_mapped_from'
(code -> concept) with a confidence between 0 and 1 and source 'MATCH/<how>', so
`medterms lookup --to` follows them and a rerun replaces them.
"""

import re
from collections import Counter, defaultdict

from medterms.db import Database
from medterms.lookup import MANIFESTATION
from medterms.normalize import normalize_term

SOURCE_PREFIX = "MATCH/"

# What each target vocabulary's codes are about, and which concept domains may link to them.
TARGETS = {
    "ICD10CM": {"condition", "symptom", "observation", "other"},
    "ICD9CM": {"condition", "symptom", "observation", "other", "procedure"},
    "NS_MSI": {"procedure", "other", "observation"},
}



def compatible(concept_domain: str, code_domain: str) -> bool:
    """Procedure concepts link to procedure codes, everything else to diagnosis codes;
    concepts UMLS couldn't classify ('other') may link to either."""
    return concept_domain == "other" or (concept_domain == "procedure") == (code_domain == "procedure")


# How much a matched target name counts, by its term type.
TARGET_WEIGHT = {"preferred": 1.0, "clinical": 0.9, "index": 0.8}
# How much the concept's matching name counts, by its term type.
SOURCE_WEIGHT = {"clinical": 1.0, "preferred": 1.0, "lay": 0.9}
EXACT, UNSPECIFIED, WORDS, CONTAINED, BROADER = 1.0, 0.95, 0.85, 0.6, 0.55
GEM_EXACT, GEM_APPROX = 0.8, 0.7

# Words ignored in word keys. "Other" and "type" are not filler: in ICD they mark a separate code.
UNSPECIFIED_WORDS = {"nos", "unspecified", "unspec"}
FILLER = UNSPECIFIED_WORDS | {"nec", "site", "of", "the", "a", "an", "and", "or", "in", "to", "by", "for", "on",
                              "disorder", "finding", "condition", "specified"}
MAX_EXTRA_WORDS = 2
CONTEXT_PENALTY, MANIFESTATION_PENALTY = 0.5, 0.5

# A broader match that leaves a single word must leave a disease name ("infective cystitis" ->
# "cystitis"), not a generic word ("cerebrovascular accident" -> "accident").
DISEASE_WORD = re.compile(r"(itis|osis|emia|oma|pathy|algia|plegia|trophy|ectasia|iasis|ism|rrhea|rrhage|cele)$"
                          r"|^(diabete|cancer|asthma|gout|hernia|ulcer|eczema|psoriasi)$")
# A contained match may not add words that put the code in another context
# ("bladder infection" is not "infection of bladder in pregnancy").
CONTEXT_WORDS = {"aftercare", "subsequent", "late", "pregnancy", "pregnant", "delivery", "puerperium", "puerperal", "childbirth", "newborn", "perinatal",
                 "neonatal", "fetu", "fetal", "maternal", "postpartum", "postprocedural", "intraoperative",
                 "postoperative", "complicating", "complication", "congenital", "following", "abortion",
                 "personal", "history", "family", "screening", "sequela", "encounter", "war", "terrorism"}
SPELLING = [(re.compile(p), r) for p, r in [
    (r"^disk", "disc"), (r"haem", "hem"), (r"^oesoph", "esoph"), (r"^oedem", "edem"), (r"aemia", "emia"),
    (r"tumour", "tumor"), (r"^paed", "ped"), (r"^orthopaed", "orthoped"), (r"^foet", "fet"),
    (r"^diarrhoea", "diarrhea"), (r"colour", "color"), (r"^gynaec", "gynec"), (r"^leukaem", "leukem"),
]]
# A word-set key shared by more target codes than this is too vague to link on ("pain").
MAX_CODES_PER_KEY = 6


def _token(word: str) -> str:
    for pattern, repl in SPELLING:
        word = pattern.sub(repl, word)
    # crude plural folding, applied the same way on both sides
    if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    return word


def word_key(term: str) -> frozenset[str] | None:
    """Order-insensitive key: 'Myocardial infarction (acute) NOS' -> {infarction, myocardial}."""
    return _words(term)[0]


def _words(term: str) -> tuple[frozenset[str] | None, bool]:
    """(word key, whether an 'unspecified'/'NOS' word was dropped)."""
    text = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", term)
    # "Adenoidectomy without tonsillectomy": what follows "without" isn't what the code is about
    text = re.sub(r"\b(without|except|excluding|other than)\b[^,;]*", " ", text, flags=re.I)
    raw = {_token(w) for w in normalize_term(text).split()}
    words = raw - FILLER
    if not words or (len(words) == 1 and len(next(iter(words))) < 4):
        return None, False
    return frozenset(words), bool(raw & UNSPECIFIED_WORDS)


def _target_index(db: Database, vocab: str):
    """exact normalized term -> {code_id: weight}; word key -> {code_id: (weight, unspecified)};
    word -> keys containing it (for contained matches)."""
    exact: dict[str, dict[int, float]] = defaultdict(dict)
    words: dict[frozenset, dict[int, tuple[float, bool]]] = defaultdict(dict)
    rows = db.query(
        "SELECT c.concept_id, s.term, s.term_normalized, s.term_type FROM concept_synonym s "
        "JOIN concept c ON c.concept_id = s.concept_id WHERE c.vocabulary_id = ? AND c.valid_end IS NULL", (vocab,))
    for cid, term, norm, term_type in rows:
        weight = TARGET_WEIGHT.get(term_type, 0.8)
        if weight > exact[norm].get(cid, 0):
            exact[norm][cid] = weight
        key, unspecified = _words(term)
        if key is not None and (weight, unspecified) > words[key].get(cid, (0, False)):
            words[key][cid] = (weight, unspecified)
    words = {k: v for k, v in words.items() if len(v) <= MAX_CODES_PER_KEY}
    postings: dict[str, set[frozenset]] = defaultdict(set)
    for key in words:
        for word in key:
            postings[word].add(key)
    return exact, words, postings


def _contained(key: frozenset, postings) -> list[frozenset]:
    """Target keys holding every word of `key` plus at most MAX_EXTRA_WORDS more."""
    lists = sorted((postings.get(w, set()) for w in key), key=len)
    if not lists or not lists[0]:
        return []
    found = set(lists[0]).intersection(*lists[1:])
    return [k for k in found if len(k) - len(key) <= MAX_EXTRA_WORDS and not (k - key) & CONTEXT_WORDS]


def _concept_terms(db: Database):
    """(cui concept_id, domain, term, normalized, term_type) for UMLS concepts, abbreviations excluded."""
    return db.query(
        "SELECT c.concept_id, c.domain, s.term, s.term_normalized, s.term_type FROM concept_synonym s "
        "JOIN concept c ON c.concept_id = s.concept_id "
        "WHERE c.vocabulary_id = 'UMLS' AND c.valid_end IS NULL AND s.term_type IN ('clinical', 'preferred', 'lay')")


DERIVED_SOURCE = "GEM_DERIVED"
DERIVED_MIN_SHARE = 0.2      # a category takes an ICD-9 code only if this share of its mapped descendants do
DERIVED_SIBLING = 0.6        # confidence of a mapping borrowed from an "unspecified" sibling
SUBSEQUENT = re.compile(r"subsequent encounter|sequela")


def derive_gems(db: Database) -> dict[str, int]:
    """Write ICD-10-CM <-> ICD-9-CM mappings the GEMs lack, derived from the ICD-10-CM hierarchy."""
    db.execute("DELETE FROM concept_relationship WHERE source = ?", (DERIVED_SOURCE,))
    info = {cid: (name, concept_class) for cid, name, concept_class in db.query(
        "SELECT concept_id, name, concept_class FROM concept WHERE vocabulary_id = 'ICD10CM'")}
    icd9 = {cid for (cid,) in db.query("SELECT concept_id FROM concept WHERE vocabulary_id = 'ICD9CM'")}
    own: dict[int, set[int]] = defaultdict(set)
    for src, dst in db.query(
            "SELECT concept_id_1, concept_id_2 FROM concept_relationship WHERE source = 'CMS_GEM' "
            "AND relationship_id IN ('mapped_from', 'approx_mapped_from')"):
        if src in info and dst in icd9:
            own[src].add(dst)
    children: dict[int, list[int]] = defaultdict(list)
    parent: dict[int, int] = {}
    for child, par in db.query(
            "SELECT concept_id_1, concept_id_2 FROM concept_relationship WHERE relationship_id = 'is_a' AND source = 'ICD10CM'"):
        children[par].append(child)
        parent[child] = par

    memo: dict[int, tuple] = {}

    def weight(cid: int) -> float:
        """How much a billable code counts when its mappings are pooled for a category: a fracture
        category has far more subsequent-encounter and sequela codes (-> ICD-9 aftercare and late
        effect codes) than initial ones, so those don't count, and open fractures count half."""
        title = info[cid][0].lower()
        if SUBSEQUENT.search(title):
            return 0.0
        return 0.5 if "open fracture" in title else 1.0

    def below(cid: int):
        """(Counter of ICD-9 targets, weight of mapped billable codes) at or under cid."""
        if cid not in memo:
            if cid in own:
                w = weight(cid)
                memo[cid] = (Counter({target: w for target in own[cid]}), w)
            else:
                total, n = Counter(), 0
                for child in children.get(cid, ()):
                    c, k = below(child)
                    total.update(c)
                    n += k
                memo[cid] = (total, n)
        return memo[cid]

    derived: dict[tuple[int, int], float] = {}
    for cid, (name, concept_class) in info.items():
        if cid in own or concept_class in ("chapter", "block"):
            continue
        counts, n = below(cid)
        if n > 0:
            for target, count in counts.most_common(5):
                share = count / n
                if share >= DERIVED_MIN_SHARE:
                    derived[(cid, target)] = round(GEM_APPROX * (0.5 + 0.5 * share), 3)
        elif cid in parent:
            for sibling in children[parent[cid]]:
                if sibling != cid and sibling in own and "unspecified" in info[sibling][0].lower():
                    for target in own[sibling]:
                        derived[(cid, target)] = DERIVED_SIBLING
    rows = []
    for (icd10, target), confidence in derived.items():
        rows.append((icd10, target, "approx_mapped_from", confidence, DERIVED_SOURCE))
        rows.append((target, icd10, "maps_to_approx", confidence, DERIVED_SOURCE))
    db.executemany(
        "INSERT INTO concept_relationship (concept_id_1, concept_id_2, relationship_id, confidence, source) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (concept_id_1, concept_id_2, relationship_id) DO NOTHING", rows)
    return {"derived_gem": len(derived), "derived_gem_codes": len({icd10 for icd10, _ in derived})}


def link(db: Database, targets: list[str] | None = None) -> dict[str, int]:
    targets = targets or [v for v in TARGETS if db.query("SELECT 1 FROM concept WHERE vocabulary_id = ? LIMIT 1", (v,))]
    db.execute("DELETE FROM concept_relationship WHERE source LIKE ?", (SOURCE_PREFIX + "%",))
    derived_stats = derive_gems(db) if db.query("SELECT 1 FROM concept WHERE vocabulary_id = 'ICD9CM' LIMIT 1") else {}
    terms = _concept_terms(db)
    best: dict[tuple[int, int], tuple[float, str]] = {}
    concept_domain = {cui: domain for cui, domain, *_ in terms}
    code_domain = dict(db.query(
        "SELECT concept_id, domain FROM concept WHERE vocabulary_id IN (%s)" % ",".join("?" * len(TARGETS)),
        list(TARGETS)))

    # words each concept is ever called, and each target code's title, for the context penalty
    # (not counting ICD's own titles that UMLS attaches to the concept, or every code would
    # look like it fits: UMLS puts "Complications ..., hypertension" on Hypertensive disease)
    concept_words: dict[int, set[str]] = defaultdict(set)
    for cui, norm in db.query(
            "SELECT s.concept_id, s.term_normalized FROM concept_synonym s JOIN concept c ON c.concept_id = s.concept_id "
            "WHERE c.vocabulary_id = 'UMLS' AND s.source NOT IN ('UMLS/ICD9CM', 'UMLS/ICD10CM')"):
        concept_words[cui].update(_token(w) for w in norm.split())
    code_title = dict(db.query(
        "SELECT concept_id, name FROM concept WHERE vocabulary_id IN (%s)" % ",".join("?" * len(TARGETS)),
        list(TARGETS)))
    penalties: dict[tuple[int, int], float] = {}

    def penalty(cui: int, code: int) -> float:
        if (cui, code) not in penalties:
            title = code_title.get(code, "")
            # "Zoster without complications" adds no complication
            kept = re.sub(r"\b(without|except|excluding|other than)\b[^,;]*", " ", title, flags=re.I)
            words = {_token(w) for w in normalize_term(kept).split()}
            p = CONTEXT_PENALTY if (words & CONTEXT_WORDS) - concept_words.get(cui, set()) else 1.0
            if MANIFESTATION.search(title):
                p *= MANIFESTATION_PENALTY
            penalties[(cui, code)] = p
        return penalties[(cui, code)]

    def offer(cui: int, code: int, score: float, how: str):
        if not compatible(concept_domain.get(cui, "other"), code_domain.get(code, "condition")):
            return
        score *= penalty(cui, code)
        if score > best.get((cui, code), (0, ""))[0]:
            best[(cui, code)] = (round(score, 3), how)

    # 1. name matches; then contained matches for concepts that got nothing from this vocabulary
    for vocab in targets:
        exact, words, postings = _target_index(db, vocab)
        domains = TARGETS[vocab]
        matched: set[int] = set()
        unmatched: dict[int, list[tuple[frozenset, float]]] = defaultdict(list)
        for cui, domain, term, norm, term_type in terms:
            if domain not in domains:
                continue
            weight = SOURCE_WEIGHT[term_type]
            for code, w in exact.get(norm, {}).items():
                offer(cui, code, EXACT * w * weight, "name")
                matched.add(cui)
            key, _ = _words(term)
            if key is None:
                continue
            for code, (w, unspecified) in words.get(key, {}).items():
                offer(cui, code, (UNSPECIFIED if unspecified else WORDS) * w * weight, "words")
                matched.add(cui)
            if len(key) >= 2:
                unmatched[cui].append((key, weight))
        for cui, keys in unmatched.items():
            if cui in matched:
                continue
            for key, weight in keys:
                for target_key in _contained(key, postings):
                    extra = len(target_key) - len(key)
                    for code, (w, _) in words[target_key].items():
                        offer(cui, code, CONTAINED * w * weight * (0.9 ** extra), "contained")
                for dropped in key:
                    broader = key - {dropped}
                    if len(broader) == 1 and not DISEASE_WORD.search(next(iter(broader))):
                        continue  # "cerebrovascular accident" -> "accident" is a different thing
                    for code, (w, _) in words.get(broader, {}).items():
                        offer(cui, code, BROADER * w * weight, "broader")

    # direct UMLS links count as certain, and seed the GEM step
    direct = db.query(
        "SELECT r.concept_id_1, r.concept_id_2 FROM concept_relationship r "
        "JOIN concept c ON c.concept_id = r.concept_id_1 AND c.vocabulary_id = 'UMLS' "
        "WHERE r.relationship_id = 'umls_cui_of'")
    certain = {(cui, code): penalty(cui, code) for cui, code in direct}

    # 2. across the GEMs, in both directions
    vocab_of = dict(db.query("SELECT concept_id, vocabulary_id FROM concept WHERE vocabulary_id IN ('ICD9CM', 'ICD10CM')"))
    gem = defaultdict(list)
    for src, dst, rel, confidence in db.query(
            "SELECT concept_id_1, concept_id_2, relationship_id, confidence FROM concept_relationship "
            "WHERE source IN ('CMS_GEM', ?)", (DERIVED_SOURCE,)):
        gem[src].append((dst, float(confidence) if confidence is not None
                         else GEM_EXACT if rel in ("maps_to", "mapped_from") else GEM_APPROX))
    linked = {**{pair: score for pair, (score, _) in best.items()}, **certain}
    for (cui, code), score in list(linked.items()):
        if vocab_of.get(code) is None:
            continue
        for other, factor in gem.get(code, ()):
            if (cui, other) not in certain:
                offer(cui, other, score * factor, "gem")

    rows = []
    stats = defaultdict(int)
    target_vocab = dict(db.query(
        "SELECT concept_id, vocabulary_id FROM concept WHERE vocabulary_id IN (%s)" % ",".join("?" * len(TARGETS)),
        list(TARGETS)))
    for (cui, code), (score, how) in best.items():
        if (cui, code) in certain:
            continue
        source = SOURCE_PREFIX + how
        rows.append((cui, code, "maps_to_approx", score, source))
        rows.append((code, cui, "approx_mapped_from", score, source))
        stats[f"{target_vocab.get(code, '?').lower()}_{how}"] += 1
    db.executemany(
        "INSERT INTO concept_relationship (concept_id_1, concept_id_2, relationship_id, confidence, source) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (concept_id_1, concept_id_2, relationship_id) DO UPDATE SET "
        "confidence = excluded.confidence, source = excluded.source",
        rows,
    )
    # UMLS's own links keep their rows; they only get a confidence when penalised
    db.executemany(
        "UPDATE concept_relationship SET confidence = ? WHERE concept_id_1 = ? AND concept_id_2 = ? "
        "AND relationship_id = 'umls_cui_of'",
        [(score if score < 1 else None, cui, code) for (cui, code), score in certain.items()])
    stats["umls_links_penalised"] = sum(1 for score in certain.values() if score < 1)
    db.commit()
    stats.update(derived_stats)
    stats["links"] = len(rows) // 2
    stats["concepts_linked"] = len({cui for cui, _ in best})
    return dict(sorted(stats.items()))
