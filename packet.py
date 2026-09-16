"""Build a homework problem packet: the professor's sheet only names problems
("§1.2 # 41, 42, 43"), so this pulls the actual problem text out of the textbook
and assembles one self-contained PDF.

The problems are *clipped* out of the textbook as vector regions rather than
re-typeset from extracted text. Calculus notation does not survive text
extraction - the professor's own sheet comes out as "⃗ a" and "− − →P Q" - so
copying the rendered region is the only way the math stays readable.

Pure PDF work, no network: `python packet.py <homework.pdf>` builds one and
`test_logic.py` exercises the parsing offline.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from statistics import median
from pathlib import Path

import pymupdf

# Textbook body geometry. The book is printed with mirrored margins: on a recto
# page problem numbers right-align at x=96 and the marginal notes column sits
# beyond x=480, while on a verso page the whole text block shifts 85pt right
# (numbers at x=181) and the notes move to the left edge. So the column is
# measured per page off the problem numbers themselves rather than hardcoded;
# every other offset below is relative to that right edge.
INSTRUCTION_DX = -20.0  # instruction paragraphs start here, relative to numbers
CLIP_DX0 = -26.0  # left edge of the clip
CLIP_DX1 = 376.0  # right edge, inside the marginal notes either way
HEADER_Y = 100.0  # running head / page number above this
PAD = 3.0

# Output page
PAGE_W, PAGE_H = 612.0, 792.0
MARGIN = 54.0
FOOT = 40.0

_SECTION_PROBLEMS = re.compile(r"[§S]?\s*(\d+\.\d+)\s*#\s*([0-9,\s–—-]+)")
_RANGE = re.compile(r"(\d+)\s*[-–—]\s*(\d+)")
_NUMBER_TOKEN = re.compile(r"^(\d+)\.$")
_HW_NUMBER = re.compile(r"homework\s*#?\s*(\d+)", re.I)
_EX_HEADING = re.compile(r"^(\d+\.\d+)\.1$")


@dataclass
class HomeworkSpec:
    """What one of the professor's homework sheets asks for."""

    number: int | None
    title: str
    book: dict[str, list[int]] = field(default_factory=dict)  # "1.2" -> [41, 42, 43]
    extra_page: int | None = None  # page where "Additional problems" starts
    extra_y: float | None = None  # and how far down it starts

    @property
    def problem_count(self) -> int:
        return sum(len(v) for v in self.book.values())


def _expand(spec: str) -> list[int]:
    """'1-5, 9-13, 18' -> [1,2,3,4,5,9,10,11,12,13,18], in the order written."""
    out: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = _RANGE.match(chunk)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo <= hi and hi - lo <= 200:  # guard a mis-parse like "1-9999"
                out.extend(range(lo, hi + 1))
            continue
        if chunk.isdigit():
            out.append(int(chunk))
    seen: set[int] = set()
    return [n for n in out if not (n in seen or seen.add(n))]


def parse_homework(pdf_path: str | Path, logger: logging.Logger) -> HomeworkSpec:
    """Read one homework sheet: which book problems, and where the extras start."""
    doc = pymupdf.open(pdf_path)
    try:
        full = "\n".join(page.get_text() for page in doc)

        m = _HW_NUMBER.search(full)
        number = int(m.group(1)) if m else None
        title = f"Homework {number}" if number else Path(pdf_path).stem

        # Only the "Book problems" part of the sheet lists section/problem
        # numbers; stop at the extras so a stray "(1)" there is never read as one.
        head = full
        cut = re.search(r"Additional\s+problems", full, re.I)
        if cut:
            head = full[: cut.start()]

        book: dict[str, list[int]] = {}
        for sec, nums in _SECTION_PROBLEMS.findall(head):
            found = _expand(nums)
            if found:
                book.setdefault(sec, [])
                book[sec].extend(n for n in found if n not in book[sec])

        extra_page = extra_y = None
        for pno, page in enumerate(doc):
            hits = page.search_for("Additional problems")
            if hits:
                # Start just below his own heading: the packet prints its own.
                extra_page, extra_y = pno, hits[0].y1 + 1
                break

        logger.info(
            "%s: %d book problem(s) across %d section(s)%s",
            title,
            sum(len(v) for v in book.values()),
            len(book),
            "" if extra_page is None else ", plus additional problems",
        )
        return HomeworkSpec(number, title, book, extra_page, extra_y)
    finally:
        doc.close()


