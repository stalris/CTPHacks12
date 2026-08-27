"""Semester suggestion API.

    python backend/server.py            # http://localhost:8000

POST /api/schedule-preferences  {"prompt": "...", "profile": {...}, "feedback": "..."}
  -> Gemini-translated hard availability + bounded schedule-ranking weights

POST /api/suggest  {"program": "CSCI-BS", "terms": [[courseId...]...], "term": "Fall"|"Spring"|"Summer"|"Winter",
                    "pins": [courseId...], "queue": [courseId...], "preferences": {"preferredSubjects": [],
                    "avoidedSubjects": []}, "fresh": bool, "ai": bool (default true; false = rule-based, no Gemini call)}
  -> {"suggested": [{"id","reason","unlocks":[ids]}], "candidates": [...same...], "progress": {...}, "source": "gemini"|"heuristic"}
POST /api/chat     {"program", "terms", "term", "messages": [{"role","text"}]} -> {"reply", "source", "track"}   advisor chatbot
                   ("fastest track" questions also return track: [{"term", "courses": [ids]}] built from the approved terms)

The eligible set is computed here (prereqs met, not taken, offered that term, still needed by the major or
Pathways). Pinned/queued courses that are eligible are locked into the term first; Gemini then chooses the rest from
the eligible set, and its picks go through the same guards as the rule-based picker (credit cap, subject cap, coreq
partner, one course per Pathways slot). Responses are cached per input; "fresh" bypasses the cache (Regenerate).
"""
import csv, json, os, re, urllib.error, urllib.request
from collections import Counter
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from optimizer import bundle_components, explore_course_schedules

ROOT = Path(__file__).resolve().parent
for _l in (ROOT / ".env").read_text().splitlines() if (ROOT / ".env").exists() else []:   # ponytail: KEY=value lines, no quoting rules
    if "=" in _l and not _l.startswith("#"):
        os.environ.setdefault(_l.split("=", 1)[0].strip(), _l.split("=", 1)[1].strip())
DATA = ROOT.parent / "frontend" / "public" / "data"
MODEL = "gemini-3.7-flash"
FALLBACK = "gemini-3.6-flash"   # ponytail: used only when 3.7 answers 503 (overloaded)
TARGET_CREDITS, MAX_CREDITS, MIN_COURSES = 15, 20, 5   # aim for ~15 cr and >=5 courses, never above 20 cr

courses = {c["id"]: c for c in json.loads((DATA / "courses.json").read_text(encoding="utf8"))}
programs = {p["id"]: p for p in json.loads((DATA / "programs.json").read_text(encoding="utf8"))}
prereqs = json.loads((DATA / "prereqs.json").read_text(encoding="utf8")) if (DATA / "prereqs.json").exists() else {}
by_code = {c["code"]: c["id"] for c in courses.values()}
# CS department's approved non-CS electives (cs.qc.cuny.edu/approved_nonCSelective.html): at most ONE may count toward
# the CSCI elective credits, and not if it already satisfies the math requirement (the fixed-course filter below handles that).
# Coursedog's "Computer Science Electives" set omits them, so they're spliced in here rather than in the scraped data.
NONCS_ELECTIVES = ("BIOL 330 MATH 202 MATH 223 MATH 224 MATH 231 MATH 232 MATH 242 MATH 245 MATH 247 MATH 248 MATH 301 MATH 317 "
                   "MATH 337 MATH 341 MATH 342 MATH 609 MATH 613 MATH 619 MATH 621 MATH 623 MATH 624 MATH 625 MATH 626 MATH 633 "
                   "MATH 634 MATH 635 MATH 636 PHYS 225 PHYS 227 PHYS 265 PHYS 311")
_noncs = [by_code[c] for c in re.findall(r"[A-Z]+ \d+", NONCS_ELECTIVES) if c in by_code]
for _pid in ("CSCI-BS", "CSCI-BA"):
    for _r in programs.get(_pid, {"requirements": []})["requirements"]:
        for _ru in _r["rules"]:
            if _ru.get("set") and _ru["kind"] == "credits":
                _ru["options"] += [[i] for i in _noncs if [i] not in _ru["options"]]
                _ru["cap"] = {"ids": _noncs, "n": 1}
for _p in programs.values():                                   # a course required elsewhere in the major can't also be an elective
    _fixed = {i for r in _p["requirements"] for ru in r["rules"] if not ru.get("set") for o in ru["options"] for i in o}
    for r in _p["requirements"]:
        for ru in r["rules"]:
            if ru.get("set"):
                ru["options"] = [o for o in ru["options"] if not set(o) & _fixed]
                if ru.get("cap"):                                  # a required MATH course can't consume the one non-CS slot
                    ru["cap"]["ids"] = [i for i in ru["cap"]["ids"] if i not in _fixed]
coreqs = set(json.loads((DATA / "coreqs.json").read_text())) if (DATA / "coreqs.json").exists() else set()
source = json.loads((DATA / "prereq_source.json").read_text()) if (DATA / "prereq_source.json").exists() else {}   # provenance

TERM_ORDER = {"Winter": 0, "Spring": 1, "Summer": 2, "Fall": 3}
NON_COMPLETION_GRADES = {"F", "FIN", "W", "WA", "WD", "WN", "WU", "INC", "IP", "NC", "AUD"}
COURSE_ROW = re.compile(
    r"\b([A-Z]{2,5})\s+(\d{1,4}[A-Z]?)\b\s+(.+?)\s+"
    r"([A-F][+-]?|FIN|P|CR|S|IP|W|WA|WD|WN|WU|INC|NC|AUD)\s+"
    r"(?:\((\d+(?:\.\d+)?)\)|(\d+(?:\.\d+)?))\s+"
    r"(FALL|SPRING|SUMMER|WINTER)\s+(\d{4})"
)
COURSE_CODE = re.compile(r"\b[A-Z]{2,5}\s+\d{1,4}[A-Z]?\b")
GREEN = (0.0, 0.502, 0.302)


TRANSFER_ROW = re.compile(r"Satisfied by:\s*-\s*(.+?)\s*$")
NOT_APPLIED = re.compile(r"\s*(Elective Courses Not Allowed|Fall-?through|Insufficient Grades|In-progress|Not Counted)\b")


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def program_from_audit(text):
    major = re.search(r"\bMajor\s+(.+?)\s+Concentration\b", text)
    degree = re.search(r"\bDegree\s+(.+?)\s+Audit date\b", text)
    major_label = major.group(1).strip() if major else ""
    degree_label = degree.group(1).strip() if degree else ""
    abbr = next((a for a in ("BA", "BS", "BBA", "BFA", "BM", "MA", "MS") if re.search(rf"\b{a}\b", f"{major_label} {degree_label}")), "")
    major_name = re.sub(r"\b(BA|BS|BBA|BFA|BM|MA|MS)\b", "", major_label).strip()
    for pid, p in programs.items():
        if abbr and not pid.endswith(f"-{abbr}"):
            continue
        if norm(p["name"]) and norm(p["name"]) in norm(major_name or major_label):
            return pid, major_label, degree_label
    return None, major_label, degree_label


