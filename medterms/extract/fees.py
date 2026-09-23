"""Extract fee-schedule rows, or a plain code list, from a PDF, driven by a per-payer profile.

Provincial fee schedules are mostly laid out as columns: a fee code at the left, a
description that may wrap over several lines, then modifier lines each with their own
fee, and one or more fee columns at the right. A profile (TOML, see medterms/profiles/)
says how to recognise each part:

  payer            payer_id the output is for, e.g. "NS_MSI"
  kind             "fees" (default) or "codes" for a code list such as a modifier appendix
  pages            page ranges to read, e.g. "216-534" (optional)
  record           regex a record's first line starts with; group `code` is the code (else the
                   whole match), optional group `category` the payer's service category
  code_template    codes only: build the code from the record's groups, e.g. "{type}={value}"
  code_max_x       records must start left of this x (points), to ignore codes cited in prose
  columns          fees only: x range (points) of each fee column, e.g. {units = [480, 535]}.
                   Names: 'units', 'amount', 'anaesthesia_units' or 'extra_*' (kept in the CSV only).
                   A number printed with '$' in the units column is taken as an amount.
  value            regex a column's text must fullmatch to count as a fee on a line without a
                   dot leader (default: 12.5, $150, 62+MU, 4+T, 35%)
  leader           regex for dot leaders, removed from words (default: 2+ dots, or a lone dot)
  modifier         regex for one modifier token, e.g. '\\(?[A-Z]{2}= ?[A-Z0-9]{4}\\*?\\)?'. A line
                   inside a record that starts with modifier tokens is a modifier row.
  heading          regex (fullmatch) for group headings printed above records (optional)
  heading_max_x    inside a record, headings must start left of this x (default: any x)
  page_section     regex with a (?P<section>...) group, searched on every line of a page, naming the
                   schedule section (specialty) the page belongs to, e.g. from the page footer
  skip             regexes for running headers, footers and page numbers
  effective_start  YYYY-MM-DD written on every row (optional; or pass --effective to the loader)

Fees printed with qualifiers ('62+MU', '30 per 15 min', 'Time Only') keep their number in
the numeric column and the printed text in fee_note, so nothing is lost.

Lines that belong to no record and match no skip pattern are listed in the report, so a
profile can be tuned until nothing important is left over. Nothing is guessed silently:
records without a fee are reported, not written.
"""

import csv
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from medterms.extract.pdftext import Line, Word

DEFAULT_VALUE = r"\$?\d[\d,]*(?:\.\d+)?(?:\s*\+\s*[A-Z]{1,2})?%?"
FEE_COLUMNS = {"units", "amount", "anaesthesia_units"}
FEE_OUTPUT = ["code", "description", "units", "amount", "anaesthesia_units", "fee_note", "modifier",
              "section", "category", "heading", "effective_start", "page"]
CODE_OUTPUT = ["code", "description", "page"]
NUMBER = re.compile(r"(\$)?\s*(\d[\d,]*(?:\.\d+)?)\s*(.*)")