@dataclass
class _Line:
    page: int
    y0: float
    y1: float
    kind: str  # "number" | "instruction" | "body"
    number: int | None = None
    cx0: float = 0.0  # clip window for this page
    cx1: float = 0.0


def _number_right_edge(words: list) -> float | None:
    """Where this page right-aligns its problem numbers, read off the numbers."""
    edges = Counter(
        round(w[2]) for w in words if _NUMBER_TOKEN.match(w[4]) and w[1] >= HEADER_Y
    )
    if edges:
        edge, count = edges.most_common(1)[0]
        if count >= 2:
            return float(edge)
    return None


def _guess_right_edge(words: list) -> float | None:
    """Same, for a page with no numbers to measure: the most common left edge.

    Only right where that edge is the body indent - a page of continuation text
    - so it is the last resort, after the mirrored-margin guess below.
    """
    lefts = Counter(round(w[0]) for w in words if w[1] >= HEADER_Y and w[0] > 50)
    if not lefts:
        return None
    return float(lefts.most_common(1)[0][0]) - 5.0


def _exercises_top(words: list) -> float:
    """Below the '1.3.1 Exercises' heading, if this page carries it.

    A section's exercises can begin at the foot of a page that is otherwise the
    end of the prose, and that prose is laid out at the same left edge as the
    instruction paragraphs - so without this it reads as one enormous
    instruction and drags the whole page into the packet.
    """
    for w in words:
        if _EX_HEADING.match(w[4]) and w[1] >= HEADER_Y:
            return float(w[3])
    return HEADER_Y


def _classify(doc: pymupdf.Document, pages: range) -> list[_Line]:
    """Walk the exercise pages and label each line by its left edge."""
    lines: list[_Line] = []
    in_range = [p for p in pages if p < doc.page_count]
    words_by_page = {p: doc[p].get_text("words") for p in in_range}
    edges = {p: _number_right_edge(words_by_page[p]) for p in in_range}

    # The book is printed with mirrored margins, so the text block sits at one
    # of two offsets by recto/verso. A page with no problem numbers on it can
    # therefore take its edge from a page of the same parity that does have
    # them - far better than guessing from the text, which on a page of prose
    # lands 15pt off and throws every instruction paragraph into the body.
    by_parity: dict[int, list[float]] = {}
    for p, e in edges.items():
        if e is not None:
            by_parity.setdefault(p % 2, []).append(e)

    first = in_range[0] if in_range else None
    for pno in in_range:
        words = words_by_page[pno]
        nx = edges[pno]
        if nx is None:
            same = by_parity.get(pno % 2)
            nx = median(same) if same else _guess_right_edge(words)
        if nx is None:
            continue
        # Only the first page of the range can hold this section's heading;
        # a later one would be the next section's, which is out of range.
        top = _exercises_top(words) if pno == first else HEADER_Y
        cx0, cx1 = nx + CLIP_DX0, nx + CLIP_DX1
        instruction_x = nx + INSTRUCTION_DX

        grouped: dict[tuple[int, int], list] = {}
        for w in words:
            x0, y0, x1, y1, text, block, line, _ = w
            if y0 < top or x0 < cx0 or x0 > cx1:
                continue  # running head, page number, margin notes
            grouped.setdefault((block, line), []).append((x0, y0, x1, y1, text))

        # Walk the page in reading order, not top-to-bottom. A radical sign or
        # superscript is drawn above its own baseline, so by y it sorts ahead of
        # the problem number it belongs to - and the previous problem's clip
        # then stretches down over the next one. Block/line order keeps each
        # fragment with its own problem.
        for key in sorted(grouped):
            ws = sorted(grouped[key], key=lambda w: w[0])
            fx0, _, fx1, _, ftext = ws[0]
            y0 = min(w[1] for w in ws)
            y1 = max(w[3] for w in ws)

            m = _NUMBER_TOKEN.match(ftext)
            if m and abs(fx1 - nx) <= 3.5:
                kind, num = "number", int(m.group(1))
            elif fx0 < instruction_x + 8:
                kind, num = "instruction", None
            else:
                kind, num = "body", None
            lines.append(_Line(pno, y0, y1, kind, num, cx0, cx1))
    return lines