def parse_degreeworks_text(text):
    program_id, major_label, degree_label = program_from_audit(text)
    seen, parsed, grouped, last = set(), [], {}, None
    for line in text.splitlines():
        if NOT_APPLIED.match(line):     # everything after these headers is listed but applies to no requirement
            break
        transfer = TRANSFER_ROW.search(line)
        if transfer and parsed:
            school = transfer.group(1).strip()
            parsed[-1]["transfer"] = school
            grouped[parsed[-1]["key"]].setdefault("transfers", {})[parsed[-1]["id"]] = school
            continue
        for m in COURSE_ROW.finditer(line):
            code = f"{m.group(1)} {m.group(2)}"
            cid = by_code.get(code)
            grade = m.group(4)
            credits = float(m.group(5) or m.group(6) or 0)
            # No grade floor here: DegreeWorks already applies each department's floor by moving a low grade into the
            # "Not Allowed" block (cut off above) or zeroing its credits. IP rows DO apply — DegreeWorks counts them.
            if (grade in NON_COMPLETION_GRADES and grade != "IP") or credits <= 0:
                continue
            term = m.group(7).title()
            year = int(m.group(8))
            key = (year, TERM_ORDER[term])
            grouped.setdefault(key, {"name": f"{term} {year}", "kind": term, "courses": [], "extra": 0})
            # DegreeWorks lists the same course in several requirement blocks; count it once per line-item, not once per block.
            # ponytail: repeated placeholder rows (ARTS 499 x6) are consecutive, cross-block repeats never are — consecutive = distinct
            row = (code, grade, credits, key)
            if row in seen and last != (code, key):
                continue
            seen.add(row)
            last = (code, key)
            if not cid:                                            # not in our catalog (e.g. a transfer-only code) — credits still count
                grouped[key]["extra"] += credits
                continue
            grouped[key]["courses"].append(cid)
            parsed.append({"id": cid, "code": code, "grade": grade, "credits": credits, "term": term, "year": year, "transfer": None, "key": key})
    for p in parsed:
        del p["key"]
    return {
        "program": program_id,
        "major": major_label or None,
        "degree": degree_label or None,
        "terms": [grouped[k] for k in sorted(grouped)],
        "courses": parsed,
    }


