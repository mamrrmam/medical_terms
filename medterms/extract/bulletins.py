"""Extract fee announcements from Nova Scotia MSI Physician's Bulletins into a dated change log.

Bulletins are newsletters, not schedules: fee tables sit between paragraphs, their columns move
from issue to issue, and values are printed as '17 MSU', '25 MSU + MU', '406 MSU 7+T', '$150/night'
or as old/new pairs in fee-increase tables. So rows are recognised by text, not column positions,
and each one carries the context it was announced in:

  issue_date       from the page footer ("PHYSICIAN'S BULLETIN July 17, 2026 2")
  announcement     the heading above it ("NEW INTERIM FEES", "FEE UPDATES", ...)
  action           new / updated / terminated, from the heading and the "Effective ..." sentence
  effective_start  the latest "Effective <date>" since the heading, else the issue date

Profile keys (TOML, kind = "bulletins", see medterms/profiles/ns_msi_bulletins.toml):

  payer, pages, skip   as for fee profiles
  issue            regex with a (?P<date>...) group, searched on every line of a page
  announcement     regex (fullmatch) for announcement headings
  fee_section      regex searched on a heading: code-first rows count only under matching headings
  effective        regex with a (?P<date>...) group for "Effective <date>" sentences
  record           regex for "CATEGORY CODE ..." rows, with groups category and code
  code_row         regex for rows that start with a code but no category, with group code
  modifier         regex for one modifier token (as for fee profiles)
  placeholders     codes printed before one was assigned ("TBD", "TBA"); reported, not written

The output is the fee CSV layout (`medterms load fees`) plus issue_date, announcement, action
and old_units. Rows are written in bulletin order (newest issue first).
"""

import datetime
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from medterms.extract.fees import Result, profile_path
from medterms.extract.pdftext import Line

OUTPUT = ["code", "description", "units", "amount", "anaesthesia_units", "fee_note", "modifier", "category",
          "heading", "effective_start", "action", "old_units", "issue_date", "announcement", "page"]

MSU = re.compile(r"(?P<units>\d+(?:\.\d+)?)\s*MSU\b\s*(?P<rest>.*)$")
DOLLARS = re.compile(r"(?P<amounts>\$\s?\d[\d,]*(?:\.\d{2})?(?:\s+\$\s?\d[\d,]*(?:\.\d{2})?)*)(?P<rest>.*)$")
OLD_NEW = re.compile(r"(?P<old>\d+\.\d{2})\s+(?P<new>\d+\.\d{2})$")
TIME_UNITS = re.compile(r"(?P<anaes>\d+)\s*\+\s*T\b")


@dataclass
class BulletinProfile:
    payer: str
    issue: re.Pattern
    announcement: re.Pattern
    fee_section: re.Pattern
    effective: re.Pattern
    record: re.Pattern
    code_row: re.Pattern
    modifier: re.Pattern
    placeholders: set[str] = field(default_factory=set)
    skip: list[re.Pattern] = field(default_factory=list)
    pages: str | None = None
    kind: str = "bulletins"

    @classmethod
    def load(cls, path: Path | str) -> "BulletinProfile":
        with open(profile_path(path), "rb") as f:
            raw = tomllib.load(f)
        return cls(
            payer=raw["payer"],
            issue=re.compile(raw["issue"]),
            announcement=re.compile(raw["announcement"]),
            fee_section=re.compile(raw["fee_section"]),
            effective=re.compile(raw["effective"]),
            record=re.compile(raw["record"]),
            code_row=re.compile(raw["code_row"]),
            modifier=re.compile(raw["modifier"]),
            placeholders=set(raw.get("placeholders", [])),
            skip=[re.compile(p) for p in raw.get("skip", [])],
            pages=raw.get("pages"),
        )


def parse_date(text: str) -> str | None:
    """'July 17, 2026' / 'March 19th, 2021' / 'Sept. 1 2023' -> '2026-07-17'."""
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text).replace(".", "").replace(",", " ")
    parts = text.split()
    if len(parts) != 3:
        return None
    month, day, year = parts
    month = month.capitalize()
    for fmt in ("%B", "%b"):
        try:
            number = datetime.datetime.strptime(month[:3] if fmt == "%b" else month, fmt).month
            return datetime.date(int(year), number, int(day)).isoformat()
        except ValueError:
            continue
    return None


def parse_value(text: str) -> tuple[str, dict[str, str]] | None:
    """Split a row's text into (description, fee fields); None when it has no recognisable fee."""
    if m := MSU.search(text):
        fee = {"units": m.group("units")}
        rest = m.group("rest").strip()
        if t := TIME_UNITS.fullmatch(rest):
            fee["anaesthesia_units"] = t.group("anaes")
        printed = text[m.start():].strip()
        if rest:
            fee["fee_note"] = printed
        return text[:m.start()].strip(), fee
    if m := DOLLARS.search(text):
        amounts = [a.replace("$", "").replace(",", "").strip() for a in m.group("amounts").split("$") if a.strip()]
        rest = m.group("rest").strip()
        fee = {"amount": amounts[0]}
        if len(amounts) > 1 or rest:
            fee["fee_note"] = text[m.start():].strip()
        return text[:m.start()].strip(), fee
    if m := OLD_NEW.search(text):
        return text[:m.start()].strip(), {"units": m.group("new"), "old_units": m.group("old")}
    return None