def _heading_label(doc: pymupdf.Document, pno: int) -> str | None:
    """Read the '1.2.1 Exercises' heading off the page, if it is on this one."""
    if not 0 <= pno < doc.page_count:
        return None
    for w in doc[pno].get_text("words"):
        m = _EX_HEADING.match(w[4])
        if m:
            return m.group(1)
    return None


def _section_pages(doc: pymupdf.Document) -> dict[str, range]:
    """Map '1.2' -> the pdf pages holding that section's exercises.

    The TOC nests chapter (level 1) > section (level 2) > "Exercises" (level 3)
    but numbers none of them, so the label has to be inferred. Position in the
    TOC gives it, except front matter ("Preface") is also level 1 and would
    shift every chapter by one - so only level-1 entries that actually contain
    sections count. The page's own "1.2.1 Exercises" heading overrides that
    whenever it is present, which is the one unambiguous source.
    """
    toc = doc.get_toc()
    entries = toc + [[1, "", doc.page_count + 1]]

    real_chapter: set[int] = set()
    last_top: int | None = None
    for i, (level, _, _) in enumerate(entries):
        if level == 1:
            last_top = i
        elif level == 2 and last_top is not None:
            real_chapter.add(last_top)

    out: dict[str, range] = {}
    chapter = section = 0
    pending: tuple[str, int] | None = None  # (label, exercise start) awaiting its end

    for i, (level, title, page) in enumerate(entries):
        if level <= 2 and pending:
            label, start = pending
            out[label] = range(start - 1, page - 1)
            pending = None
        if level == 1:
            if i in real_chapter:
                chapter += 1
                section = 0
        elif level == 2:
            section += 1
        elif level == 3 and title.strip().lower().startswith("exercise") and section:
            label = _heading_label(doc, page - 1) or f"{chapter}.{section}"
            pending = (label, page)
    return out


def _runs(lines: list[_Line]) -> list[tuple[int, float, float, float, float]]:
    """Collapse lines into (page, top, bottom, clip x0, clip x1) bands.

    One band per page touched, so a problem split across a page break comes out
    as two clips rather than one impossible rectangle.
    """
    bands: list[tuple[int, float, float, float, float]] = []
    for ln in lines:
        if bands and bands[-1][0] == ln.page:
            page, top, bot, cx0, cx1 = bands[-1]
            bands[-1] = (page, min(top, ln.y0), max(bot, ln.y1), cx0, cx1)
        else:
            bands.append((ln.page, ln.y0, ln.y1, ln.cx0, ln.cx1))
    return bands


class _Writer:
    """Stacks clipped regions down a page, starting a new one when full."""

    def __init__(self, out: pymupdf.Document):
        self.out = out
        self.page: pymupdf.Page | None = None
        self.y = 0.0

    def _new_page(self) -> None:
        self.page = self.out.new_page(width=PAGE_W, height=PAGE_H)
        self.y = MARGIN

    def space(self, need: float) -> None:
        if self.page is None or self.y + need > PAGE_H - FOOT:
            self._new_page()

    def text(self, s: str, size: float = 11.0, gap: float = 6.0, bold: bool = True) -> None:
        self.space(size + gap)
        assert self.page is not None
        self.page.insert_text(
            (MARGIN, self.y + size),
            s,
            fontname="hebo" if bold else "helv",
            fontsize=size,
        )
        self.y += size + gap

    def clip(
        self,
        src: pymupdf.Document,
        page: int,
        top: float,
        bottom: float,
        cx0: float,
        cx1: float,
    ) -> None:
        rect = pymupdf.Rect(cx0, top - PAD, cx1, bottom + PAD)
        h, w = rect.height, rect.width
        if h <= 1:
            return
        # Taller than a whole page (a long problem with a figure): scale to fit.
        avail = PAGE_H - MARGIN - FOOT
        scale = min(1.0, avail / h)
        self.space(h * scale)
        assert self.page is not None
        dest = pymupdf.Rect(MARGIN, self.y, MARGIN + w * scale, self.y + h * scale)
        self.page.show_pdf_page(dest, src, page, clip=rect)
        self.y += h * scale + 4


