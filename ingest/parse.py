"""Parse my.harvard search-result HTML fragments into structured course records.

The /search/ endpoint returns JSON whose `hits` field is a rendered HTML string
containing 15 "course cards". Everything we need for conflict detection lives in
the card itself -- term, session, meeting days, and meeting time -- so a full
catalog ingest costs ~545 page fetches rather than one fetch per course.

Day encoding: my.harvard marks meeting days with aria-label="Friday, selected"
on the day pill. Unselected days carry aria-label="Friday". That single suffix is
the entire signal, so we key off it exactly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DAY_NAMES

CARD_SPLIT = "<!-- Course Card -->"

# "1:30pm - 3:30pm", tolerating the newline+indent my.harvard emits mid-range.
TIME_RANGE_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*(am|pm)\s*-\s*(\d{1,2}):(\d{2})\s*(am|pm)", re.I
)
SELECTED_DAY_RE = re.compile(r'aria-label="(\w+), selected"')
DETAIL_URL_RE = re.compile(r"^/course/([^/]+)/([^/]+)/([^/]+)$")


@dataclass
class Meeting:
    day_mask: int
    start_min: int
    end_min: int
    raw_time: str = ""


@dataclass
class Course:
    course_id: str
    crse_offer_nbr: str
    code: str
    subject: str
    catalog: str
    section: str
    term: str
    title: str
    school: str = ""
    department: str = ""
    description: str = ""
    session: str = ""
    detail_url: str = ""
    instructors: list[str] = field(default_factory=list)
    meetings: list[Meeting] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Stable identity across ingests."""
        return f"{self.course_id}|{self.section}|{self.term}"


def to_minutes(hour: int, minute: int, meridiem: str) -> int:
    """Convert 12-hour clock parts to minutes past midnight."""
    meridiem = meridiem.lower()
    if hour == 12:
        hour = 0
    if meridiem == "pm":
        hour += 12
    return hour * 60 + minute


def minutes_to_label(m: int) -> str:
    h, mi = divmod(m, 60)
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{mi:02d}{suffix}"


def parse_day_mask(html: str) -> int:
    """Bitmask of meeting days; Sunday is bit 0."""
    mask = 0
    for name in SELECTED_DAY_RE.findall(html):
        if name in DAY_NAMES:
            mask |= 1 << DAY_NAMES.index(name)
    return mask


def mask_to_days(mask: int) -> list[str]:
    return [DAY_NAMES[i] for i in range(7) if mask & (1 << i)]


def _schedule_region(card_html: str) -> str:
    """Narrow to the schedule block so a course description containing something
    like "10:00am" can't be mistaken for a meeting time."""
    start = card_html.find("<!-- Week Days -->")
    if start == -1:
        start = card_html.find("<!-- End Week Days -->")
    if start == -1:
        return ""
    end = card_html.find("<!-- Begin: Location -->", start)
    if end == -1:
        end = card_html.find("<!-- End Col -->", start)
    return card_html[start : end if end != -1 else len(card_html)]


def parse_meetings(card_html: str) -> list[Meeting]:
    """Extract meeting blocks. Courses with no fixed time (TBA / asynchronous)
    legitimately return [] -- they can never conflict with anything."""
    region = _schedule_region(card_html)
    if not region:
        return []
    mask = parse_day_mask(region)
    meetings = []
    for m in TIME_RANGE_RE.finditer(region):
        h1, m1, ap1, h2, m2, ap2 = m.groups()
        start = to_minutes(int(h1), int(m1), ap1)
        end = to_minutes(int(h2), int(m2), ap2)
        if end <= start:  # defensive: never seen, but don't emit inverted ranges
            continue
        meetings.append(
            Meeting(
                day_mask=mask,
                start_min=start,
                end_min=end,
                raw_time=f"{minutes_to_label(start)} - {minutes_to_label(end)}",
            )
        )
    return meetings


def parse_card(card_html: str) -> Optional[Course]:
    soup = BeautifulSoup(card_html, "html.parser")
    root = soup.find("div", class_="course-card")
    if root is None:
        return None

    detail_a = soup.find("a", href=DETAIL_URL_RE)
    if detail_a is None:
        return None
    href = detail_a["href"]
    m = DETAIL_URL_RE.match(href)
    if not m:
        return None
    code, url_term, section = m.groups()

    title = detail_a.get_text(strip=True)

    # Badge reads "LAW 3500 FA02" -> subject / catalog.
    subject = catalog = ""
    for badge in soup.select("span.hs-tooltip-toggle"):
        parts = badge.get_text(strip=True).split()
        if len(parts) >= 2:
            subject, catalog = parts[0], parts[1]
            break

    instructors = [
        a.find("span", class_="link-body").get_text(strip=True)
        for a in soup.find_all("a", href=re.compile(r"^/instructor/"))
        if a.find("span", class_="link-body")
    ]

    school = ""
    school_a = soup.find("a", href=re.compile(r"^/school/"))
    if school_a:
        school = school_a.get_text(strip=True)

    department = ""
    dept_a = soup.find("a", href=re.compile(r"Department%2FField="))
    if dept_a:
        department = dept_a.get_text(strip=True)

    description = ""
    desc = soup.find("div", class_="course-description")
    if desc:
        description = desc.get_text(" ", strip=True)

    # The right-hand column emits term then session as bare spans.
    term = url_term.replace("-", " ")
    session = ""
    spans = [s.get_text(strip=True) for s in soup.find_all("span")]
    for s in spans:
        if re.fullmatch(r"\d{4}\s+(Fall|Spring|Summer|Winter)", s):
            term = s
        elif "Term" in s and len(s) < 40 and not s[0].isdigit():
            session = session or s

    return Course(
        course_id=root.get("data-course-id", ""),
        crse_offer_nbr=root.get("data-crse-offer-nbr", ""),
        code=code,
        subject=subject,
        catalog=catalog,
        section=section,
        term=term,
        title=title,
        school=school,
        department=department,
        description=description,
        session=session,
        detail_url=href,
        instructors=instructors,
        meetings=parse_meetings(card_html),
    )


def parse_hits(hits_html: str) -> list[Course]:
    """Parse the `hits` string from a /search/ JSON response."""
    out = []
    for chunk in hits_html.split(CARD_SPLIT)[1:]:
        course = parse_card(chunk)
        if course is not None:
            out.append(course)
    return out


def parse_term_facets(facets_html: str) -> list[tuple[str, int]]:
    """Available terms and their course counts, e.g. ("2026 Fall", 8163)."""
    soup = BeautifulSoup(facets_html, "html.parser")
    out = []
    for inp in soup.find_all("input", attrs={"data-type": "Term"}):
        value = inp.get("value", "")
        label = inp.find_parent("label")
        count = 0
        if label:
            nums = re.findall(r">(\d+)<", str(label))
            if nums:
                count = int(nums[-1])
        if value:
            out.append((value, count))
    return out
