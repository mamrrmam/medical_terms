"""Extract fee-schedule rows from a PDF, driven by a per-payer profile.

Provincial fee schedules are mostly laid out as columns: a fee code at the left, a
description that may wrap over several lines, and one or more fee columns at the right.
A profile (TOML, see profiles/example.toml) says how to recognise each part:

  payer            payer_id the output is for, e.g. "NS_MSI"
  pages            page ranges holding the schedule, e.g. "40-412" (optional)
  code             regex a record's first line starts with, e.g. '\\d{2}\\.\\d{2}[A-Z]?'
  code_max_x       codes must start left of this x (points), to ignore codes cited in prose
  values           names of the right-hand fee columns, left to right: ["units"], ["amount", "extra_anaesthesia"]
                   ('units' and 'amount' feed the fee loader; 'extra_*' columns are kept in the CSV only)
  columns          x range (points) of each fee column, e.g. {units = [440, 520]}; needed when a
                   row can leave a column empty. Without it, values fill `values` left to right.
  value            regex for one fee value (default: 1,234.56 with optional $)
  value_min_x      fee values must start right of this x (points); defaults to the leftmost column
  modifier         regex for a modifier line inside a record, with a (?P<modifier>...) group (optional)
  section          regex for section headings, with a (?P<section>...) group (optional)
  skip             regexes for running headers, footers and page numbers
  effective_start  YYYY-MM-DD written on every row (optional; or pass --effective to the loader)

Lines that belong to no record and match no skip pattern are listed in the report, so a
profile can be tuned until nothing important is left over. Nothing is guessed silently:
records without a fee are reported, not written.
"""

import csv
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from medterms.extract.pdftext import Line

DEFAULT_VALUE = r"\$?\d{1,3}(?:,\d{3})*\.\d{2}"
OUTPUT_COLUMNS = ["code", "description", "units", "amount", "modifier", "effective_start", "section", "page"]


@dataclass
class Profile:
    payer: str
    code: re.Pattern
    values: list[str]
    value: re.Pattern
    code_max_x: float | None = None
    value_min_x: float | None = None
    columns: dict[str, tuple[float, float]] = field(default_factory=dict)
    modifier: re.Pattern | None = None
    section: re.Pattern | None = None
    skip: list[re.Pattern] = field(default_factory=list)
    pages: str | None = None
    effective_start: str | None = None

    @classmethod
    def load(cls, path: Path) -> "Profile":
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        values = raw.get("values", ["amount"])
        unknown = set(values) - {"units", "amount"} - {v for v in values if v.startswith("extra_")}
        if unknown:
            raise ValueError(f"{path}: values must be 'units', 'amount' or 'extra_*', not {sorted(unknown)}")
        columns = {name: tuple(rng) for name, rng in raw.get("columns", {}).items()}
        if set(columns) - set(values):
            raise ValueError(f"{path}: columns {sorted(set(columns) - set(values))} are not in values")
        value_min_x = raw.get("value_min_x", min((lo for lo, _ in columns.values()), default=None))
        compile_ = lambda key: re.compile(raw[key]) if raw.get(key) else None  # noqa: E731
        return cls(
            payer=raw["payer"],
            code=re.compile(raw["code"]),
            values=values,
            value=re.compile(raw.get("value", DEFAULT_VALUE)),
            code_max_x=raw.get("code_max_x"),
            value_min_x=value_min_x,
            columns=columns,
            modifier=compile_("modifier"),
            section=compile_("section"),
            skip=[re.compile(p) for p in raw.get("skip", [])],
            pages=raw.get("pages"),
            effective_start=raw.get("effective_start"),
        )


@dataclass
class Record:
    code: str
    page: int
    x0: float
    section: str
    description: list[str] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)
    modifiers: list[tuple[str, dict[str, str]]] = field(default_factory=list)


@dataclass
class Result:
    rows: list[dict[str, str]]
    no_fee: list[Record]
    leftover: list[Line]
    duplicates: list[tuple[str, str]]
    pages: Counter