def extract_audit_pdf(data):
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError("Install pypdf first: python -m pip install -r backend/requirements.txt") from e
    reader = PdfReader(BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _mmul(a, b):
    return (
        a[0] * b[0] + a[2] * b[1],
        a[1] * b[0] + a[3] * b[1],
        a[0] * b[2] + a[2] * b[3],
        a[1] * b[2] + a[3] * b[3],
        a[0] * b[4] + a[2] * b[5] + a[4],
        a[1] * b[4] + a[3] * b[5] + a[5],
    )


def _pt(m, x, y):
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def _color(vals):
    try:
        return tuple(round(float(v), 4) for v in vals)
    except Exception:
        return ()


def _green(c):
    return len(c) == 3 and all(abs(a - b) < 0.02 for a, b in zip(c, GREEN))


def _page_text_lines(page, pageno):
    fragments = []

    def visitor(text, cm, tm, font_dict, font_size):
        t = " ".join(text.split())
        if not t:
            return
        m = _mmul(tuple(float(x) for x in cm), tuple(float(x) for x in tm))
        fragments.append({"page": pageno, "x": m[4], "y": m[5], "font": float(font_size), "text": t})

    page.extract_text(visitor_text=visitor)
    rows = []
    for frag in sorted(fragments, key=lambda f: (-f["y"], f["x"])):
        row = next((r for r in rows if abs(r["y"] - frag["y"]) <= 2.5), None)
        if not row:
            row = {"page": pageno, "y": frag["y"], "x": frag["x"], "font": frag["font"], "parts": []}
            rows.append(row)
        row["x"] = min(row["x"], frag["x"])
        row["font"] = max(row["font"], frag["font"])
        row["parts"].append(frag)
    for row in rows:
        row["text"] = " ".join(p["text"] for p in sorted(row["parts"], key=lambda p: p["x"]))
        row["codes"] = [c for c in COURSE_CODE.findall(row["text"]) if c in by_code]
        row["green"] = False
    return rows


def _green_marks(page, reader):
    from pypdf.generic import ContentStream

    marks, path, stack = [], [], [{"ctm": (1, 0, 0, 1, 0, 0), "fill": (), "stroke": ()}]
    contents = page.get_contents()
    if contents is None:
        return marks
    for operands, op in ContentStream(contents, reader).operations:
        op = op.decode() if isinstance(op, bytes) else op
        st = stack[-1]
        if op == "q":
            stack.append({"ctm": st["ctm"], "fill": st["fill"], "stroke": st["stroke"]})
        elif op == "Q":
            if len(stack) > 1:
                stack.pop()
        elif op == "cm":
            st["ctm"] = _mmul(st["ctm"], tuple(float(x) for x in operands))
        elif op == "rg":
            st["fill"] = _color(operands)
        elif op == "RG":
            st["stroke"] = _color(operands)
        elif op == "g":
            st["fill"] = (round(float(operands[0]), 4),)
        elif op == "G":
            st["stroke"] = (round(float(operands[0]), 4),)
        elif op in ("m", "l"):
            path.append(_pt(st["ctm"], float(operands[0]), float(operands[1])))
        elif op == "c":
            for i in (0, 2, 4):
                path.append(_pt(st["ctm"], float(operands[i]), float(operands[i + 1])))
        elif op == "re":
            x, y, w, h = [float(v) for v in operands]
            path += [_pt(st["ctm"], px, py) for px, py in ((x, y), (x + w, y), (x + w, y + h), (x, y + h))]
        elif op in ("f", "F", "f*", "S", "B", "B*"):
            fill = op in ("f", "F", "f*", "B", "B*")
            stroke = op in ("S", "B", "B*")
            if path and ((fill and _green(st["fill"])) or (stroke and _green(st["stroke"]))):
                xs, ys = [p[0] for p in path], [p[1] for p in path]
                x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
                if 5 <= x1 - x0 <= 14 and 5 <= y1 - y0 <= 14 and x0 < 70:
                    marks.append({"x": (x0 + x1) / 2, "y": (y0 + y1) / 2})
            path = []
        elif op == "n":
            path = []
    return marks


def _titleish(line):
    text = line["text"].strip()
    if not text or COURSE_CODE.search(text) or text.startswith(("Still needed:", "Credits applied:", "Course Title")):
        return False
    if text.isupper() and len(text) >= 4:
        return True
    return line["font"] >= 12 and not any(x in text for x in ("Credits applied", "Course Title"))


def _codes_between(lines, start, title):
    out, seen = [], set()
    collected = False
    last_code_y = None
    start_x = lines[start]["x"]
    broad = "MATH REQUIREMENT" in title.upper()
    broad_floor = None
    if broad:
        boundary = next((line for line in lines[start + 1:] if line["text"].strip().startswith(("Electives", "Insufficient Grades", "In-progress", "Legend"))), None)
        broad_floor = boundary["y"] + 14 if boundary else None
    else:
        green_labels = [line for line in lines[start + 1:] if line.get("green") and not line["codes"] and line["x"] <= start_x + 8]
        broad_floor = green_labels[1]["y"] + 14 if len(green_labels) > 1 else None
    for line in lines[start + 1:]:
        text = line["text"].strip()
        if broad_floor is not None and line["y"] <= broad_floor:
            break
        if text.startswith(("Still needed:", "Insufficient Grades", "In-progress", "Course Title", "Legend")):
            break
        if broad and text.startswith("Electives"):
            break
        if not broad and collected and line.get("green") and not line["codes"] and line["x"] <= start_x + 8 and (last_code_y is None or last_code_y - line["y"] > 14):
            break
        if not broad and collected and _titleish(line):
            break
        for code in line["codes"]:
            if code not in seen:
                out.append(by_code[code])
                seen.add(code)
                collected = True
                last_code_y = line["y"]
    return out


def extract_completed_requirements(data):
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    completed = []
    for pageno, page in enumerate(reader.pages, 1):
        lines = _page_text_lines(page, pageno)
        marks = _green_marks(page, reader)
        for line in lines:
            line["green"] = any(abs(line["y"] - mark["y"]) <= 5 and abs(line["x"] - mark["x"]) <= 45 for mark in marks)
        page_titles = []
        for i, line in enumerate(lines):
            text = re.sub(r"\s+(COMPLETE|STILL NEEDED)$", "", line["text"]).strip()
            if not (_titleish(line) and (line["green"] or line["text"].endswith(" COMPLETE"))):
                continue
            courses_for_req = _codes_between(lines, i, text)
            if courses_for_req or "REQUIREMENT" in text.upper():
                page_titles.append({"title": text, "x": line["x"], "idx": i, "courses": courses_for_req})
        for item in page_titles:
            parent = next((p["title"] for p in reversed(page_titles) if p["idx"] < item["idx"] and p["x"] < item["x"] - 2), None)
            completed.append({"title": item["title"], "parent": parent, "courses": item["courses"], "page": pageno})
    return completed


def parse_audit_pdf(data):
    text = extract_audit_pdf(data)
    audit = parse_degreeworks_text(text)
    audit["completedRequirements"] = extract_completed_requirements(data)
    return audit


def multipart_file(headers, data):
    content_type = headers.get("content-type", "")
    m = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", content_type)
    if not m:
        return None
    boundary = ("--" + (m.group(1) or m.group(2))).encode()
    for part in data.split(boundary):
        if b"filename=" not in part or b"\r\n\r\n" not in part:
            continue
        _, _, body = part.partition(b"\r\n\r\n")
        return body.rsplit(b"\r\n", 1)[0]
    return None

# Real class sections from CUNYfirst (backend/sections.py): {termId: {courseId: [section...]}}.
sections = json.loads((DATA / "sections.json").read_text(encoding="utf8")) if (DATA / "sections.json").exists() else {}
_meta = json.loads((DATA / "sections_meta.json").read_text(encoding="utf8")) if (DATA / "sections_meta.json").exists() else {}
SEASON = _meta.get("season", {})              # {"Fall": ["1269"], ...} -- term ids live in sections.py

# ---- Pathways (CUNY general education) -----------------------------------------------------------
AREA = {"English Composition": "EC", "Mathematical&QuantitativeReasoning": "MQR", "Life and Physical Sciences": "LPS",
        "World Cultures": "WCGI", "US Experience": "USED", "Creative Expression": "CE",
        "Individual and Society": "IS", "Scientific World": "SW"}
OPTION = {"Literature": "LIT", "Language": "LANG", "Science": "SCI", "Synthesis": "SYN", "Writing Intensive": "W"}
FLEX = {"WCGI", "USED", "CE", "IS", "SW"}
CO = {"LIT", "LANG", "SCI", "CO4"}   # College Option slots
gened = {}   # courseId -> {"area": "SW"|None, "opts": {"SCI", "W", ...}}
with open(ROOT / "data" / "gened.csv", encoding="utf8", errors="ignore") as f:
    for row in csv.DictReader(f):
        cid = by_code.get(row["Course"].strip())
        if not cid:
            continue
        g = gened.setdefault(cid, {"area": None, "opts": set()})
        g["area"] = next((v for k, v in AREA.items() if k in row["Pathways Area"]), g["area"])
        g["opts"] |= {v for k, v in OPTION.items() if k in row["Writing Intesive / College Options"]}
for cid, c in courses.items():                                   # any "W" course is Writing Intensive
    if c["code"].endswith("W"):
        gened.setdefault(cid, {"area": None, "opts": set()})["opts"].add("W")

# Structural rule from Pathways policy (independent of any course text): College Writing 2 requires College Writing 1.
_ec1 = [by_code[c] for c in ("ENGL 110", "ENGL 110H") if c in by_code]
for cid, g in gened.items():
    if g["area"] == "EC" and cid not in _ec1 and not any(set(grp) & set(_ec1) for grp in prereqs.get(cid, [])):
        prereqs.setdefault(cid, []).append(_ec1)

# (slot id, label, fit(courseId)) — in the order we fill them.  One course fills one slot; W overlaps.
def _area(a): return lambda cid: gened.get(cid, {}).get("area") == a
def _opt(o): return lambda cid: o in gened.get(cid, {}).get("opts", ())
SLOTS = [
    ("EC1", "College Writing 1 (ENGL 110)", lambda cid: courses[cid]["code"] in ("ENGL 110", "ENGL 110H")),
    ("EC2", "College Writing 2", _area("EC")),
    ("MQR", "Math & Quantitative Reasoning", _area("MQR")),
    ("LPS", "Life & Physical Sciences", _area("LPS")),
    ("WCGI", "World Cultures & Global Issues", _area("WCGI")),
    ("USED", "US Experience in its Diversity", _area("USED")),
    ("CE", "Creative Expression", _area("CE")),
    ("IS", "Individual & Society", _area("IS")),
    ("SW", "Scientific World", _area("SW")),
    ("FLEX", "Additional Flexible Core", lambda cid: gened.get(cid, {}).get("area") in FLEX),
    ("LIT", "College Option: Literature", _opt("LIT")),
    ("LANG", "College Option: Language", _opt("LANG")),
    ("SCI", "College Option: Science", _opt("SCI")),
    ("CO4", "College Option: Synthesis / additional", lambda cid: _opt("SYN")(cid) or gened.get(cid, {}).get("area") in FLEX | {"LPS"} or bool(gened.get(cid, {}).get("opts", set()) & {"LIT", "LANG", "SCI"})),
]


def pathways(taken):
    """Maximum bipartite matching of courses to slots (augmenting paths): one course fills one slot, so a course counted
    for Required/Flexible Core never also counts for College Option, and a major course listed under both (CSCI 111:
    SW + College Option Science) lands wherever it fills the most slots overall. W overlaps (below)."""
    fits = [[cid for cid in sorted(taken) if fit(cid)] for _, _, fit in SLOTS]
    match = {}                                                   # courseId -> slot index

    def augment(i, seen):
        for cid in fits[i]:
            if cid not in seen:
                seen.add(cid)
                if cid not in match or augment(match[cid], seen):
                    match[cid] = i
                    return True
        return False

    # scarce College Option slots (LIT/LANG/SCI) first so a double-listed major course lands there; catch-all CO4 last
    for i in sorted(range(len(SLOTS)), key=lambda i: (SLOTS[i][0] not in CO - {"CO4"}, SLOTS[i][0] == "CO4")):
        augment(i, set())
    by_slot = {i: cid for cid, i in match.items()}
    out = [{"slot": slot, "label": label, "course": by_slot.get(i)} for i, (slot, label, _) in enumerate(SLOTS)]
    w = [cid for cid in taken if _opt("W")(cid)][:2]
    out.append({"slot": "W", "label": "Writing Intensive (2)", "course": w[0] if w else None})
    out.append({"slot": "W2", "label": "Writing Intensive (2)", "course": w[1] if len(w) > 1 else None})
    return out


# ---- Major requirements ----------------------------------------------------------------------------
def audit_reqs(body_or_reqs):
    reqs = body_or_reqs if isinstance(body_or_reqs, list) else body_or_reqs.get("auditRequirements", [])
    out = []
    for req in reqs or []:
        if not isinstance(req, dict):
            continue
        ids = [cid for cid in req.get("courses", []) if cid in courses]
        out.append({"title": str(req.get("title") or ""), "parent": req.get("parent"), "courses": ids,
                    "page": req.get("page") if isinstance(req.get("page"), int) else None})
    return out


def is_math_req(req):
    return "math" in norm(req["name"])


def is_calculus_option(option):
    return any(courses[i]["subject"] == "MATH" and "calculus" in courses[i]["name"].lower() for i in option if i in courses)


def audit_suppressed(program, audit_requirements):
    reqs = audit_reqs(audit_requirements)
    titles = [norm(r["title"]) for r in reqs if r["courses"]]
    suppressed = set()
    for req in program["requirements"]:
        if not is_math_req(req):
            continue
        all_options = [o for rule in req["rules"] for o in rule["options"]]
        if any(t == "calculusrequirement" for t in titles):
            suppressed |= {i for o in all_options if is_calculus_option(o) for i in o}
        elif any(t == "mathrequirement" for t in titles):
            suppressed |= {i for o in all_options for i in o}
    return suppressed


def math_audit_completions(audit_requirements):
    calc_done = [r for r in audit_requirements if norm(r["title"]) == "calculusrequirement" and r["courses"]]
    math_done = [r for r in audit_requirements if norm(r["title"]) == "mathrequirement" and r["courses"]]
    return calc_done or math_done


def major_progress(program, taken, audit_requirements=None):
    """`taken` may be a set or a {courseId: times_taken} Counter; repeatable courses (CSCI 381 x3) count every time
    toward credit-kind rules, once toward count-kind rules."""
    times = taken if isinstance(taken, dict) else {i: 1 for i in taken}
    audit_requirements = audit_reqs(audit_requirements or [])
    out = []
    for req in program["requirements"]:
        have = need = 0
        completed, missing = [], []   # completed/todo options (OR-groups)
        audit_completed = []
        for rule in req["rules"]:
            # cap: {"ids", "n"} — at most n of these ids count; once met, the rest neither count nor show as missing
            cap, blocked = rule.get("cap"), set()
            if cap:
                got = [i for i in cap["ids"] if i in taken]
                if len(got) >= cap["n"]:
                    blocked = set(cap["ids"]) - set(got[:cap["n"]])
            ok = lambda i: i in taken and i not in blocked
            sat = [o for o in rule["options"] if any(ok(i) for i in o)]
            need += rule["n"]
            have += sum(courses[i]["credits"] * times[i] for o in sat for i in o if ok(i)) if rule["kind"] == "credits" else min(len(sat), rule["n"])
            completed += [[i for i in o if ok(i)] for o in sat]
            if have < need:
                missing += [o for o in rule["options"] if o not in sat and not set(o) <= blocked]
        if is_math_req(req):
            audit_math_done = math_audit_completions(audit_requirements)
            calc_done = [r for r in audit_math_done if norm(r["title"]) == "calculusrequirement"]
            if calc_done and missing:
                covered = [o for o in missing if is_calculus_option(o)]
                if covered:
                    missing = [o for o in missing if o not in covered]
                    have += len(covered)
                    audit_completed += calc_done
                    if not missing:
                        have = max(have, need)
            elif audit_math_done and norm(audit_math_done[0]["title"]) == "mathrequirement":
                missing = []
                have = max(have, need)
                audit_completed += audit_math_done
        completed_keys = {tuple(o) for o in completed}
        for req_done in audit_completed:
            key = tuple(req_done["courses"])
            if key and key not in completed_keys:
                completed.append(req_done["courses"])
                completed_keys.add(key)
        item = {"name": req["name"], "have": have, "need": need, "set": next((r["set"] for r in req["rules"] if r.get("set")), None),
                "unit": "credits" if any(r["kind"] == "credits" for r in req["rules"]) else "courses", "completed": completed, "missing": missing}
        if audit_completed:
            item["auditCompleted"] = audit_completed
        out.append(item)
    return out


def level(cid):
    m = re.match(r"[0-9]+", courses[cid]["code"].split()[1])
    if not m:                                                 # 29 catalog codes have no leading digit (CAS E11, ACCT E305)
        return 0                                              # treat as intro: never flagged unverified, never a W tiebreak
    n = int(m.group())
    return n // 10 if n >= 1000 else n                        # CUNY 4-digit codes: CHEM 1013 is 100-level


def verified(cid):
    """True when we have a prerequisite source for this course, or it is an intro (<200) course. 200+ courses with no
    known prerequisite are shown as 'unverified' so students confirm with an advisor."""
    return cid in source or cid in prereqs or level(cid) < 200


def validate(terms):
    """Re-check an approved plan term by term: every prerequisite group must be met by an EARLIER term
    (same term allowed only for coreq-able courses, precalc by placement). Returns violations."""
    out, before = [], set()
    for i, term in enumerate(terms):
        same = set(term)
        for cid in term:
            if cid not in courses:
                continue
            for group in prereqs.get(cid, []):
                ok = any(p in before for p in group) or placement(group) or (cid in coreqs and any(p in same for p in group))
                if not ok:
                    out.append({"id": cid, "term": i, "missing": group})
        before |= same
    return out


def placement(group):
    """ponytail: a prereq group made only of precalc-track MATH (below 120, or 122) is assumed met by placement exam."""
    def num(cid): return int(re.match(r"\d+", courses[cid]["code"].split()[1]).group())
    return all(courses[p]["subject"] == "MATH" and (num(p) < 120 or num(p) == 122) for p in group)


def prereqs_met(cid, taken):
    return all(any(p in taken for p in group) or placement(group) for group in prereqs.get(cid, []))


def offered_text(cid, term):
    """The catalog's `courseTypicallyOffered` prose. Nearly worthless on its own -- it is the literal string
    "Fall, Spring" for 3,611 of 3,834 courses -- so it is only the last resort in offered() below."""
    o = courses[cid]["offered"]
    return term in o or o in ("", "All Terms", "Offer as needed") if term in ("Summer", "Winter") else term in o or "All" in o or o in ("", "Offer as needed")


def sections_for(cid, term):
    """Real sections for this course in that season, newest scraped term first ([] if none)."""
    for t in SEASON.get(term, ()):
        if secs := sections.get(t, {}).get(cid):
            return secs
    return []


def offered(cid, term):
    """Does this course actually run that term?

    Real sections win, but ONLY to move a course between seasons. Absence from every scraped term falls back
    to the catalog prose, and that fallback is load-bearing -- do not "tighten" it. 2,730 of 3,834 catalog
    courses have no section in any scraped term, and they are not all dead: 14 of the 44 courses in the
    CSCI-BS requirements (CSCI 310, 335, 365, 383, ...) and PHYS 204/227 on the official degree map are among
    them, because electives rotate across years. Treating "no section" as "not offered" would delete a third
    of the major's elective list. What we CAN say confidently is the narrower claim below.
    """
    if sections_for(cid, term):
        return True
    if any(cid in sections.get(t, {}) for t in sections):
        return False            # it runs at QC in another season -- for THAT we trust the section data
    return offered_text(cid, term)


def fits(sec, avail):
    """One section against the student's availability. Times are minutes past midnight, so an overlap is a
    single comparison. A section with no meeting time (asynchronous online, TBA) always fits."""
    for m in [sec] + (sec.get("extra") or []):
        if m.get("start") is None:
            continue
        if m["start"] < avail.get("earliest", 0) or m["end"] > avail.get("latest", 24 * 60):
            return False
        for day, s, e in avail.get("busy") or ():
            if day in m["days"] and m["start"] < e and s < m["end"]:
                return False
    return True


def available(cid, term, avail):
    """Sections of this course the student could actually attend. Returns None when we have no schedule
    data for it at all -- 'unknown', which must not be confused with 'nothing fits'."""
    secs = sections_for(cid, term)
    if not secs:
        return None
    return [s for s in secs if fits(s, avail)] if avail else secs



def schedulable_sections(cid, term, avail=None, open_only=True):
    """Complete section choices the optimizer may actually register.

    Unlike ``available()``, this filters CUNYfirst registration status when planning a
    published term and bundles required lecture/lab/recitation rows into one option.
    """
    secs = available(cid, term, avail)
    if not secs:
        return []
    if open_only:
        secs = [s for s in secs if str(s.get("status", "")).strip().lower() == "open"]
    return bundle_components(secs)

def map_rank(program, cid):
    """Position in the official degree map (earlier = smaller) or 99."""
    for i, sem in enumerate(program["semesters"]):
        if any(cid in slot["courses"] for slot in sem["slots"]):
            return i
    return 99


def preference_subjects(preferences):
    """Canonical preferred/avoided subject sets; malformed or omitted preferences are inert."""
    preferences = preferences if isinstance(preferences, dict) else {}
    clean = lambda name: {str(x).strip().upper() for x in preferences.get(name, []) if str(x).strip()} \
        if isinstance(preferences.get(name, []), list) else set()
    preferred = clean("preferredSubjects")
    return preferred, clean("avoidedSubjects") - preferred



SCHEDULE_WEIGHT_KEYS = (
    "campus_days", "gap_minutes", "early_minutes", "late_minutes", "campus_span_minutes",
)
DAY_CODES = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")


def sanitize_availability(raw):
    """Validate a hard availability object before it reaches the scheduler."""
    if not isinstance(raw, dict):
        return None

    def minute(value, default):
        try:
            return max(0, min(24 * 60, int(round(float(value)))))
        except (TypeError, ValueError):
            return default

    earliest = minute(raw.get("earliest"), 0)
    latest = minute(raw.get("latest"), 24 * 60)
    busy = []
    for item in raw.get("busy") or []:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            continue
        day = str(item[0])
        start, end = minute(item[1], -1), minute(item[2], -1)
        if day in DAY_CODES and 0 <= start < end <= 24 * 60:
            busy.append((day, start, end))
    busy = sorted(set(busy), key=lambda b: (DAY_CODES.index(b[0]), b[1], b[2]))
    return {"busy": busy, "earliest": earliest, "latest": latest}


def merge_availability(manual, generated):
    """Manual controls remain authoritative; Gemini can only add hard constraints."""
    manual = sanitize_availability(manual)
    generated = sanitize_availability(generated)
    if not manual and not generated:
        return None
    if not manual:
        return generated
    if not generated:
        return manual
    return {
        "busy": sorted(set(tuple(x) for x in manual["busy"] + generated["busy"]), key=lambda b: (DAY_CODES.index(b[0]), b[1], b[2])),
        "earliest": max(manual["earliest"], generated["earliest"]),
        "latest": min(manual["latest"], generated["latest"]),
    }


def sanitize_schedule_profile(raw):
    """Bound every Gemini-controlled field before it can affect ranking."""
    raw = raw if isinstance(raw, dict) else {}
    source_name = raw.get("source")
    source_name = source_name if source_name in ("gemini", "heuristic") else "heuristic"
    weights_raw = raw.get("weights") if isinstance(raw.get("weights"), dict) else {}
    weights = {}
    for name in SCHEDULE_WEIGHT_KEYS:
        try:
            value = float(weights_raw.get(name, 0.0))
        except (TypeError, ValueError):
            value = 0.0
        weights[name] = max(0.0, min(1.0, value))

    def optional_minutes(name, low, high):
        value = raw.get(name)
        if value in (None, ""):
            return None
        try:
            return max(low, min(high, int(round(float(value)))))
        except (TypeError, ValueError):
            return None

    return {
        "summary": str(raw.get("summary") or "")[:320],
        "weights": weights,
        "availability": sanitize_availability(raw.get("availability")),
        "commuteMinutes": optional_minutes("commuteMinutes", 0, 360),
        "maxCampusSpanMinutes": optional_minutes("maxCampusSpanMinutes", 60, 14 * 60),
        "source": source_name,
    }


def interpret_schedule_preferences(body):
    """Translate natural language or rejection feedback into a safe ranking profile.

    This intentionally reuses the team's shared Gemini helper, so preference parsing,
    semester generation, and Advisor chat use the same 3.7 -> 3.6 fallback behavior.
    """
    prompt_text = str(body.get("prompt") or "").strip()[:4000]
    feedback = str(body.get("feedback") or "").strip()[:2000]
    if not prompt_text and not feedback:
        raise ValueError("Enter schedule preferences or feedback first.")
    if not os.environ.get("GEMINI_API_KEY"):
        raise RuntimeError("GEMINI_API_KEY is not configured.")
    current = sanitize_schedule_profile(body.get("profile"))
    rejected = body.get("scheduleMetrics")
    rejected = rejected if isinstance(rejected, dict) else {}

    request = f"""You translate a college student's natural-language scheduling preferences into a small numeric profile.
Python, not you, enforces course eligibility, section availability, and time conflicts.

SUPPORTED SOFT COST WEIGHTS (each 0.0 to 1.0; larger = more important to reduce):
- campus_days: fewer days commuting to campus. A long commute should usually raise this.
- gap_minutes: less idle time between classes.
- early_minutes: less class time before 10:00 AM.
- late_minutes: less class time after 5:00 PM.
- campus_span_minutes: shorter first-class-to-last-class campus days.

HARD AVAILABILITY:
- Use day tokens Mo Tu We Th Fr Sa Su.
- busy is a list like [["Tu", 540, 780]] meaning Tuesday 9:00 AM-1:00 PM.
- earliest/latest are minutes after midnight.
- Only use hard availability when the student explicitly says they cannot attend then (work, caregiving, appointment, "never", "can't", etc.). Mere dislikes belong in weights.
- Do not turn commute time itself into a fake busy block. Commute is a ranking signal unless the student gives an actual cannot-attend interval.

CAMPUS SPAN:
- If the student gives a preferred maximum amount of time on campus in one day, set maxCampusSpanMinutes. It is a soft threshold: the optimizer penalizes only minutes beyond it.

FEEDBACK:
- If feedback is supplied, revise the CURRENT PROFILE to address why the shown schedule was rejected.
- Use REJECTED SCHEDULE METRICS as evidence when helpful. "Too much waiting" should increase gap_minutes; "too many trips" should increase campus_days.
- Return the entire replacement profile, not a patch.

STUDENT PREFERENCE PROMPT:
{prompt_text or "(none)"}

CURRENT PROFILE:
{json.dumps(current, sort_keys=True)}

REJECTED SCHEDULE METRICS:
{json.dumps(rejected, sort_keys=True)}

STUDENT FEEDBACK:
{feedback or "(none)"}

Return ONLY this JSON object:
{{
  "summary": "one short plain-English explanation of what you prioritized",
  "weights": {{"campus_days": 0.0, "gap_minutes": 0.0, "early_minutes": 0.0, "late_minutes": 0.0, "campus_span_minutes": 0.0}},
  "availability": {{"busy": [], "earliest": 0, "latest": 1440}},
  "commuteMinutes": null,
  "maxCampusSpanMinutes": null
}}
"""
    try:
        parsed = json.loads(gemini([{"parts": [{"text": request}]}], json_out=True, temperature=0.2))
    except Exception as exc:
        raise RuntimeError(f"Gemini preference interpretation failed: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Gemini returned an invalid preference profile.")
    parsed["source"] = "gemini"
    profile = sanitize_schedule_profile(parsed)
    if not profile["summary"]:
        profile["summary"] = "Gemini translated your schedule preferences into optimizer weights."
    return profile

def candidates(program, taken, term, avail=None, preferences=None, audit_requirements=None):
    times = Counter(taken)            # multiplicity matters for credit-kind rules only
    taken = set(taken)
    preferred, avoided = preference_subjects(preferences)
    audit_requirements = audit_reqs(audit_requirements or [])
    major = major_progress(program, times, audit_requirements)
    slots = pathways(taken)
    open_slots = [(s["slot"], s["label"], fit) for s, (_, _, fit) in zip(slots, SLOTS) if not s["course"]]
    need_w = sum(1 for s in slots if s["slot"].startswith("W") and not s["course"])
    major_ids = {i for req in program["requirements"] for r in req["rules"] for o in r["options"] for i in o}
    major_subjects = {courses[i]["subject"] for i in major_ids if i in courses}
    eligible = {cid for cid in courses if cid not in taken and prereqs_met(cid, taken)}
    eligible |= {cid for cid in coreqs if cid not in taken and prereqs_met(cid, taken | eligible)}   # coreq: pair with a same-term course
    suppressed = audit_suppressed(program, audit_requirements)
    out = []
    for cid, c in courses.items():
        if cid in suppressed or cid not in eligible or not offered(cid, term) or c["credits"] == 0:
            continue
        secs = available(cid, term, avail)
        if secs == []:                       # has real sections, none the student can attend -- drop it
            continue                         # (None means "no schedule data", which is not a reason to drop)
        reason, score, key = None, None, cid
        for req in major:
            hit = next((o for o in req["missing"] if cid in o), None)
            if hit:
                label = req["name"].replace("Major Requirements - ", "Major ")
                if req.get("set"):                                # catalog course set, e.g. "Computer Science Electives"
                    reason, score, key = f"{label}: {req['set']} ({req['have']}/{req['need']} {req['unit']})", (2, c["code"]), cid
                elif len(hit) == 1 and req["unit"] == "courses":
                    reason, score, key = f"{label}: required", (0, map_rank(program, cid)), cid
                else:
                    reason, score, key = f"{label}: " + " or ".join(courses[i]["code"] for i in hit), (2, map_rank(program, cid)), "|".join(hit)
                break
        if not reason:
            slot = next((s for s in open_slots if s[2](cid)), None)
            if slot:
                reason, score, key = f"Pathways: {slot[1]}", (1, [s[0] for s in SLOTS].index(slot[0])), slot[0]
            elif need_w and _opt("W")(cid):
                reason, score, key = "Writing Intensive requirement", (3, level(cid)), "W"   # lowest-level W first: ENGL 165W, not ACCT 361W via ACCT 261
            elif cid in gened:
                reason, score = "Free elective (Pathways-listed)", (5, c["code"])
        if not reason:
            continue
        if (rank := map_rank(program, cid)) < 99 and reason.startswith("Major"):
            # Only major entries on the official map are mandatory priorities. Sample Gen Ed entries remain choices.
            score = (0, rank)
        flexible = reason.startswith(("Pathways", "Writing Intensive", "Free elective"))
        if flexible:
            pref_rank = 0 if c["subject"] in preferred else 2 if c["subject"] in avoided else 1
            # Keep the existing requirement/level ordering, then use preferences as a soft tie-breaker.
            score = (score[0], pref_rank, *score[1:]) if reason.startswith("Free elective") else (*score, pref_rank)
            if c["subject"] in preferred:
                reason += f"; matches your {c['subject']} preference"
        out.append({"id": cid, "reason": reason, "score": score, "key": key,
                    "verified": verified(cid), "source": source.get(cid, "policy" if cid in prereqs else None),
                    "sections": secs[:6] if secs else None})   # null = no schedule data, shown as such
    # tie-break: prefer courses that are prerequisites of something on the official degree map (e.g. PHYS 103 over ASTR 2)
    map_prereqs = {p for sem in program["semesters"] for slot in sem["slots"] for cid in slot["courses"] for g in prereqs.get(cid, []) for p in g}
    out.sort(key=lambda x: (x["score"], x["id"] not in map_prereqs, courses[x["id"]]["code"]))
    buckets, kept = {}, []                                    # keep variety: <=8 per Pathways slot, <=25 per other kind
    for x in out:
        flexible = x["reason"].startswith(("Pathways", "Writing Intensive", "Free elective"))
        pref_rank = x["score"][-1] if flexible and not x["reason"].startswith("Free elective") else x["score"][1] if flexible else None
        b = (("P", x["key"], pref_rank) if x["score"][0] == 1 else (x["score"][0], pref_rank) if flexible else x["score"][0])
        if buckets.get(b, 0) < (8 if x["score"][0] == 1 else 25):
            kept.append(x); buckets[b] = buckets.get(b, 0) + 1
    out = kept
    for x in out[:80]:
        x["unlocks"] = sorted((o for o in courses if o not in taken and not prereqs_met(o, taken) and prereqs_met(o, taken | {x["id"]})),
                              key=lambda o: (o not in major_ids, courses[o]["code"]))
    return out[:80], {"major": major, "pathways": slots, "credits": sum(courses[i]["credits"] for i in taken if i in courses)}


def pick(cands, taken, locked=(), order=()):
    """Fill a term. `locked` (pins/queue) go in unconditionally, then `order` (Gemini's picks) and finally the rule-based
    phases (required core <=3 -> one catalog elective -> Pathways/W -> more electives -> free electives), every one
    through the same guards: <=MAX_CREDITS, <=5 per subject, coreq partner present, one course per Pathways slot.
    Stops once the term has >=MIN_COURSES and ~TARGET_CREDITS."""
    picked, credits, per_subject, used = [], 0, {}, {}
    ids = lambda: taken | {p["id"] for p in picked}
    full = lambda: credits >= TARGET_CREDITS and len(picked) >= MIN_COURSES

    def add(c, force=False):
        nonlocal credits
        if any(p["id"] == c["id"] for p in picked):
            return False
        subj, cr = courses[c["id"]]["subject"], courses[c["id"]]["credits"]
        if not force:
            if used.get(c["key"], 0) >= (2 if c["key"] == "W" else 1) or per_subject.get(subj, 0) >= 5 or credits + cr > MAX_CREDITS:
                return False
            if not prereqs_met(c["id"], ids()):                   # a coreq course needs its partner picked too
                return False
            if c["reason"].startswith("Pathways") and any(x["slot"] == c["key"] and x["course"] for x in pathways(ids())):
                return False                                      # a course already picked this term fills that slot
        picked.append(c); credits += cr; per_subject[subj] = per_subject.get(subj, 0) + 1; used[c["key"]] = used.get(c["key"], 0) + 1
        return True

    for c in locked:
        add(c, force=True)
    for c in order:
        if not full():
            add(c)
    core = [c for c in cands if c["score"][0] == 0]
    electives = sorted((c for c in cands if c["score"][0] == 2 and "Major " in c["reason"] and c["key"] == c["id"]),
                       key=lambda c: (-courses[c["id"]]["credits"], courses[c["id"]]["code"]))
    free = [c for c in cands if c["reason"].startswith("Free")]
    rest = [c for c in cands if c not in core and c not in electives and c not in free]
    for c in core[:8]:
        if full() or sum(1 for p in picked if p["score"][0] == 0) >= 3:
            break
        add(c)
    if not full() and not any(p in electives for p in picked):
        for c in electives:
            if add(c):
                break
    for c in rest + electives + free:
        if full() or len(picked) >= 7:
            break
        add(c)
    return picked


def when(c):
    """'TuTh 1:40PM-2:30PM' for the first fitting section, or '' when we have no schedule for it."""
    s = (c.get("sections") or [None])[0]
    if not s or s.get("start") is None:
        return ""
    fmt = lambda m: f"{(m // 60 - 1) % 12 + 1}:{m % 60:02d}{'AM' if m < 720 else 'PM'}"
    return f'{s["days"]} {fmt(s["start"])}-{fmt(s["end"])}'


def gemini_order(program, term, cands, locked, progress, avail=None, preferences=None):
    """Gemini's ordered picks (candidate dicts with its one-line reason), or None (no key / failure)."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    major_ids = {i for req in program["requirements"] for r in req["rules"] for o in r["options"] for i in o}
    lines = [f'{c["id"]} | {courses[c["id"]]["code"]} {courses[c["id"]]["name"]} | {courses[c["id"]]["credits"]} cr | {c["reason"]} | {when(c)} | unlocks: '
             + ", ".join(courses[u]["code"] for u in c["unlocks"] if u in major_ids)   # only major unlocks: a gen-ed chain (ACCT 261 -> 361W) is never a reason
             for c in cands[:40] if c not in locked]
    lcr = sum(courses[c["id"]]["credits"] for c in locked)
    fixed = ("The student has already placed these in the term (keep them, do not repeat them): "
             + ", ".join(f'{courses[c["id"]]["code"]} ({courses[c["id"]]["credits"]} cr)' for c in locked) + f" = {lcr} credits.\n") if locked else ""
    # every listed course already fits the student's availability -- this only asks for a compact day/time spread
    sched = ("Every course listed below already fits the student's stated availability. Among equally good choices prefer\n"
             "a term whose meeting times cluster on fewer days and do not leave long midday gaps.\n") if avail else ""
    preferred, avoided = preference_subjects(preferences)
    preference_rule = (f"For flexible choices only (Pathways, Writing Intensive, and free electives), prefer subjects "
                       f"{', '.join(sorted(preferred)) or '(none)'}, then neutral subjects, and put avoided subjects "
                       f"{', '.join(sorted(avoided)) or '(none)'} last. This is a soft preference: use an avoided subject "
                       "when no valid alternative fills the same requirement. Never apply these preferences to Major requirements. "
                       "Mention a preferred-subject match briefly in its reason.\n") if preferred or avoided else ""
    prompt = f"""You are an academic advisor at Queens College planning a {term} semester for a {program['name']} ({program['degree']}) student
who has completed {progress['credits']} credits. {fixed}Pick courses from the ELIGIBLE list only so the whole term has at least {MIN_COURSES} courses
and {TARGET_CREDITS}-{MAX_CREDITS} credits (so pick about {max(TARGET_CREDITS - lcr, 0)}-{MAX_CREDITS - lcr} more credits, {max(MIN_COURSES - len(locked), 0)} or more courses).
Balance major courses with Pathways (general education) courses, prefer courses that unlock the most major courses, and
avoid more than 2 courses of the same subject. Never take a course only to reach a later general-education or Writing
Intensive course: fill each Pathways or Writing Intensive slot with the lowest-level course that fits it directly. List them most important first.
{preference_rule}{sched}Return ONLY JSON: {{"courses": [{{"id": "<id>", "reason": "<one short sentence for the student>"}}]}}

ELIGIBLE (id | course | credits | why it is needed | meeting time | what it unlocks):
""" + "\n".join(lines)
    try:
        chosen = json.loads(gemini([{"parts": [{"text": prompt}]}], json_out=True))["courses"]
    except Exception as e:                                      # ponytail: any Gemini failure -> heuristic
        print("gemini failed:", e)
        return None
    valid = {c["id"]: c for c in cands}
    out = []
    for x in chosen:
        if not isinstance(x, dict) or x.get("id") not in valid:
            continue
        c = valid[x["id"]]
        reason = x.get("reason") or c["reason"]
        subject = courses[c["id"]]["subject"]
        if "matches your " in c["reason"] and f"{subject} preference" not in reason:
            reason += f"; matches your {subject} preference"
        out.append(dict(c, reason=reason))
    return list({p["id"]: p for p in out}.values()) or None       # Gemini sometimes repeats a course


def unlocks(cid, base, program):
    """Courses whose prereqs are met with `base` (this plan) but NOT without `cid`: the course is *necessary* for them.
    Evaluated against the whole proposed term, so a course never 'unlocks' something its term-mate already covers."""
    with_c, without_c = base | {cid}, base - {cid}
    major_ids = {i for req in program["requirements"] for r in req["rules"] for o in r["options"] for i in o}
    return sorted((o for o in courses if o not in with_c and prereqs.get(o) and prereqs_met(o, with_c) and not prereqs_met(o, without_c)),
                  key=lambda o: (o not in major_ids, courses[o]["code"]))




def optimized_schedules(cands, taken, terms, term, avail, locked=(), order=(), schedule_profile=None, limit=20):
    """Search the full eligible pool for distinct conflict-free full-time terms."""
    profile = sanitize_schedule_profile(schedule_profile)
    ranked, seen = [], set()
    for cand in [*locked, *order, *cands]:
        if cand["id"] not in seen:
            seen.add(cand["id"])
            ranked.append(cand)

    open_only = not bool(terms)
    section_map, meta = {}, {}
    for cand in ranked:
        cid = cand["id"]
        choices = schedulable_sections(cid, term, avail, open_only=open_only)
        if not choices:
            continue
        section_map[cid] = choices
        meta[cid] = {"credits": courses[cid]["credits"], "subject": courses[cid]["subject"], "key": cand["key"]}

    required = [c["id"] for c in locked]
    missing_required = [cid for cid in required if cid not in section_map]
    info = {
        "applied": False, "count": 0, "limit": limit,
        "poolCourses": len(section_map), "poolSections": sum(len(v) for v in section_map.values()),
        "openOnly": open_only, "schedules": [], "profile": profile,
        "effectiveAvailability": avail,
        "ranking": "weighted-explored-pool" if any(profile["weights"].values()) else "neutral",
    }
    if missing_required:
        info["reason"] = "A pinned course has no usable section for this term."
        return [], info

    current_term_index = len(terms)
    def academically_valid(ids):
        problems = validate([*terms, list(ids)])
        return not any(v["term"] == current_term_index for v in problems)

    schedules = explore_course_schedules(
        section_map, meta, required=required, min_courses=MIN_COURSES,
        target_credits=TARGET_CREDITS, max_credits=MAX_CREDITS, max_courses=7,
        limit=limit, valid_course_set=academically_valid, open_only=open_only,
        weights=profile["weights"], max_campus_span_minutes=profile["maxCampusSpanMinutes"], ranking_pool=80,
    )
    info["applied"] = bool(schedules)
    info["count"] = len(schedules)
    info["schedules"] = schedules
    if not schedules:
        info["reason"] = "No full-time conflict-free schedule was found in the usable candidate pool."
    return schedules, info


_cache = {}   # ponytail: in-memory, per process; inputs include availability/preferences. Restart clears it.


def suggest(body):
    program = programs[body["program"]]
    term = body.get("term", "Fall")
    terms = body.get("terms") or ([body["taken"]] if body.get("taken") else [])
    times = Counter(i for t in terms for i in t)
    taken = set(times)
    manual_avail = body.get("avail") or None
    schedule_profile = sanitize_schedule_profile(body.get("scheduleProfile"))
    avail = merge_availability(manual_avail, schedule_profile.get("availability"))
    preferences = body.get("preferences") or None
    preferred, avoided = preference_subjects(preferences)
    audit_requirements = audit_reqs(body)
    cands, progress = candidates(program, times, term, avail, preferences, audit_requirements)
    valid = {c["id"]: c for c in cands}
    locked = [valid[i] for i in dict.fromkeys(body.get("pins", []) + body.get("queue", [])) if i in valid]
    ai = bool(body.get("ai", True))
    key = (body["program"], tuple(tuple(t) for t in terms), term, tuple(c["id"] for c in locked), ai,
           json.dumps(manual_avail, sort_keys=True), json.dumps(schedule_profile, sort_keys=True),
           tuple(sorted(preferred)), tuple(sorted(avoided)), json.dumps(audit_requirements, sort_keys=True))
    if not body.get("fresh") and key in _cache:
        return _cache[key]
    lcr = sum(courses[c["id"]]["credits"] for c in locked)
    order = None if not ai or (lcr >= TARGET_CREDITS and len(locked) >= MIN_COURSES) else gemini_order(program, term, cands, locked, progress, avail, preferences)

    # The teammate's ai flag controls Gemini *course ordering*. The deterministic
    # section optimizer runs independently and can use a previously interpreted
    # scheduleProfile without making another Gemini call.
    if terms:
        schedule_options = []
        optimizer_info = {
            "applied": False, "count": 0, "limit": 20,
            "poolCourses": 0, "poolSections": 0, "openOnly": False, "schedules": [],
            "reason": "Future pattern term keeps the academic course planner; no live section optimization applied.",
            "profile": schedule_profile, "effectiveAvailability": avail, "ranking": "academic-pattern",
        }
    else:
        schedule_options, optimizer_info = optimized_schedules(cands, taken, terms, term, avail, locked, order or (), schedule_profile, limit=20)

    if schedule_options:
        picked = []
        selected_by_id = {item["course_id"]: item for item in schedule_options[0]["sections"]}
        proposal_order = list(dict.fromkeys(c["id"] for c in [*locked, *(order or ()), *cands]))
        for cid in proposal_order:
            item = selected_by_id.get(cid)
            if not item:
                continue
            c = valid[cid]
            selected = item["section"]
            component_secs = {str(s.get("sec", "")) for s in (selected.get("components") or [selected])}
            alternatives = [s for s in (c.get("sections") or []) if str(s.get("sec", "")) not in component_secs]
            c["sections"] = [selected, *alternatives[:5]]
            picked.append(c)
    else:
        picked = pick(cands, taken, locked, order or ())

    base = taken | {c["id"] for c in picked}
    for c in cands:
        c["unlocks"] = unlocks(c["id"], base, program)
    strip = lambda c: {k: v for k, v in c.items() if k not in ("score", "key")}
    out = {"suggested": [strip(c) for c in picked], "candidates": [strip(c) for c in cands], "progress": progress,
           "source": "gemini" if order else "heuristic", "optimizer": optimizer_info, "violations": validate(terms),
           "schedule": {"basis": "published" if not terms else "pattern", "scraped": _meta.get("scraped")} if sections else None}
    _cache[key] = out
    return out

def gemini(contents, system=None, json_out=False, temperature=0.4, model=MODEL):
    """One generateContent call; returns the text or raises. `contents` is the Gemini turn list."""
    body = {"contents": contents, "generationConfig": {"temperature": temperature, **({"responseMimeType": "application/json"} if json_out else {})}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={os.environ['GEMINI_API_KEY']}",
                                 data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["candidates"][0]["content"]["parts"][0]["text"]
    except urllib.error.HTTPError as e:
        if e.code == 503 and model != FALLBACK:
            print(f"{model} overloaded, falling back to {FALLBACK}", flush=True)
            return gemini(contents, system, json_out, temperature, FALLBACK)
        raise


def chat_context(program, terms, term):
    """What the advisor bot knows about this student: plan so far, progress, and what is eligible next term."""
    times = Counter(i for t in terms for i in t)
    cands, progress = candidates(program, times, term)
    plan = "\n".join(f"Term {n}: " + (", ".join(f'{courses[i]["code"]}' for i in t if i in courses) or "(empty)") for n, t in enumerate(terms, 1)) or "(nothing approved yet)"
    major = "\n".join(f'- {m["name"]}: {m["have"]}/{m["need"]} {m["unit"]}' + (f'; still missing e.g. ' + ", ".join(courses[o[0]]["code"] for o in m["missing"][:4] if o and o[0] in courses) if m["missing"] else "")
                      for m in progress["major"])
    pathways = ", ".join(f'{p["label"]}: {courses[p["course"]]["code"] if p["course"] else "open"}' for p in progress["pathways"])
    elig = "\n".join(f'- {courses[c["id"]]["code"]} {courses[c["id"]]["name"]} ({courses[c["id"]]["credits"]} cr) - {c["reason"]}' for c in cands[:40])
    return f"""You are a friendly, concise academic advisor chatbot inside the Queens College Degree Planner app.
The student is in the {program['name']} ({program['degree']}) program with {progress['credits']} approved credits.
Answer only from the facts below plus general advising sense; if asked about a course not listed, say you can't verify it and suggest
they add it in the planner (it will flag missing prerequisites). Keep replies short (a few sentences or a short list). Never invent
course codes, credits, or prerequisites.

APPROVED PLAN:
{plan}

MAJOR PROGRESS:
{major}

PATHWAYS: {pathways}

ELIGIBLE NEXT {term.upper()} (code name (credits) - why it is needed):
{elig}"""


FAST_TRACK_RE = re.compile(r"fast(est)?[ -]?track|quick(est)?|shortest|soonest|fewest (terms|semesters)|graduate (early|fast|sooner|asap)|finish (early|fast|sooner|asap)|whole plan|full plan|entire plan|rest of my (plan|degree)", re.I)
MAX_TRACK_TERMS = 12


def fast_track(program, terms, term):
    """Rule-based path from the approved `terms` to a finished major: one filled term after another (Fall/Spring only)
    until every major rule is met, nothing is eligible, or MAX_TRACK_TERMS. Returns [{"term", "courses": [ids]}]."""
    taken, out = Counter(i for t in terms for i in t), []
    for _ in range(MAX_TRACK_TERMS):
        cands, progress = candidates(program, taken, term)
        if all(m["have"] >= m["need"] for m in progress["major"]):
            break
        picked = pick(cands, set(taken))
        if not picked:
            break
        out.append({"term": term, "courses": [c["id"] for c in picked]})
        taken.update(c["id"] for c in picked)
        term = "Spring" if term == "Fall" else "Fall"
    return out


def track_text(track, done):
    lines = [f'{n}. {t["term"]}: ' + ", ".join(f'{courses[i]["code"]} ({courses[i]["credits"]} cr)' for i in t["courses"]) for n, t in enumerate(track, 1)]
    return "\n".join(lines) + ("\n(major requirements complete after this)" if done else "\n(stops here: nothing further is eligible or the cap was reached; Pathways/free electives may remain)")


def chat(body):
    """POST /api/chat {"program", "terms", "term", "messages": [{"role": "user"|"model", "text"}]} -> {"reply", "source"}"""
    program = programs[body["program"]]
    msgs = [m for m in body.get("messages", []) if isinstance(m, dict) and m.get("role") in ("user", "model") and str(m.get("text", "")).strip()][-20:]
    if not msgs or msgs[-1]["role"] != "user":
        return {"error": "last message must be from the user"}
    if not os.environ.get("GEMINI_API_KEY"):
        return {"reply": "The advisor chat needs GEMINI_API_KEY set on the server.", "source": "none"}
    terms, term = body.get("terms") or [], body.get("term", "Fall")
    system = chat_context(program, terms, term)
    track = None
    if FAST_TRACK_RE.search(msgs[-1]["text"]):              # ponytail: keyword intent; a Gemini classifier if it misfires
        track = fast_track(program, terms, term)
        done = not track or all(m["have"] >= m["need"] for m in candidates(program, Counter(i for t in terms + [x["courses"] for x in track] for i in t), term)[1]["major"])
        system += (f"\n\nFASTEST TRACK (computed by the planner from the {len(terms)} approved term(s); "
                   "the student asked for this - present EVERY term below verbatim as a numbered list, then one or two sentences of advice):\n"
                   + (track_text(track, done) if track else "(nothing to add: the major requirements are already met by the approved terms)"))
    contents = [{"role": m["role"], "parts": [{"text": str(m["text"])[:4000]}]} for m in msgs]
    try:
        return {"reply": gemini(contents, system=system, temperature=0.6), "source": "gemini", "track": track}
    except Exception as e:                                      # ponytail: surface the failure, no retry
        detail = f"HTTP {e.code}: {e.read()[:200].decode(errors='replace')}" if isinstance(e, urllib.error.HTTPError) else str(e)
        print("gemini chat failed:", detail, flush=True)
        return {"reply": f"Sorry, the advisor is unavailable right now ({detail}).", "source": "error"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-headers", "content-type")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send({})

    def do_GET(self):
        if self.path == "/api/programs":
            return self._send([{"id": p["id"], "name": p["name"], "degree": p["degree"]} for p in programs.values()])
        if self.path == "/api/gened":                            # Pathways tags per course, for the add-course filter
            labels = {"EC": "English Composition", "MQR": "Math & Quantitative Reasoning", "LPS": "Life & Physical Sciences",
                      "WCGI": "World Cultures & Global Issues", "USED": "US Experience in its Diversity", "CE": "Creative Expression",
                      "IS": "Individual & Society", "SW": "Scientific World", "LIT": "College Option: Literature",
                      "LANG": "College Option: Language", "SCI": "College Option: Science", "SYN": "College Option: Synthesis", "W": "Writing Intensive"}
            return self._send({"labels": labels, "courses": {cid: ([g["area"]] if g["area"] else []) + sorted(g["opts"]) for cid, g in gened.items()}})
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length", 0)) or 0)
        if self.path == "/api/audit":
            try:
                pdf = multipart_file(self.headers, raw)
                if not pdf:
                    return self._send({"error": "Upload a DegreeWorks PDF."}, 400)
                return self._send(parse_audit_pdf(pdf))
            except Exception as e:
                return self._send({"error": str(e)}, 400)
        body = json.loads(raw or b"{}")
        if self.path == "/api/schedule-preferences":
            try:
                return self._send(interpret_schedule_preferences(body))
            except ValueError as e:
                return self._send({"error": str(e)}, 400)
            except RuntimeError as e:
                return self._send({"error": str(e)}, 503)
        if self.path == "/api/suggest" and body.get("program") in programs:
            return self._send(suggest(body))
        if self.path == "/api/chat" and body.get("program") in programs:
            return self._send(chat(body))
        self._send({"error": "bad request"}, 400)


if __name__ == "__main__":
    print(f"{len(courses)} courses, {len(programs)} programs, {len(prereqs)} with prereqs, {len(gened)} gen-ed; "
          f"gemini: {'on' if os.environ.get('GEMINI_API_KEY') else 'off (heuristic)'}")
    port = int(os.environ.get("PORT", 8000))   # ponytail: PORT= to run a second copy beside a live one
    print(f"listening on :{port}")
    ThreadingHTTPServer(("", port), Handler).serve_forever()
