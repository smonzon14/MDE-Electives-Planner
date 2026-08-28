"""MDE elective policy engine.

Encodes "MDE Elective Policy" (last updated 2025-08-10). Two things drive every
answer here and neither is a property of the course alone:

  1. The student's background -- whether they have a SEAS area of study, and
     whether they have a physical design background. Rule 2 changes the level
     gate entirely; rule 1a only applies to some students.
  2. The current term -- which year/season the student is in, which sets how
     many electives they take and whether CS50 is still owed.

So eligibility is always evaluated as (course, profile), never (course) alone.

Design rule: when the policy depends on a list we don't have (the three
"approved ..." lists), this module returns UNKNOWN, never a guess. Silently
treating an unverified course as satisfying a requirement is the one failure
mode that could actually cost someone a credit.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import ROOT

POLICY_PATH = ROOT / "mde_policy.yaml"
LISTS_PATH = ROOT / "approved_lists.yaml"

# Verdicts for "does this course satisfy requirement X?"
YES = "yes"
NO = "no"
UNKNOWN = "unknown"       # policy depends on a list we don't have
VERIFY = "verify"         # counts, but a human must confirm something


# --------------------------------------------------------------- profile ---

@dataclass
class StudentProfile:
    """Everything about a student that changes what counts for credit.

    Carries no identity field: the profile is owned and stored by the browser
    and arrives with each request, so the server never knows whose it is.
    """
    year: int = 1                                   # 1 or 2
    season: str = "Fall"                            # Fall | Spring
    # Rule 2: does the student have a background in a SEAS area of study?
    seas_background: bool = False
    # Their area(s) of study, as SEAS subject codes. Rule 2a-i holds these to
    # graduate level specifically.
    seas_areas: list[str] = field(default_factory=list)
    # Rule 1a: architecture, industrial design, etc.
    physical_design_background: bool = False
    # required | online_certificate | petition | completed
    cs50_status: str = "required"
    completed_codes: list[str] = field(default_factory=list)

    @property
    def term_slot(self) -> str:
        return f"{self.year}-{self.season}"

    @property
    def is_first_semester(self) -> bool:
        return self.year == 1 and self.season == "Fall"

    @property
    def cs50_outstanding(self) -> bool:
        return self.cs50_status == "required"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["term_slot"] = self.term_slot
        d["is_first_semester"] = self.is_first_semester
        d["cs50_outstanding"] = self.cs50_outstanding
        return d


# ------------------------------------------------------------ level math ---

def parse_level(catalog: str) -> Optional[int]:
    """Numeric course level from a catalog number.

    Handles the formats my.harvard actually emits: "239", "112X", "134Y",
    "505M.40", "3500", "A819", "50".
    """
    if not catalog:
        return None
    m = re.match(r"\s*(\d+)", catalog)
    if not m:
        m = re.search(r"(\d+)", catalog)   # e.g. "A819"
    return int(m.group(1)) if m else None


def _in_ranges(n: int, ranges: list) -> bool:
    return any(lo <= n <= hi for lo, hi in ranges)


# --------------------------------------------------------------- results ---

@dataclass
class RequirementVerdict:
    requirement_id: str
    name: str
    verdict: str                    # YES | NO | UNKNOWN | VERIFY
    reason: str = ""


@dataclass
class CourseEligibility:
    code: str
    level: Optional[int]
    level_label: str
    level_short: str
    is_graduate: bool
    is_seas: bool
    counts_at_all: bool
    satisfies: list[RequirementVerdict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    is_cs50: bool = False
    is_core: bool = False
    is_technical: bool = False        # on the approved technical list
    is_project_based: bool = False    # on the approved GSD project-based list

    def satisfied_ids(self) -> list[str]:
        return [v.requirement_id for v in self.satisfies if v.verdict in (YES, VERIFY)]

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "level": self.level,
            "level_label": self.level_label,
            "level_short": self.level_short,
            "is_graduate": self.is_graduate,
            "is_seas": self.is_seas,
            "counts_at_all": self.counts_at_all,
            "is_cs50": self.is_cs50,
            "is_core": self.is_core,
            "is_technical": self.is_technical,
            "is_project_based": self.is_project_based,
            "warnings": self.warnings,
            "satisfies": [asdict(v) for v in self.satisfies],
            "satisfied_ids": self.satisfied_ids(),
        }


# ---------------------------------------------------------------- engine ---

def norm_code(s: str) -> str:
    return re.sub(r"[\s\-_.]", "", (s or "")).upper()


class Policy:
    def __init__(self, policy_path: Path = POLICY_PATH, lists_path: Path = LISTS_PATH):
        self.policy_path = policy_path
        self.lists_path = lists_path
        self.reload()

    def reload(self) -> None:
        self.p: dict[str, Any] = yaml.safe_load(self.policy_path.read_text()) or {}
        raw_lists = yaml.safe_load(self.lists_path.read_text()) or {}

        self.lists: dict[str, dict] = {}
        for name, body in raw_lists.items():
            if not isinstance(body, dict):
                continue
            codes = {norm_code(c) for c in (body.get("codes") or [])}
            # GSD project-based courses are given by catalog number only; the
            # subject prefix is not in the source spreadsheet, so match on
            # school=GSD + catalog rather than inventing a code.
            gsd_numbers = {str(n).strip() for n in (body.get("gsd_catalog_numbers") or [])}
            self.lists[name] = {
                "verified": bool(body.get("verified", False)),
                "codes": codes,
                "gsd_numbers": gsd_numbers,
                "loaded": bool(codes or gsd_numbers),
                "source": body.get("source", ""),
            }

        self.seas_subjects = set(self.p.get("seas", {}).get("subjects", []))
        self.seas_conditional = self.p.get("seas", {}).get("conditional_subjects", {}) or {}
        self.grad_schools = set(self.p.get("graduate_by_default_schools", []))
        self.outside_schools = set(self.p.get("outside_harvard_schools", []) or [])
        self.reqs = {r["id"]: r for r in self.p.get("requirements", [])}
        self.levels = self.p.get("fas_levels", {})
        self.cs50 = self.p.get("cs50", {}) or {}
        self.core_codes = {norm_code(c["code"]): c
                           for c in (self.p.get("core_codes") or []) if c.get("code")}
        self.below100 = self.p.get("below_100_rule", {}) or {}
        self.crosslist_cfg = self.p.get("crosslisting", {}) or {}

        self._override_by_code: dict[str, dict] = {}
        for ov in self.crosslist_cfg.get("overrides", []) or []:
            for c in ov.get("codes", []):
                self._override_by_code[norm_code(c)] = ov

    # -- list helpers -------------------------------------------------------

    def list_loaded(self, name: str) -> bool:
        return self.lists.get(name, {}).get("loaded", False)

    def in_list(self, name: str, course: dict) -> bool:
        """Is this course on the named approved list?

        Takes the whole course, not just a code, because GSD entries are keyed
        by catalog number with the subject left implicit.
        """
        spec = self.lists.get(name)
        if not spec:
            return False
        if norm_code(course.get("code") or "") in spec["codes"]:
            return True
        nums = spec.get("gsd_numbers")
        if nums and (course.get("school") or "").upper() == "GSD":
            if str(course.get("catalog") or "").strip() in nums:
                return True
        return False

    def missing_lists(self) -> list[str]:
        return [n for n, v in self.lists.items() if not v["loaded"]]

    # -- classification -----------------------------------------------------

    def level_band(self, course: dict) -> str:
        """FAS level band key, or 'graduate_school' / 'unknown'.

        Bands matter more than raw numbers: 1710 is numerically above 200 but
        sits in the 1000-1999 "For Undergraduates and Graduates" band, so it is
        NOT a graduate-level elective. Comparing `level >= 200` gets this wrong.
        """
        school = (course.get("school") or "").upper()
        if school in self.grad_schools:
            return "graduate_school"
        # MIT numbering ("1.000") encodes department, not level. Running it
        # through the FAS bands would classify every MIT course as sub-100 and
        # rule 3 would discard it.
        if school in self.outside_schools:
            return "outside_harvard"

        level = parse_level(course.get("catalog") or "")
        if level is None:
            return "unknown"

        below = self.levels.get("below_100", {})
        if level <= int(below.get("max", 99)):
            return "below_100"
        for key in ("ug_and_grad", "graduate", "grad_research"):
            spec = self.levels.get(key, {})
            if _in_ranges(level, spec.get("ranges", [])):
                return key
        return "unknown"

    def classify_level(self, course: dict) -> tuple[Optional[int], str, bool]:
        """Returns (level, label, is_graduate). See level_label_short for the
        compact form the UI puts on a badge."""
        level = parse_level(course.get("catalog") or "")
        band = self.level_band(course)

        if band == "graduate_school":
            return level, "Graduate school course", True
        if band == "outside_harvard":
            # Unknowable from the number; rule 5 still requires graduate level.
            return level, "Non-Harvard cross-registration", False
        if band == "unknown":
            return level, "Unknown level", False

        spec = self.levels.get(band, {})
        return level, spec.get("label", band), bool(spec.get("graduate", False))

    # Long labels wrap to two lines inside a pill, which is most of why the
    # course card looked disorganised. The full text stays in the tooltip.
    LEVEL_SHORT_FIXED = {
        "Graduate school course": "Graduate",
        "Non-Harvard cross-registration": "Cross-reg",
        "Unknown level": "Level unknown",
    }

    def level_label_short(self, label: str) -> str:
        if label in self.LEVEL_SHORT_FIXED:
            return self.LEVEL_SHORT_FIXED[label]
        for spec in self.levels.values():
            if isinstance(spec, dict) and spec.get("label") == label:
                return spec.get("short") or label
        return label

    def is_seas(self, course: dict) -> tuple[bool, Optional[str]]:
        """Returns (is_seas, caveat). SEAS is a subset of FAS."""
        school = (course.get("school") or "").upper()
        subject = (course.get("subject") or "").upper()
        seas_school = (self.p.get("seas", {}).get("school") or "FAS").upper()

        if school != seas_school:
            return False, None
        if subject in self.seas_subjects:
            return True, None
        if subject in self.seas_conditional:
            return True, self.seas_conditional[subject]
        return False, None

    def is_core(self, course: dict) -> bool:
        """Required MDE courses are not electives and never count toward the 9."""
        return norm_code(course.get("code") or "") in self.core_codes

    def is_cs50(self, course: dict) -> bool:
        return norm_code(course.get("code") or "") == norm_code(self.cs50.get("code", "COMPSCI50"))

    def looks_like_independent_study(self, course: dict) -> bool:
        spec = (self.reqs.get("independent_study", {}) or {}).get("detect", {}) or {}
        title = course.get("title") or ""
        if spec.get("title_regex") and re.search(spec["title_regex"], title, re.I):
            return True
        lo_hi = spec.get("fas_level_range")
        level = parse_level(course.get("catalog") or "")
        school = (course.get("school") or "").upper()
        if (lo_hi and level is not None and school not in self.grad_schools
                and school not in self.outside_schools):
            if lo_hi[0] <= level <= lo_hi[1]:
                return True
        return False

    # -- the main evaluation ------------------------------------------------

    def evaluate(self, course: dict, profile: StudentProfile,
                 crosslist: Optional[dict] = None) -> CourseEligibility:
        """Evaluate one course against one student's situation.

        `crosslist` is an optional record from the cross-list detector:
        {"schools": [...], "codes": [...], "partners": [...]}
        """
        code = course.get("code") or ""
        school = (course.get("school") or "").upper()
        level, level_label, is_grad = self.classify_level(course)
        seas, seas_caveat = self.is_seas(course)
        cs50 = self.is_cs50(course)

        el = CourseEligibility(
            code=code, level=level, level_label=level_label,
            level_short=self.level_label_short(level_label),
            is_graduate=is_grad, is_seas=seas, counts_at_all=True, is_cs50=cs50,
        )

        # A required core is not an elective. Returning early keeps it out of
        # the GSD/SEAS minimums -- otherwise every student's two cores would
        # appear to satisfy the GSD requirement on their own.
        if self.is_core(course):
            el.is_core = True
            el.counts_at_all = False
            core = self.core_codes[norm_code(code)]
            el.warnings.append(
                f"Required MDE core ({core.get('name', code)}) -- not an elective, "
                f"so it does not count toward the {self.p.get('electives_total', 9)}.")
            return el
        # A waived or already-completed CS50 is not a course the student will
        # take, so it cannot fill a SEAS elective slot. Rule 2 is a minimum of
        # two SEAS courses and nothing in the policy reduces that for a waiver.
        # validate_plan already skipped CS50 when tallying a plan, but evaluate()
        # still reported satisfies=[seas], so a petitioned CS50 was badged as a
        # SEAS course and matched the "requirement it satisfies: SEAS" filter.
        if cs50 and not profile.cs50_outstanding:
            el.counts_at_all = False
            el.warnings.append(
                f"CS50 already satisfied ({profile.cs50_status}) -- not a course you "
                f"still take, so it does not count toward the SEAS minimum.")
            return el
        if seas_caveat:
            el.warnings.append(f"{course.get('subject')}: {seas_caveat}")

        el.is_technical = self.in_list("technical", course)
        el.is_project_based = self.in_list("project_based", course)

        # --- Rule 3: below the 100 level -----------------------------------
        exceptions = {norm_code(c) for c in (self.below100.get("exceptions") or [])}
        # Rule 3 exempts "CS50 and any approved exceptions". The MDE
        # "Approved 0-100-level SEAS Electives" sheet contains sub-100 courses
        # (CS50, CS51, CS79, APMTH 10, ENG-SCI 51) -- being on it IS the
        # approval, so those are exceptions too.
        on_approved_low = self.in_list("seas_0_100", course)
        if (level is not None and school not in self.grad_schools
                and school not in self.outside_schools and level < 100):
            if norm_code(code) in exceptions or cs50 or on_approved_low:
                if cs50:
                    # A waived CS50 returned early above, so reaching here means
                    # the student still owes the course.
                    if profile.is_first_semester:
                        el.warnings.append(
                            "CS50 is required as an elective in your first semester "
                            "unless waived by online certificate or petition.")
                    else:
                        el.warnings.append(
                            "CS50 was due in your first semester -- confirm your standing "
                            "with the program office.")
                elif on_approved_low:
                    el.warnings.append(
                        "Below the 100 level, but on the MDE approved 0-100-level SEAS list "
                        "-- rule 3 exempts approved exceptions. Program director approval is "
                        "still required.")
            else:
                el.counts_at_all = False
                el.warnings.append(
                    "Rule 3: below the 100 level. Requires program director approval and is "
                    "normally an overload -- does not count toward elective requirements "
                    "or the 18-credit semester load.")

        # --- Cross-listing --------------------------------------------------
        # The whole GROUP decides, and this course is a member of it -- so the
        # course's own school/SEAS status must be counted alongside its partners.
        gsd_seas_crosslist = False
        blocked_from_gsd_seas = False
        if crosslist:
            members = [{"school": school, "is_seas": seas}] + [
                {"school": (p.get("school") or "").upper(), "is_seas": bool(p.get("is_seas"))}
                for p in crosslist.get("partners", [])
            ]
            has_gsd = any(m["school"] == "GSD" for m in members)
            has_seas = any(m["is_seas"] for m in members)
            has_nonseas_fas = any(m["school"] == "FAS" and not m["is_seas"] for m in members)
            has_other_school = any(m["school"] not in ("GSD", "FAS") for m in members)

            codes_str = " / ".join(crosslist.get("codes", []))
            conf = crosslist.get("confidence", "medium")

            # Credit follows the code you enroll under. The policy restricts
            # "FAS courses (or courses from other Harvard schools) that are
            # cross-listed with the GSD and/or SEAS" -- i.e. THOSE listings.
            # The GSD-coded listing of the same class is still a GSD course, so
            # only block when this course is itself the outside listing.
            self_is_gsd_or_seas = (school == "GSD") or seas
            if not self_is_gsd_or_seas and (has_gsd or has_seas):
                blocked_from_gsd_seas = True
                el.warnings.append(
                    f"This is the non-GSD/non-SEAS listing of a cross-listed class "
                    f"({codes_str}): per policy it cannot count as a GSD or SEAS elective. "
                    f"Enrol under the GSD or SEAS code if you need that credit. "
                    f"[detected, {conf} confidence -- verify]")
            if has_gsd and has_seas and self_is_gsd_or_seas:
                gsd_seas_crosslist = True
                el.warnings.append(
                    f"Cross-listed GSD/SEAS ({codes_str}): may count toward the GSD **or** "
                    f"SEAS requirement, but not both. [detected, {conf} confidence -- verify]")
                # A SEAS course cross-listed with the GSD is adjusted for graduate
                # students, so it counts as graduate level via GSD enrollment.
                if self.crosslist_cfg.get("seas_via_gsd_is_graduate") and not is_grad:
                    is_grad = True
                    el.is_graduate = True
                    el.warnings.append(
                        "Counts as graduate level via GSD enrollment (a SEAS course "
                        "cross-listed with the GSD is adjusted for graduate students).")

        override = self._override_by_code.get(norm_code(code))
        if override:
            el.warnings.append(override.get("note", "Policy override applies."))

        indep = self.looks_like_independent_study(course)

        # --- Requirement-by-requirement ------------------------------------
        if el.counts_at_all:
            el.satisfies.append(self._eval_gsd(
                course, profile, school, is_grad, indep, blocked_from_gsd_seas,
                override, gsd_seas_crosslist))
            el.satisfies.append(self._eval_seas(
                course, profile, seas, is_grad, indep, blocked_from_gsd_seas,
                override, gsd_seas_crosslist))
            el.satisfies.append(self._eval_fas_non_seas(course, school, seas, is_grad))
            el.satisfies.append(self._eval_other_harvard(school))
            el.satisfies.append(self._eval_outside_harvard(course, school))
            if indep:
                el.satisfies.append(RequirementVerdict(
                    "independent_study", self.reqs["independent_study"]["name"], VERIFY,
                    "Looks like an independent study / reading course: at most one is "
                    "allowed, it needs a petition, and it cannot satisfy the GSD or SEAS "
                    "requirement. Confirm the course type."))

        return el

    def _eval_gsd(self, course, profile, school, is_grad, indep,
                  blocked, override, gsd_seas_crosslist=False) -> RequirementVerdict:
        req = self.reqs["gsd"]
        name = req["name"]
        # A GSD<->SEAS cross-listing counts at EITHER school, so the SEAS-coded
        # listing of such a course is still eligible for the GSD requirement.
        if school != "GSD" and not gsd_seas_crosslist:
            return RequirementVerdict("gsd", name, NO, "Not a GSD course.")
        if override and override.get("counts_only_as") == "seas":
            return RequirementVerdict("gsd", name, NO,
                                      "Policy override: this course counts only as SEAS.")
        if blocked:
            return RequirementVerdict("gsd", name, NO,
                                      "Cross-listed outside GSD/SEAS -- cannot count as a GSD elective.")
        if indep:
            return RequirementVerdict("gsd", name, NO,
                                      "Independent studies cannot satisfy the GSD requirement (rule 7).")
        if not is_grad:
            return RequirementVerdict("gsd", name, NO, "Must be graduate level.")

        # Rule 1a applies only to students without a physical design background.
        if profile.physical_design_background:
            return RequirementVerdict("gsd", name, YES,
                                      "Counts toward the 2-course GSD requirement.")

        sub = req.get("conditional_sub_requirement", {}) or {}
        list_name = sub.get("approved_list", "project_based")
        if not self.list_loaded(list_name):
            return RequirementVerdict(
                "gsd", name, VERIFY,
                "Counts toward the GSD requirement. One of your two GSD courses must be "
                "project-based (you have no physical design background), but the approved "
                "project-based list has not been loaded -- cannot verify that here.")
        if self.in_list(list_name, course):
            return RequirementVerdict("gsd", name, YES,
                                      "Counts toward the GSD requirement, and is project-based.")
        return RequirementVerdict("gsd", name, YES,
                                  "Counts toward the GSD requirement, but is NOT on the "
                                  "approved project-based list -- your other GSD course must be.")

    def _eval_seas(self, course, profile, seas, is_grad, indep,
                   blocked, override, gsd_seas_crosslist=False) -> RequirementVerdict:
        req = self.reqs["seas"]
        name = req["name"]
        # A GSD<->SEAS cross-listing counts at either school, so a GSD-coded
        # listing (e.g. SCI 6272) is still eligible for the SEAS requirement.
        if not seas and not gsd_seas_crosslist:
            return RequirementVerdict("seas", name, NO, "Not a SEAS course.")
        if blocked:
            return RequirementVerdict("seas", name, NO,
                                      "Cross-listed outside GSD/SEAS -- cannot count as a SEAS elective.")
        if indep:
            return RequirementVerdict("seas", name, NO,
                                      "Independent studies cannot satisfy the SEAS requirement (rule 7).")

        subject = (course.get("subject") or "").upper()
        pol = req.get("level_policy", {}) or {}
        band = self.level_band(course)

        # `is_grad` already reflects the band, plus the GSD-enrollment uplift for
        # cross-listed courses. Use it rather than a raw numeric threshold: 1710
        # is > 200 numerically but sits in the undergraduate/graduate band.
        if band == "unknown":
            return RequirementVerdict("seas", name, UNKNOWN, "Course level could not be parsed.")

        if profile.seas_background:
            in_area = subject in {a.upper() for a in profile.seas_areas}
            if is_grad:
                return RequirementVerdict("seas", name, YES,
                                          "Counts toward the 2-course SEAS requirement.")
            if in_area:
                return RequirementVerdict(
                    "seas", name, NO,
                    f"{subject} is one of your areas of study, so it must be graduate level "
                    f"(2xx / 2xxx). Switching your area of study may be petitioned.")
            return RequirementVerdict(
                "seas", name, NO,
                "With a SEAS background you are expected to take graduate-level SEAS "
                "courses. The approved 0-100-level allowance applies only to students "
                "without a SEAS background.")

        # No SEAS background.
        spec = pol.get("without_seas_background", {})
        if is_grad:
            return RequirementVerdict("seas", name, YES,
                                      "Counts toward the 2-course SEAS requirement.")
        # The approved list is explicitly "0-100-level", so it covers the
        # below_100 band as well as 100-199 / 1000-1999.
        if spec.get("allow_100_level_if_approved") and band in ("ug_and_grad", "below_100"):
            list_name = spec.get("approved_list", "seas_0_100")
            if not self.list_loaded(list_name):
                return RequirementVerdict(
                    "seas", name, UNKNOWN,
                    "100-level SEAS course. Without a SEAS background you may count an "
                    "APPROVED 0-100-level course, but that list has not been loaded.")
            if self.in_list(list_name, course):
                return RequirementVerdict("seas", name, YES,
                                          "On the approved 0-100-level list -- counts toward SEAS.")
            return RequirementVerdict("seas", name, NO,
                                      "100-level SEAS course not on the approved 0-100-level list.")
        return RequirementVerdict("seas", name, NO, "Below the allowed level.")

    def _eval_fas_non_seas(self, course, school, seas, is_grad) -> RequirementVerdict:
        req = self.reqs["fas_non_seas"]
        name = req["name"]
        if school != "FAS" or seas:
            return RequirementVerdict("fas_non_seas", name, NO, "Not a non-SEAS FAS course.")
        if not is_grad:
            return RequirementVerdict(
                "fas_non_seas", name, NO,
                "FAS courses must be graduate level (200+/2000+). A 'For Undergraduates and "
                "Graduates' course counts only if the instructor modifies it for graduate "
                "students, with written proof submitted to the program office.")
        return RequirementVerdict("fas_non_seas", name, YES, "Counts, up to a maximum of 4.")

    def _eval_outside_harvard(self, course, school) -> RequirementVerdict:
        req = self.reqs["outside_harvard"]
        name = req["name"]
        if school not in self.outside_schools:
            return RequirementVerdict("outside_harvard", name, NO, "Not a cross-registration course.")
        return RequirementVerdict(
            "outside_harvard", name, VERIFY,
            "Counts toward the max-4 outside-Harvard cap (rule 5), but it must be "
            "graduate level -- MIT course numbers encode department, not level, so "
            "confirm this yourself. my.harvard lists no meeting time for these; add "
            "a custom block with the real time so conflict checking works.")

    def _eval_other_harvard(self, school) -> RequirementVerdict:
        req = self.reqs["other_harvard"]
        name = req["name"]
        allowed = {s.upper() for s in (req.get("match", {}).get("school") or [])}
        if school in allowed:
            return RequirementVerdict("other_harvard", name, YES, "Counts, up to a maximum of 4.")
        return RequirementVerdict("other_harvard", name, NO, "Not a non-GSD Harvard graduate school.")

    # -- plan-level validation ---------------------------------------------

    def validate_plan(self, courses: list[dict], profile: StudentProfile,
                      crosslists: Optional[dict] = None,
                      outside_harvard_count: int = 0) -> dict:
        """Check a full set of electives against the minimums and caps.

        `courses` are catalog rows (planned + completed). Cross-listed GSD/SEAS
        courses are assigned greedily to whichever requirement still needs them,
        since policy allows either but not both.
        """
        crosslists = crosslists or {}
        evaluated = []
        for c in courses:
            el = self.evaluate(c, profile, crosslists.get(c.get("key")))
            evaluated.append((c, el))

        counted = [(c, el) for c, el in evaluated if el.counts_at_all and not el.is_cs50]

        # Assign GSD/SEAS. Courses that can only go one way are placed first.
        gsd_only, seas_only, either = [], [], []
        for c, el in counted:
            ids = set(el.satisfied_ids())
            if "gsd" in ids and "seas" in ids:
                either.append((c, el))
            elif "gsd" in ids:
                gsd_only.append((c, el))
            elif "seas" in ids:
                seas_only.append((c, el))

        gsd = list(gsd_only)
        seas = list(seas_only)
        for c, el in either:
            if len(seas) < 2 and len(seas) <= len(gsd):
                seas.append((c, el))
            else:
                gsd.append((c, el))

        def codes(pairs):
            return [p[0].get("code") for p in pairs]

        issues, satisfied, unverifiable = [], [], []

        # Rule 1 -- GSD minimum
        if len(gsd) >= 2:
            satisfied.append(f"GSD: {len(gsd)}/2 courses.")
        else:
            issues.append(f"GSD: {len(gsd)}/2 courses (rule 1 requires at least 2, 8 credits).")

        # Rule 1a -- project-based, only without a physical design background
        if not profile.physical_design_background:
            if not self.list_loaded("project_based"):
                unverifiable.append(
                    "Rule 1a: one GSD course must be project-based, but the approved "
                    "project-based list has not been loaded.")
            else:
                n = sum(1 for c, _ in gsd if self.in_list("project_based", c))
                if n >= 1:
                    satisfied.append("GSD project-based requirement met.")
                else:
                    issues.append("Rule 1a: none of your GSD courses is on the approved "
                                  "project-based list.")

        # Rule 2 -- SEAS minimum
        if len(seas) >= 2:
            satisfied.append(f"SEAS: {len(seas)}/2 courses.")
        else:
            issues.append(f"SEAS: {len(seas)}/2 courses (rule 2 requires at least 2, 8 credits).")

        # Rule 2a-ii / 2b-ii -- technical, all students
        if not self.list_loaded("technical"):
            unverifiable.append(
                "Rule 2: at least one SEAS course must be technical, but the approved "
                "technical list has not been loaded.")
        else:
            n = sum(1 for c, _ in seas if self.in_list("technical", c))
            if n >= 1:
                satisfied.append("SEAS technical requirement met.")
            else:
                issues.append("Rule 2: none of your SEAS courses is on the approved "
                              "technical list.")

        # Caps
        caps = []
        n_fas = sum(1 for _, el in counted if "fas_non_seas" in el.satisfied_ids())
        n_other = sum(1 for _, el in counted if "other_harvard" in el.satisfied_ids())
        n_indep = sum(1 for _, el in counted if "independent_study" in el.satisfied_ids())
        n_outside = sum(1 for _, el in counted
                        if "outside_harvard" in el.satisfied_ids()) + outside_harvard_count
        for cap_id, n in (("fas_non_seas", n_fas), ("other_harvard", n_other),
                          ("independent_study", n_indep),
                          ("outside_harvard", n_outside)):
            req = self.reqs[cap_id]
            limit = int(req.get("max_courses", 0))
            caps.append({"id": cap_id, "name": req["name"], "count": n, "max": limit})
            if n > limit:
                issues.append(f"{req['name']}: {n} exceeds the maximum of {limit} (rule {req['rule']}).")

        total = len(counted)
        if total > self.p.get("electives_total", 9):
            issues.append(f"{total} counting electives exceeds the {self.p['electives_total']} required.")

        # CS50
        cs50_note = None
        if profile.cs50_outstanding:
            has = any(el.is_cs50 for _, el in evaluated)
            if has:
                cs50_note = "CS50 included as a first-semester elective."
            else:
                cs50_note = ("CS50 is still outstanding. It must be taken in your first "
                             "semester unless waived by online certificate or petition.")
        else:
            cs50_note = f"CS50 satisfied ({profile.cs50_status})."

        return {
            "profile": profile.to_dict(),
            "electives_this_term": self.electives_this_term(profile),
            "counted_total": total,
            "electives_required": self.p.get("electives_total", 9),
            "assignment": {"gsd": codes(gsd), "seas": codes(seas)},
            "satisfied": satisfied,
            "issues": issues,
            "unverifiable": unverifiable,
            "caps": caps,
            "cs50": cs50_note,
            "cores": [c.get("code") for c, el in evaluated if el.is_core],
            "excluded": [
                {"code": c.get("code"),
                 "title": c.get("title"),
                 "reason": el.warnings[0] if el.warnings else
                           "Does not satisfy any elective requirement."}
                for c, el in evaluated if not el.counts_at_all and not el.is_core
            ],
            "counts_but_unassigned": [
                {"code": c.get("code"), "title": c.get("title"),
                 "reason": next((v.reason for v in el.satisfies
                                 if v.requirement_id == "fas_non_seas" and v.verdict == NO),
                                "Does not satisfy any elective requirement.")}
                for c, el in counted if not el.satisfied_ids()
            ],
        }

    def electives_this_term(self, profile: StudentProfile) -> int:
        return int((self.p.get("electives_by_term") or {}).get(profile.term_slot, 2))

    def as_dict(self) -> dict:
        return {
            "program": self.p.get("program"),
            "policy_version": self.p.get("policy_version"),
            "electives_total": self.p.get("electives_total"),
            "electives_by_term": self.p.get("electives_by_term"),
            "cores_by_term": self.p.get("cores_by_term"),
            "seas_subjects": sorted(self.seas_subjects),
            "requirements": [
                {"id": r["id"], "rule": r.get("rule"), "name": r["name"],
                 "short": r.get("short") or r["name"],
                 "kind": r.get("kind"), "min_courses": r.get("min_courses"),
                 "max_courses": r.get("max_courses")}
                for r in self.p.get("requirements", [])
            ],
            "lists": {n: {"loaded": v["loaded"], "verified": v["verified"],
                          "count": len(v["codes"]) + len(v.get("gsd_numbers") or ()),
                          "source": v["source"]}
                      for n, v in self.lists.items()},
            "missing_lists": self.missing_lists(),
        }