def _split_values(line: Line, profile: Profile) -> tuple[list[str], dict[str, str]]:
    """Peel fee values off the right of a line; return (remaining words, {column: value})."""
    words = list(line.words)
    found = []
    while words and profile.value.fullmatch(words[-1].text) and (
        profile.value_min_x is None or words[-1].x0 >= profile.value_min_x
    ):
        found.append(words.pop())
    found.reverse()
    parsed = {}
    for i, word in enumerate(found):
        if profile.columns:
            centre = (word.x0 + word.x1) / 2
            name = next((n for n, (lo, hi) in profile.columns.items() if lo <= centre <= hi), None)
        else:
            name = profile.values[i] if i < len(profile.values) else None
        if name is None:  # a number outside every column belongs to the description
            words.append(word)
            continue
        parsed[name] = word.text.replace("$", "").replace(",", "")
    words.sort(key=lambda w: w.x0)
    return [w.text for w in words], parsed


def extract(lines: list[Line], profile: Profile) -> Result:
    rows: list[dict[str, str]] = []
    no_fee: list[Record] = []
    leftover: list[Line] = []
    pages: Counter = Counter()
    section = ""
    record: Record | None = None

    def flush():
        nonlocal record
        if record is None:
            return
        description = " ".join(record.description).strip()
        base = {"code": record.code, "description": description, "effective_start": profile.effective_start or "",
                "section": record.section, "page": str(record.page)}
        emitted = False
        if record.values:
            rows.append({**base, "modifier": "", **record.values})
            emitted = True
        for modifier, values in record.modifiers:
            if values:
                rows.append({**base, "modifier": modifier, **values})
                emitted = True
        if emitted:
            pages[record.page] += 1
        else:
            no_fee.append(record)
        record = None

    for line in lines:
        text = line.text
        if any(p.search(text) for p in profile.skip):
            continue
        if profile.section and (m := profile.section.fullmatch(text)):
            flush()
            section = m.group("section").strip()
            continue

        code_match = profile.code.match(text)
        if code_match and (profile.code_max_x is None or line.x0 <= profile.code_max_x):
            flush()
            words, values = _split_values(line, profile)
            rest = " ".join(words)[code_match.end():].strip()
            record = Record(code_match.group(0), line.page, line.x0, section, [rest] if rest else [], values)
            continue

        if record is not None and line.x0 > record.x0 + 1:
            if profile.modifier and (m := profile.modifier.match(text)):
                _, values = _split_values(line, profile)
                record.modifiers.append((m.group("modifier"), values))
                continue
            words, values = _split_values(line, profile)
            if words:
                record.description.append(" ".join(words))
            for name, value in values.items():
                record.values.setdefault(name, value)
            continue

        flush()
        leftover.append(line)
    flush()

    seen: dict[tuple[str, str], dict] = {}
    duplicates = []
    for row in rows:
        key = (row["code"], row["modifier"])
        if key in seen and {k: row.get(k) for k in profile.values} != {k: seen[key].get(k) for k in profile.values}:
            duplicates.append(key)
        seen.setdefault(key, row)
    return Result(rows, no_fee, leftover, duplicates, pages)


def write_csv(result: Result, path: Path) -> None:
    extra = sorted({k for row in result.rows for k in row} - set(OUTPUT_COLUMNS))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS + extra, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result.rows)


def write_report(result: Result, profile: Profile, source: str, path: Path, sample: int = 200) -> None:
    lines = [
        f"# Extraction report: {source}",
        "",
        f"- payer: {profile.payer}",
        f"- rows written: {len(result.rows)} ({len({r['code'] for r in result.rows})} codes)",
        f"- records without a fee (not written): {len(result.no_fee)}",
        f"- leftover lines (matched nothing): {len(result.leftover)}",
        f"- code+modifier pairs with conflicting fees: {len(result.duplicates)}",
        "",
        "## Records without a fee",
        "",
        *[f"- p{r.page} `{r.code}` {' '.join(r.description)[:120]}" for r in result.no_fee[:sample]],
        "",
        "## Conflicting duplicates",
        "",
        *[f"- `{code}` {modifier}" for code, modifier in result.duplicates[:sample]],
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