@dataclass
class Profile:
    payer: str
    record: re.Pattern
    kind: str = "fees"
    code_template: str | None = None
    code_max_x: float | None = None
    columns: dict[str, tuple[float, float]] = field(default_factory=dict)
    value: re.Pattern = re.compile(DEFAULT_VALUE)
    leader: re.Pattern = re.compile(r"\.{2,}|^\.$")
    modifier: re.Pattern | None = None
    heading: re.Pattern | None = None
    heading_max_x: float | None = None
    page_section: re.Pattern | None = None
    skip: list[re.Pattern] = field(default_factory=list)
    pages: str | None = None
    effective_start: str | None = None

    @classmethod
    def load(cls, path: Path | str) -> "Profile":
        """Load a profile from a path, or by name from medterms/profiles/ ('ns_msi_fees')."""
        path = Path(path)
        if not path.exists() and path.suffix == "":
            path = Path(str(resources.files("medterms").joinpath("profiles", f"{path}.toml")))
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        kind = raw.get("kind", "fees")
        if kind not in ("fees", "codes"):
            raise ValueError(f"{path}: kind must be 'fees' or 'codes', not {kind!r}")
        columns = {name: tuple(rng) for name, rng in raw.get("columns", {}).items()}
        unknown = {c for c in columns if c not in FEE_COLUMNS and not c.startswith("extra_")}
        if unknown:
            raise ValueError(f"{path}: columns must be {sorted(FEE_COLUMNS)} or 'extra_*', not {sorted(unknown)}")
        if kind == "fees" and not columns:
            raise ValueError(f"{path}: a fees profile needs [columns]")
        compile_ = lambda key: re.compile(raw[key]) if raw.get(key) else None  # noqa: E731
        return cls(
            payer=raw["payer"],
            record=re.compile(raw["record"]),
            kind=kind,
            code_template=raw.get("code_template"),
            code_max_x=raw.get("code_max_x"),
            columns=columns,
            value=re.compile(raw.get("value", DEFAULT_VALUE)),
            leader=re.compile(raw.get("leader", r"\.{2,}|^\.$")),
            modifier=compile_("modifier"),
            heading=compile_("heading"),
            heading_max_x=raw.get("heading_max_x"),
            page_section=compile_("page_section"),
            skip=[re.compile(p) for p in raw.get("skip", [])],
            pages=raw.get("pages"),
            effective_start=raw.get("effective_start"),
        )


@dataclass
class Modifier:
    tokens: list[str]
    values: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class Record:
    code: str
    page: int
    x0: float
    section: str
    heading: str
    category: str = ""
    description: list[str] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)
    modifiers: list[Modifier] = field(default_factory=list)


@dataclass
class Result:
    rows: list[dict[str, str]]
    no_fee: list[Record]
    leftover: list[Line]
    duplicates: list[tuple[str, ...]]
    pages: Counter


def parse_fee(column: str, text: str) -> dict[str, str]:
    """'62+MU' -> units 62 plus a note; '$150' in the units column -> amount 150; 'Time Only' -> note only."""
    m = NUMBER.fullmatch(text)
    if m is None or m.group(3).startswith("%"):
        return {"fee_note": f"{column} {text}"}
    dollars, number, rest = m.groups()
    target = "amount" if dollars and column == "units" else column
    out = {target: number.replace(",", "")}
    if rest:
        out["fee_note"] = f"{column} {text}"
    return out


class _Extractor:
    def __init__(self, profile: Profile):
        self.p = profile
        self.value_min_x = min((lo for lo, _ in profile.columns.values()), default=None)

    def clean(self, line: Line) -> tuple[list[Word], bool]:
        """Words with dot leaders removed, and whether the line had a leader."""
        words, leader = [], False
        for w in line.words:
            text = self.p.leader.sub("", w.text)
            leader = leader or text != w.text
            if text:
                words.append(Word(w.x0, w.x1, text))
        return words, leader

    def split(self, words: list[Word], leader: bool) -> tuple[str, dict[str, str]]:
        """(text left of the fee columns, {column: printed fee})."""
        if self.value_min_x is None:
            return " ".join(w.text for w in words), {}
        left = [w for w in words if w.x0 < self.value_min_x]
        right = [w for w in words if w.x0 >= self.value_min_x]
        values: dict[str, list[str]] = {}
        stray = []
        for w in right:
            name = next((n for n, (lo, hi) in self.p.columns.items() if lo <= w.x0 <= hi), None)
            if name is None:
                stray.append(w)
            else:
                values.setdefault(name, []).append(w.text)
        joined = {n: " ".join(t) for n, t in values.items()}
        if not leader and not all(self.p.value.fullmatch(v) for v in joined.values()):
            return " ".join(w.text for w in words), {}  # prose that happens to reach the fee columns
        return " ".join(w.text for w in left + stray), joined

    def modifier_tokens(self, text: str) -> tuple[list[str], str]:
        """Leading modifier tokens of a line and the text after them."""
        tokens, pos = [], 0
        sep = re.compile(r"\s*[,;]?\s*")
        while self.p.modifier:
            start = sep.match(text, pos).end()
            m = self.p.modifier.match(text, start)
            if not m:
                break
            tokens.append(re.sub(r"^(\(?[A-Z]{2})\s?[=-]\s?", r"\1=", m.group(0)))
            pos = m.end()
        return tokens, text[pos:].strip(" ,;") if tokens else text