def build_packet(
    spec: HomeworkSpec,
    homework_pdf: str | Path,
    textbook_pdf: str | Path,
    out_path: str | Path,
    logger: logging.Logger,
    course: str = "MATH 2321",
) -> dict[str, object]:
    """Write the packet and report what made it in and what did not."""
    book = pymupdf.open(textbook_pdf)
    hw = pymupdf.open(homework_pdf)
    out = pymupdf.open()
    missing: list[str] = []
    included = 0

    try:
        sections = _section_pages(book)
        w = _Writer(out)

        w.text(f"{course} — {spec.title}", size=17, gap=4)
        w.text(
            f"Problem packet · built {date.today():%b %d, %Y} · "
            f"{spec.problem_count} textbook problems + the professor's own",
            size=8,
            gap=14,
            bold=False,
        )

        for sec, wanted in spec.book.items():
            pages = sections.get(sec)
            if pages is None:
                missing.append(f"§{sec} (no such section in the textbook)")
                logger.warning("Packet: textbook has no section %s", sec)
                continue

            lines = _classify(book, pages)

            # Split the page stream into problems, remembering the instruction
            # paragraph each one sits under ("determine the magnitude and
            # direction of the given vector") - without it a bare problem like
            # "41. v = (3, 4)" says nothing about what to do.
            problems: dict[int, list[_Line]] = {}
            heading: dict[int, list[_Line]] = {}
            current: list[_Line] = []
            instruction: list[_Line] = []
            active: int | None = None

            for ln in lines:
                if ln.kind == "number" and ln.number is not None:
                    if active is not None:
                        problems[active] = current
                    active = ln.number
                    current = [ln]
                    heading[active] = instruction
                elif ln.kind == "instruction":
                    if active is not None:
                        problems[active] = current
                        active, current = None, []
                        instruction = []
                    instruction.append(ln)
                elif active is not None:
                    current.append(ln)
            if active is not None:
                problems[active] = current

            w.text(f"§{sec}", size=13, gap=8)

            last_heading: list[_Line] | None = None
            for n in wanted:
                lns = problems.get(n)
                if not lns:
                    missing.append(f"§{sec} #{n}")
                    logger.warning("Packet: §%s #%d not found in exercises", sec, n)
                    continue

                intro = heading.get(n) or []
                if intro and intro is not last_heading:
                    for page, top, bot, cx0, cx1 in _runs(intro):
                        w.clip(book, page, top, bot, cx0, cx1)
                    last_heading = intro

                for page, top, bot, cx0, cx1 in _runs(lns):
                    w.clip(book, page, top, bot, cx0, cx1)
                included += 1

        if spec.extra_page is not None:
            w.text("Additional problems", size=13, gap=8)
            for pno in range(spec.extra_page, hw.page_count):
                page = hw[pno]
                top = spec.extra_y if pno == spec.extra_page else page.rect.y0
                rect = pymupdf.Rect(page.rect.x0, top, page.rect.x1, page.rect.y1)
                avail = PAGE_H - MARGIN - FOOT
                scale = min(1.0, (PAGE_W - 2 * MARGIN) / rect.width, avail / rect.height)
                w.space(rect.height * scale)
                assert w.page is not None
                dest = pymupdf.Rect(
                    MARGIN, w.y, MARGIN + rect.width * scale, w.y + rect.height * scale
                )
                w.page.show_pdf_page(dest, hw, pno, clip=rect)
                w.y += rect.height * scale + 4

        if out.page_count == 0:
            raise RuntimeError("packet came out empty")

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        out.save(out_path, deflate=True, garbage=3)
        logger.info(
            "Packet: %s -> %d problem(s) on %d page(s)%s",
            Path(out_path).name,
            included,
            out.page_count,
            f", {len(missing)} not found" if missing else "",
        )
        return {"included": included, "missing": missing, "pages": out.page_count}
    finally:
        out.close()
        hw.close()
        book.close()


if __name__ == "__main__":
    import sys

    from common import setup_logging

    log = setup_logging(True)
    src = Path(sys.argv[1])
    textbook = sys.argv[2] if len(sys.argv) > 2 else None
    if not textbook:
        raise SystemExit("usage: python packet.py <homework.pdf> <textbook.pdf> [out.pdf]")
    out = sys.argv[3] if len(sys.argv) > 3 else f"{src.stem} packet.pdf"
    result = build_packet(parse_homework(src, log), src, textbook, out, log)
    print(result)