CODE_IN_TEXT = re.compile(r"\b(?:\d{2}\.\d{1,2}[A-Z]?|[A-Z]{1,5}\d{1,4}[A-Z]?)\b")
LEADER = re.compile(r"\s*\.{2,}\s*")
LABELS = re.compile(r"(Description|Billing|Category|Premium|Specialty|Location|Multiples|Note)\b")


def _action(announcement: str, sentence: str, code: str) -> str:
    """new / updated / terminated. A terminating sentence that names other codes ("F2010 has been
    terminated and replaced with ...") doesn't terminate the code in the row below it."""
    if re.search(r"terminat|discontinu|delet", sentence + " " + announcement, re.I):
        named = set(CODE_IN_TEXT.findall(sentence))
        if not named or code in named:
            return "terminated"
    if re.search(r"\bNEW\b", announcement):
        return "new"
    return "updated"


def _split_row(text: str, modifier: re.Pattern) -> tuple[list[str], str, dict[str, str]] | None:
    """Text after a row's code -> (modifier tokens, description, fee fields), or None without a fee.

    Modifiers can lead the description ("RP=SUBS 17 MSU"), follow the fee as a specialty list
    ("17 MSU SP=NEUR, SP=INMD"), or be a whole dot-leader line ("LO=HOSP, SP=NEUR. ...... 18.39")."""
    leader = bool(LEADER.search(text))
    text = LEADER.sub(" ", text).strip()
    tokens, text = _modifier_tokens(text, modifier)
    parsed = parse_value(text)
    if parsed is None and leader and (m := re.search(r"(?P<units>\d+(?:\.\d+)?)$", text)):
        parsed = text[:m.start()].strip(" ."), {"units": m.group("units")}
    if parsed is None:
        return None
    description, fee = parsed
    if note := fee.get("fee_note"):
        after, rest = _modifier_tokens(re.sub(r"^\S+\s*MSU\s*", "", note), modifier)
        if after and not rest:
            tokens += after
            fee = {k: v for k, v in fee.items() if k != "fee_note"}
    return tokens, description.strip(" ."), fee


def _add_note(row: dict, text: str) -> None:
    """Append printed text such as '+MU' to the row's fee_note, keeping the printed fee in front."""
    printed = row.get("fee_note") or (row.get("units") and f"{row['units']} MSU") or (
        row.get("amount") and f"${row['amount']}") or ""
    row["fee_note"] = f"{printed} {text}".strip()