def _section_by_page(lines: list[Line], profile: Profile) -> dict[int, str]:
    sections: dict[int, str] = {}
    if profile.page_section:
        for line in lines:
            if m := profile.page_section.search(line.text):
                sections.setdefault(line.page, m.group("section").strip())
    return sections


def extract(lines: list[Line], profile: Profile) -> Result:
    if profile.kind == "codes":
        return extract_codes(lines, profile)
    x = _Extractor(profile)
    rows: list[dict[str, str]] = []
    no_fee: list[Record] = []
    leftover: list[Line] = []
    pages: Counter = Counter()
    sections = _section_by_page(lines, profile)
    heading, heading_line = "", None
    record: Record | None = None

    def flush():
        nonlocal record
        if record is None:
            return
        description = " ".join(record.description).strip()
        base = {"code": record.code, "effective_start": profile.effective_start or "", "section": record.section,
                "category": record.category, "heading": record.heading, "page": str(record.page)}

        def row(modifier: str, values: dict[str, str], extra: list[str]) -> dict[str, str]:
            fee: dict[str, str] = {}
            notes = []
            for column, text in values.items():
                parsed = parse_fee(column, text)
                if "fee_note" in parsed:
                    notes.append(parsed.pop("fee_note"))
                fee.update(parsed)
            text = " ".join([description, *extra]).strip()
            empty = dict.fromkeys(FEE_COLUMNS & set(profile.columns), "") | {"amount": ""}
            return {**base, "description": text, "modifier": modifier, **empty, **fee, "fee_note": "; ".join(notes)}

        emitted = 0
        if record.values:
            rows.append(row("", record.values, []))
            emitted += 1
        for mod in record.modifiers:
            if mod.values:
                rows.append(row(", ".join(mod.tokens), mod.values, mod.notes))
                emitted += 1
        if emitted:
            pages[record.page] += emitted
        else:
            no_fee.append(record)
        record = None

    previous_page = None
    for line in lines:
        section = sections.get(line.page, "")
        if line.page != previous_page:
            if record is not None and record.section != section:
                flush()
            previous_page = line.page
        text = line.text
        if any(p.search(text) for p in profile.skip) or (profile.page_section and profile.page_section.search(text)):
            continue

        words, leader = x.clean(line)
        if not words:
            continue
        left, values = x.split(words, leader)

        code_match = profile.record.match(left)
        if code_match and (profile.code_max_x is None or line.x0 <= profile.code_max_x):
            flush()
            groups = code_match.groupdict()
            code = groups.get("code") or code_match.group(0)
            rest = left[code_match.end():].strip()
            record = Record(code, line.page, line.x0, section, heading, groups.get("category") or "",
                            [rest] if rest else [], values)
            heading_line = None
            continue

        # headings right of heading_max_x (centred titles) count only between records
        if (profile.heading and profile.heading.fullmatch(left) and not values
                and (record is None or profile.heading_max_x is None or line.x0 <= profile.heading_max_x)):
            flush()
            # a heading that wraps onto a second line continues the one above it
            heading = f"{heading} {left}" if heading_line is not None and heading_line.page == line.page else left
            heading_line = line
            continue
        heading_line = None

        if record is not None and line.x0 > record.x0 + 1:
            tokens, rest = x.modifier_tokens(left)
            last = record.modifiers[-1] if record.modifiers else None
            if tokens and last is not None and not last.values:
                last.tokens.extend(tokens)  # modifier list wrapped onto a second line
                last.notes.extend([rest] if rest else [])
                last.values.update(values)
            elif tokens:
                record.modifiers.append(Modifier(tokens, values, [rest] if rest else []))
            elif last is not None:
                last.notes.append(left)
                for name, value in values.items():
                    last.values.setdefault(name, value)
            else:
                record.description.append(left)
                for name, value in values.items():
                    record.values.setdefault(name, value)
            continue

        flush()
        leftover.append(line)
    flush()

    return Result(rows, no_fee, leftover, _conflicts(rows, ("code", "section", "modifier"),
                                                     ("units", "amount", "anaesthesia_units", "fee_note")), pages)


