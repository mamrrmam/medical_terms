"""Curated lay phrases and body-part patterns for the annotator (medterms/lexicon_data/*.csv).

Vocabularies miss how people talk: "can't put weight on it", "broke his wrist", "my knee
hurts", "quit smoking". The lexicon maps such wording to terms the dictionary already knows
("difficulty walking", "fracture of wrist", "knee pain") or straight to codes, so nothing in it
depends on one UMLS release. It is plain data: add a line to patterns.csv or body_parts.csv
and check it with `medterms annotate --check-lexicon`.

Pattern syntax (patterns.csv):

  words            matched after normalization ("can't" is "cant")
  [a|b|c]          one of the alternatives; each may be several words or contain slots
  x?  [a|b]?       optional word or group
  <part>           a body part from body_parts.csv ("knee", "lower back", "tummy")
  <det>            up to three of my/his/her/their/the/left/right/both/...
  <obj>            me/him/her/them/you/us

Targets: '|'-separated; each that resolves becomes a candidate, and the one reaching the most
code sets (ICD-10-CM, ICD-9-CM, NS) comes first, earlier targets winning ties. Each is a term looked up
in the dictionary, with {part}, {adj} and {pain} filled in from the matched body part (a target
naming a field the part doesn't have is skipped), or a code "vocab:code" ("icd10cm:Z87.891").
"a+b" adds b's codes to a's candidate ("icd10cm:Z87.891+icd9cm:V15.82"); "-" suppresses the
phrase, so nothing is tagged there.
"""

import csv
import itertools
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from medterms.normalize import normalize_term

DET = {"my", "his", "her", "their", "your", "our", "its", "the", "a", "an", "both", "left", "right", "this",
       "that", "these", "those", "one", "some", "either"}
OBJ = {"me", "him", "her", "them", "you", "us"}
MAX_DET = 3
CODE_SPEC = re.compile(r"^[a-z0-9_]+:[A-Za-z0-9.\-]+$")


@dataclass
class BodyPart:
    part: str
    adjective: str = ""
    pain: str = ""


@dataclass
class Pattern:
    text: str          # as written in the file
    targets: str
    note: str
    sequence: tuple    # one expansion: ("w", word) / ("slot", name) items


@dataclass
class LexiconMatch:
    length: int            # tokens consumed
    pattern: Pattern
    part: BodyPart | None

    def targets(self) -> list[str]:
        """The pattern's targets with body part fields filled in, in order."""
        out = []
        for target in self.pattern.targets.split("|"):
            target = target.strip()
            fields = set(re.findall(r"\{(\w+)\}", target))
            if fields:
                values = {"part": self.part.part, "adj": self.part.adjective, "pain": self.part.pain} if self.part else {}
                if any(not values.get(f) for f in fields):
                    continue
                target = target.format(**values)
            out.append(target)
        return out


def _split_top(text: str, sep: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for ch in text:
        depth += ch == "["
        depth -= ch == "]"
        if ch == sep and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    return parts + [current]


def expand(pattern: str) -> list[tuple]:
    """All token sequences a pattern stands for: '[cant|unable to] walk' -> 2 sequences."""
    options_per_element = []
    pos, text = 0, pattern.strip()
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        if text[pos] == "[":
            depth, end = 0, pos
            while end < len(text):
                depth += text[end] == "["
                depth -= text[end] == "]"
                if depth == 0:
                    break
                end += 1
            inner = text[pos + 1:end]
            options = [seq for alt in _split_top(inner, "|") for seq in expand(alt)]
            pos = end + 1
        else:
            end = pos
            while end < len(text) and not text[end].isspace() and text[end] != "[":
                end += 1
            word = text[pos:end]
            pos = end
            optional_word = word.endswith("?")
            word = word.rstrip("?")
            if word.startswith("<") and word.endswith(">"):
                options = [(("slot", word[1:-1]),)]
            else:
                options = [tuple(("w", w) for w in normalize_term(word).split())]
            if optional_word:
                options.append(())
            options_per_element.append(options)
            continue
        if pos < len(text) and text[pos] == "?":
            options.append(())
            pos += 1
        options_per_element.append(options)
    return [tuple(itertools.chain.from_iterable(combo)) for combo in itertools.product(*options_per_element)]


class Lexicon:
    def __init__(self, patterns: list[Pattern], parts: dict[str, BodyPart]):
        self.patterns = patterns
        self.parts = parts
        self.by_word: dict[str, list[Pattern]] = {}
        self.by_slot: list[Pattern] = []
        for p in patterns:
            if not p.sequence:
                continue
            kind, value = p.sequence[0]
            if kind == "w":
                self.by_word.setdefault(value, []).append(p)
            else:
                self.by_slot.append(p)

    @classmethod
    def load(cls, patterns_path: Path | None = None, parts_path: Path | None = None) -> "Lexicon":
        """The bundled lexicon, or the files given."""
        base = resources.files("medterms").joinpath("lexicon_data")
        patterns_path = patterns_path or Path(str(base.joinpath("patterns.csv")))
        parts_path = parts_path or Path(str(base.joinpath("body_parts.csv")))
        parts = {}
        for row in _rows(parts_path):
            parts[normalize_term(row["word"])] = BodyPart(row["part"].strip(), (row.get("adjective") or "").strip(),
                                                         (row.get("pain") or "").strip())
        patterns = []
        for row in _rows(patterns_path):
            text = row["pattern"].strip()
            for seq in dict.fromkeys(expand(text)):
                patterns.append(Pattern(text, (row.get("targets") or "").strip(), (row.get("note") or "").strip(), seq))
        return cls(patterns, parts)

    def match(self, tokens, i: int) -> LexiconMatch | None:
        """Longest pattern match starting at token i (earlier lines win ties); tokens are the
        annotator's (start, end, normalized word, punctuation before, clause end before)."""
        best = None
        for p in itertools.chain(self.by_word.get(tokens[i][2], ()), self.by_slot):
            found = self._match(p.sequence, tokens, i)
            if found and (best is None or found[0] > best.length):
                best = LexiconMatch(found[0], p, found[1])
        return best

    def _match(self, sequence, tokens, i):
        j, part = i, None

        def usable(k):
            return k < len(tokens) and (k == i or not tokens[k][3])

        for kind, value in sequence:
            if kind == "w":
                if not usable(j) or tokens[j][2] != value:
                    return None
                j += 1
            elif value == "det":
                n = 0
                while n < MAX_DET and usable(j) and tokens[j][2] in DET:
                    j, n = j + 1, n + 1
            elif value == "obj":
                if not usable(j) or tokens[j][2] not in OBJ:
                    return None
                j += 1
            elif value == "part":
                for n in (2, 1):
                    if all(usable(k) for k in range(j, j + n)):
                        key = " ".join(tokens[k][2] for k in range(j, j + n))
                        if key in self.parts:
                            part = self.parts[key]
                            j += n
                            break
                else:
                    return None
            else:
                raise ValueError(f"unknown slot <{value}>")
        return (j - i, part) if j > i else None


def _rows(path: Path):
    with open(path, newline="", encoding="utf-8") as f:
        lines = [line for line in f if line.strip() and not line.startswith("#")]
    return list(csv.DictReader(lines))