def extract(lines: list[Line], profile: BulletinProfile) -> Result:
    issue_by_page: dict[int, str] = {}
    for line in lines:
        if line.page not in issue_by_page and (m := profile.issue.search(line.text)):
            if date := parse_date(m.group("date")):
                issue_by_page[line.page] = date

    rows: list[dict[str, str]] = []
    skipped: list[Line] = []
    announcement, fee_section = "", False
    effective, sentence = None, ""
    current: dict | None = None
    anchors: set[float] = set()
    continuation = 0

    for line in lines:
        text = line.text.strip()
        issue = issue_by_page.get(line.page, "")
        if any(p.search(text) for p in profile.skip) or profile.issue.search(text):
            continue
        if profile.announcement.fullmatch(text):
            announcement, fee_section = text, bool(profile.fee_section.search(text))
            effective, sentence, current = None, "", None
            continue
        if m := profile.effective.search(text):
            if date := parse_date(m.group("date")):
                effective, sentence, current = date, text, None
                continue

        m = profile.record.match(text)
        code_first = None if m else profile.code_row.match(text)
        if m or (code_first and fee_section):
            match = m or code_first
            code = match.group("code")
            split = _split_row(text[match.end():], profile.modifier)
            if split is None and m:
                # a category row whose fee is on the next lines, or a terminated code with none printed
                tokens, rest = _modifier_tokens(text[match.end():].strip(), profile.modifier)
                split = tokens, rest, {}
            if split is not None:
                if code in profile.placeholders:
                    skipped.append(line)
                    current = None
                    continue
                tokens, description, fee = split
                current = {
                    "code": code, "description": description, "modifier": ", ".join(tokens),
                    "category": (m.group("category") if m else ""), "heading": announcement,
                    "effective_start": effective or issue, "action": _action(announcement, sentence, code),
                    "issue_date": issue, "announcement": announcement, "page": str(line.page), **fee,
                }
                rows.append(current)
                # continuation lines line up with the row's start, its code or its description
                anchors = {line.x0, line.words[1 if m else 0].x0,
                           next((w.x0 for w in line.words[2 if m else 1:]), line.x0)}
                continuation = 0
                continue

        if current is None or continuation >= 6 or min(abs(line.x0 - a) for a in anchors) >= 8 or LABELS.match(text):
            current = None
            continue
        continuation += 1
        has_fee = any(current.get(k) for k in ("units", "amount"))
        if re.fullmatch(r"\+\s*MU|\d+\s*\+\s*T", text):
            _add_note(current, text)
            continue
        tokens, rest = _modifier_tokens(LEADER.sub(" ", text).strip(), profile.modifier)
        if not has_fee and (m := re.fullmatch(r"MSU\b\s*(.*)", rest)) and (
                n := re.search(r"\s(\d+(?:\.\d+)?)$", current["description"])):
            # "... for ME=CARE 20.99" / "ME=CARE MSU +MU": the unit label wrapped onto the next line
            current["units"] = n.group(1)
            current["description"] = current["description"][:n.start()].strip()
            if tokens and not current["modifier"]:
                current["modifier"] = ", ".join(tokens)
            if m.group(1):
                _add_note(current, m.group(1))
            continue
        split = _split_row(text, profile.modifier)
        if tokens and split and (has_fee or current["modifier"]):
            # another modifier row for the same code, with its own fee
            more_tokens, _, fee = split
            current = {**current, "modifier": ", ".join(more_tokens), "units": "", "amount": "",
                       "anaesthesia_units": "", "fee_note": "", "old_units": "", **fee}
            rows.append(current)
        elif tokens and not current["modifier"]:
            current["modifier"] = ", ".join(tokens)
            if split:
                current.update(split[2])
            elif rest:
                _add_note(current, rest)
        elif split and not has_fee:
            _, extra, fee = split
            current.update(fee)
            current["description"] = f"{current['description']} {extra}".strip()
        elif not split and not tokens:
            if has_fee and (len(text) > 70 or re.search(r"\.(\s|$)", text)):
                current = None  # prose under the table, not a wrapped description
                continue
            current["description"] = f"{current['description']} {text}".strip()

    no_fee = [r for r in rows if not any(r.get(k) for k in ("units", "amount", "fee_note")) and r["action"] != "terminated"]
    rows = [r for r in rows if r not in no_fee]
    for r in rows:
        for column in OUTPUT:
            r.setdefault(column, "")
    return Result(rows, no_fee, skipped, [], _pages(rows))


def _modifier_tokens(text: str, modifier: re.Pattern) -> tuple[list[str], str]:
    tokens, pos = [], 0
    sep = re.compile(r"\s*[,;]?\s*")
    while m := modifier.match(text, sep.match(text, pos).end()):
        tokens.append(re.sub(r"^(\(?[A-Z]{2})\s?[=-]\s?", r"\1=", m.group(0)))
        pos = m.end()
    return tokens, text[pos:].strip(" ,;")


def _pages(rows):
    from collections import Counter
    return Counter(int(r["page"]) for r in rows)


def write_csv(result: Result, path: Path) -> None:
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result.rows)


def write_report(result: Result, profile: BulletinProfile, source: str, path: Path,
                 known_codes: set[str] | None = None) -> None:
    no_fee = result.no_fee
    from collections import Counter
    actions = Counter(r["action"] for r in result.rows)
    lines = [
        f"# Bulletin extraction report: {source}",
        "",
        f"- payer: {profile.payer}",
        f"- rows written: {len(result.rows)} ({len({r['code'] for r in result.rows})} codes); "
        + ", ".join(f"{k} {v}" for k, v in sorted(actions.items())),
        f"- rows with a code but no fee found (not written): {len(no_fee)}",
        f"- placeholder codes skipped: {len(result.leftover)}",
        f"- rows whose effective date fell back to the issue date: "
        f"{sum(1 for r in result.rows if r['effective_start'] == r['issue_date'])}",
        "",
        "## Rows by issue",
        "",
        "| issue | effective | action | code | modifier | fee | description |",
        "|---|---|---|---|---|---|---|",
        *[f"| {r['issue_date']} | {r['effective_start']} | {r['action']} | `{r['code']}` | {r['modifier']} | "
          f"{r['fee_note'] or (r['units'] and r['units'] + ' MSU') or '$' + r['amount']}"
          f"{r['old_units'] and ' (was ' + r['old_units'] + ')'} | {r['description'][:70]} |"
          for r in result.rows],
        "",
        "## Rows with a code but no fee",
        "",
        *[f"- p{r['page']} {r['issue_date']} `{r['code']}` {r['description'][:100]}" for r in no_fee],
        "",
        "## Placeholder codes",
        "",
        *[f"- p{l.page}: {l.text[:140]}" for l in result.leftover],
    ]
    if known_codes is not None:
        missing = sorted({r["code"] for r in result.rows} - known_codes)
        lines += ["", "## Codes not in the comparison schedule", "", ", ".join(f"`{c}`" for c in missing)]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