def extract_codes(lines: list[Line], profile: Profile) -> Result:
    """Code lists: a code at the left, a description that may wrap. No fee columns."""
    rows: list[dict[str, str]] = []
    leftover: list[Line] = []
    pages: Counter = Counter()
    current: dict | None = None
    current_x = 0.0
    for line in lines:
        text = line.text
        if any(p.search(text) for p in profile.skip):
            continue
        m = profile.record.match(text)
        if m and (profile.code_max_x is None or line.x0 <= profile.code_max_x):
            code = profile.code_template.format(**m.groupdict()) if profile.code_template else (
                m.groupdict().get("code") or m.group(0))
            current = {"code": code, "description": text[m.end():].strip(), "page": str(line.page)}
            current_x = line.x0
            rows.append(current)
            pages[line.page] += 1
        elif current is not None and line.x0 > current_x + 1:
            current["description"] = f"{current['description']} {text}".strip()
        else:
            current = None
            leftover.append(line)
    return Result(rows, [], leftover, _conflicts(rows, ("code",), ("description",)), pages)


def _conflicts(rows, key_fields, value_fields) -> list[tuple[str, ...]]:
    seen: dict[tuple, dict] = {}
    conflicts = []
    for row in rows:
        key = tuple(row.get(k, "") for k in key_fields)
        values = {k: row.get(k, "") for k in value_fields}
        if key in seen and seen[key] != values and key not in conflicts:
            conflicts.append(key)
        seen.setdefault(key, values)
    return conflicts


def write_csv(result: Result, path: Path, profile: Profile) -> None:
    base = FEE_OUTPUT if profile.kind == "fees" else CODE_OUTPUT
    extra = sorted({k for row in result.rows for k in row} - set(base))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=base + extra, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result.rows)


def write_report(result: Result, profile: Profile, source: str, path: Path, sample: int = 200) -> None:
    lines = [
        f"# Extraction report: {source}",
        "",
        f"- payer: {profile.payer} ({profile.kind})",
        f"- rows written: {len(result.rows)} ({len({r['code'] for r in result.rows})} codes)",
        f"- records without a fee (not written): {len(result.no_fee)}",
        f"- leftover lines (matched nothing): {len(result.leftover)}",
        f"- duplicate keys with conflicting values: {len(result.duplicates)}",
        "",
        "## Records without a fee",
        "",
        *[f"- p{r.page} `{r.code}` {' '.join(r.description)[:120]}" for r in result.no_fee[:sample]],
        "",
        "## Conflicting duplicates",
        "",
        *[f"- {' / '.join(f'`{k}`' for k in key)}" for key in result.duplicates[:sample]],
        "",
        "## Leftover lines",
        "",
        "Add skip patterns for headers and footers; anything else here may be a record the profile missed.",
        "",
        *[f"- p{l.page} x={l.x0:.0f}: {l.text[:160]}" for l in result.leftover[:sample]],
        "",
        "## Rows per page",
        "",
        *[f"- p{page}: {count}" for page, count in sorted(result.pages.items())],
    ]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
