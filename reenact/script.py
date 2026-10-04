"""The Script: the target's sole verbal authority.

Line markup (a compact form of Hypit's `<script>` syntax):

  ``||``              hand the on-screen Caption Cue to the next one
  ``@{id!}``          Moment: an event (reveal, verdict) other layers can respond to
  ``@{id}…@{/id}``    Selection: the span of meaning an element serves (B-roll, title)
  ``<shown|spoken>``  Dual Text: the Caption shows ``shown``, the performer says ``spoken``

A line becomes a list of atoms (one character of plain text, or one Dual Text unit);
markers and cue breaks point at atom indices, so after alignment every event has a time.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field

TOKEN = re.compile(r"\|\||@\{(/?)([A-Za-z0-9_\-]+)(!?)\}|<([^<>|]+)\|([^<>|]+)>")


@dataclass
class Atom:
    display: str
    speech: str


@dataclass
class Line:
    role: str
    atoms: list[Atom] = field(default_factory=list)
    cue_breaks: set[int] = field(default_factory=set)          # atom index that starts a new cue
    moments: dict[str, int] = field(default_factory=dict)       # id -> atom index
    selections: dict[str, list[int]] = field(default_factory=dict)  # id -> [start, end) atom index

    @property
    def display(self) -> str:
        return "".join(a.display for a in self.atoms)

    @property
    def speech(self) -> str:
        return "".join(a.speech for a in self.atoms)


def parse_line(role: str, text: str) -> Line:
    line = Line(role=role)
    pos = 0
    open_selections: dict[str, int] = {}
    for match in TOKEN.finditer(text):
        for ch in text[pos:match.start()]:
            line.atoms.append(Atom(ch, ch))
        pos = match.end()
        token = match.group(0)
        index = len(line.atoms)
        if token == "||":
            if index:
                line.cue_breaks.add(index)
        elif match.group(2):
            closing, name, moment = match.group(1), match.group(2), match.group(3)
            if moment:
                line.moments[name] = index
            elif closing:
                start = open_selections.pop(name, 0)
                line.selections[name] = [start, index]
            else:
                open_selections[name] = index
        else:
            line.atoms.append(Atom(match.group(4), match.group(5)))
    for ch in text[pos:]:
        line.atoms.append(Atom(ch, ch))
    for name, start in open_selections.items():
        line.selections[name] = [start, len(line.atoms)]
    # Leading/trailing whitespace atoms carry nothing on screen or in speech.
    while line.atoms and not line.atoms[-1].display.strip() and not line.atoms[-1].speech.strip():
        line.atoms.pop()
    return line


def strip_markup(text: str, *, spoken: bool) -> str:
    """Plain text of a line: what the performer says (spoken) or what the viewer reads."""
    def repl(match: re.Match) -> str:
        if match.group(4):
            return match.group(5) if spoken else match.group(4)
        return ""
    return re.sub(r"\s{2,}", " ", TOKEN.sub(repl, text)).strip()


# ── language helpers ───────────────────────────────────────────────────────────────

def is_cjk(ch: str) -> bool:
    return "㐀" <= ch <= "鿿" or "豈" <= ch <= "﫿" or "぀" <= ch <= "ヿ" or "가" <= ch <= "힯"


def normalise(ch: str) -> str:
    """Comparable form of one character; '' for punctuation and whitespace."""
    if not ch or ch.isspace():
        return ""
    cat = unicodedata.category(ch)
    if cat[0] in {"P", "S", "Z", "C"}:
        return ""
    return unicodedata.normalize("NFKC", ch).lower()


def spoken_units(text: str) -> float:
    """Rough speaking length: CJK characters count 1, Latin words count ~1.6."""
    cjk = sum(1 for ch in text if is_cjk(ch))
    words = len(re.findall(r"[A-Za-z0-9]+", text))
    return cjk + words * 1.6


def estimate_seconds(lines: list[str], pace: float = 4.6) -> float:
    """Measured delivery estimate for one Segment (brisk social pace)."""
    speech = sum(spoken_units(text) for text in lines) / pace
    return speech + 0.3 * max(0, len(lines) - 1) + 0.8


# ── Caption cues ───────────────────────────────────────────────────────────────────

CLAUSE_END = set("，。！？；、,.!?;:：…")


def cue_ranges(line: Line, max_units: float = 14.0) -> list[tuple[int, int]]:
    """Split a line's atoms into Caption Cues: explicit ``||`` first, then at clause
    punctuation, then by length, so each Cue reads as one unit."""
    bounds = sorted({0, *line.cue_breaks, len(line.atoms)})
    result: list[tuple[int, int]] = []
    for start, end in zip(bounds, bounds[1:]):
        _split(line, start, end, max_units, result)
    return [(a, b) for a, b in result if "".join(x.display for x in line.atoms[a:b]).strip(" ，。,.")]


def _units(line: Line, a: int, b: int) -> float:
    return spoken_units("".join(x.display for x in line.atoms[a:b]))


def _breakable(line: Line, index: int) -> bool:
    """A Cue may not start between two Latin/digit atoms (inside a word or number)."""
    if index <= 0 or index >= len(line.atoms):
        return True
    left, right = line.atoms[index - 1].display[-1:], line.atoms[index].display[:1]
    return not (left.isascii() and left.isalnum() and right.isascii() and (right.isalnum() or right == "."))


def _split(line: Line, start: int, end: int, max_units: float, out: list[tuple[int, int]]) -> None:
    total = _units(line, start, end)
    if total <= max_units * 1.25 or end - start <= 2:
        out.append((start, end))
        return
    # Prefer the clause boundary closest to the middle; otherwise the most balanced legal break.
    parts = math.ceil(total / max_units)
    target = total / parts
    best, best_cost = None, None
    for index in range(start + 1, end):
        running = _units(line, start, index)
        if not _breakable(line, index):
            continue
        clause = line.atoms[index - 1].display[-1:] in CLAUSE_END
        cost = abs(running - target) - (target * 0.6 if clause else 0)
        if running < 2 or total - running < 2:
            cost += 100
        if best_cost is None or cost < best_cost:
            best, best_cost = index, cost
    if best is None:
        out.append((start, end))
        return
    _split(line, start, best, max_units, out)
    _split(line, best, end, max_units, out)


def cue_text(line: Line, start: int, end: int) -> str:
    text = "".join(a.display for a in line.atoms[start:end]).strip()
    # Trailing clause punctuation reads as noise in short-form Captions.
    return text.rstrip("，。、,;；")
