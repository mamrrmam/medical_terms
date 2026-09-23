"""PDF pages -> positioned text lines, using pdfplumber (pip install medterms[pdf])."""

from dataclasses import dataclass, field


@dataclass
class Word:
    x0: float
    x1: float
    text: str


@dataclass
class Line:
    page: int  # 1-based
    top: float
    words: list[Word] = field(default_factory=list)

    @property
    def x0(self) -> float:
        return self.words[0].x0

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


def parse_pages(spec: str | None, count: int) -> list[int]:
    """'3-5,9' -> [3, 4, 5, 9] (1-based); None -> every page. Open-ended '12-' runs to the end."""
    if not spec:
        return list(range(1, count + 1))
    pages = []
    for part in spec.split(","):
        start, _, end = part.strip().partition("-")
        first = int(start)
        last = (int(end) if end else count) if "-" in part else first
        pages.extend(range(first, min(last, count) + 1))
    return pages


def read_lines(path, pages: str | None = None, y_tolerance: float = 3.0) -> list[Line]:
    """Words grouped into lines by vertical position, left to right, page by page."""
    import pdfplumber

    lines: list[Line] = []
    with pdfplumber.open(path) as pdf:
        for number in parse_pages(pages, len(pdf.pages)):
            words = pdf.pages[number - 1].extract_words(x_tolerance=1.5, y_tolerance=y_tolerance)
            page_lines: list[Line] = []
            for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
                if page_lines and abs(page_lines[-1].top - w["top"]) <= y_tolerance:
                    page_lines[-1].words.append(Word(w["x0"], w["x1"], w["text"]))
                else:
                    page_lines.append(Line(number, w["top"], [Word(w["x0"], w["x1"], w["text"])]))
            for line in page_lines:
                line.words.sort(key=lambda w: w.x0)
            lines.extend(page_lines)
    return lines


def read_tables(path, pages: str | None = None) -> list[tuple[int, list[str | None]]]:
    """(page, row cells) for every table row pdfplumber finds on ruled tables."""
    import pdfplumber

    rows = []
    with pdfplumber.open(path) as pdf:
        for number in parse_pages(pages, len(pdf.pages)):
            for table in pdf.pages[number - 1].extract_tables():
                rows.extend((number, row) for row in table)
    return rows
