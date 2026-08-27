"""Conflict-free class-section optimizer.

The academic planner decides *which courses* are worth taking. This module solves the
separate combinatorial problem: choose one real section for each requested course so
all meetings can coexist.

Hard constraints are always enforced:
  * one section per requested course
  * no overlapping meetings
  * optionally require sections whose CUNYfirst status is Open

Preferences are optional. With ``weights=None`` every feasible schedule is considered
equally good and results are returned in deterministic search order. This is the safe
MVP/default: we do not invent student preferences. A later Gemini/user-preference
layer can supply normalized weights in [0, 1] without changing the solver.

The search uses backtracking with the most-constrained-course-first (MRV) heuristic,
so it prunes a branch as soon as a newly chosen section conflicts with the partial
schedule instead of constructing the full Cartesian product first.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
DEFAULT_EARLY = 10 * 60       # 10:00 AM
DEFAULT_LATE = 17 * 60        # 5:00 PM
SUPPORTED_WEIGHTS = {"campus_days", "gap_minutes", "early_minutes", "late_minutes", "campus_span_minutes"}


@dataclass(frozen=True)
class Choice:
    """A chosen section paired with its course id."""

    course_id: str
    section: Mapping


def _day_set(raw: str) -> set[str]:
    """Convert the scraper's compact day string (e.g. ``MoWe`` or ``TuTh``) to a set."""
    raw = raw or ""
    return {d for d in DAYS if d in raw}


def meetings(section: Mapping) -> List[Mapping]:
    """Return the primary meeting plus any extra meetings (lab/recitation/etc.)."""
    return [section] + list(section.get("extra") or [])


def meetings_conflict(a: Mapping, b: Mapping) -> bool:
    """True when two individual meetings overlap on at least one common day.

    TBA/asynchronous meetings have no numeric time and are treated as non-conflicting;
    there is no honest time conflict to enforce until a time exists.
    """
    if a.get("start") is None or a.get("end") is None or b.get("start") is None or b.get("end") is None:
        return False
    if not (_day_set(a.get("days", "")) & _day_set(b.get("days", ""))):
        return False
    return a["start"] < b["end"] and b["start"] < a["end"]


def sections_conflict(a: Mapping, b: Mapping) -> bool:
    """True when any primary/extra meeting from section A conflicts with section B."""
    return any(meetings_conflict(ma, mb) for ma in meetings(a) for mb in meetings(b))


def compatible(section: Mapping, chosen: Sequence[Choice]) -> bool:
    """Whether ``section`` can be added to the partial schedule."""
    return all(not sections_conflict(section, item.section) for item in chosen)


def _is_open(section: Mapping) -> bool:
    """CUNYfirst uses Open / Closed / Wait List. Blank status is treated as unknown, not open."""
    return str(section.get("status", "")).strip().lower() == "open"


def _filtered_options(
    course_sections: Mapping[str, Sequence[Mapping]], open_only: bool
) -> Dict[str, List[Mapping]]:
    out: Dict[str, List[Mapping]] = {}
    for cid, options in course_sections.items():
        values = [s for s in options if (not open_only or _is_open(s))]
        # Deterministic order makes neutral-mode pagination stable across requests.
        values.sort(key=lambda s: (
            s.get("start") is None,
            s.get("start") if s.get("start") is not None else 24 * 60,
            s.get("days", ""),
            str(s.get("sec", "")),
        ))
        out[cid] = values
    return out


def feasible_schedules(
    course_sections: Mapping[str, Sequence[Mapping]], *, open_only: bool = True
) -> Iterator[List[Choice]]:
    """Yield conflict-free schedules lazily using backtracking + MRV.

    The function does *not* generate the Cartesian product up front. At each recursive
    step it chooses the remaining course with the fewest currently compatible sections,
    so impossible branches die as early as possible.
    """
    options = _filtered_options(course_sections, open_only)
    if not options or any(not values for values in options.values()):
        return

    remaining = tuple(sorted(options))

    def search(todo: Tuple[str, ...], chosen: List[Choice]) -> Iterator[List[Choice]]:
        if not todo:
            yield list(chosen)
            return

        compatible_by_course = {
            cid: [section for section in options[cid] if compatible(section, chosen)]
            for cid in todo
        }
        if any(not values for values in compatible_by_course.values()):
            return

        cid = min(todo, key=lambda c: (len(compatible_by_course[c]), c))
        rest = tuple(c for c in todo if c != cid)
        for section in compatible_by_course[cid]:
            chosen.append(Choice(cid, section))
            yield from search(rest, chosen)
            chosen.pop()

    yield from search(remaining, [])


def schedule_metrics(
    schedule: Sequence[Choice],
    max_campus_span_minutes: Optional[float] = None,
) -> Dict[str, float]:
    """Compute preference attributes. Lower is better for every metric.

    ``campus_span_minutes`` is the total first-class-to-last-class span across
    campus days. ``max_daily_span_minutes`` is exposed for the UI. When the
    student gives a soft maximum campus day, ``excess_span_minutes`` measures
    only the amount beyond that threshold.
    """
    per_day: Dict[str, List[Tuple[int, int]]] = {d: [] for d in DAYS}
    early = late = 0

    for choice in schedule:
        for meeting in meetings(choice.section):
            start, end = meeting.get("start"), meeting.get("end")
            if start is None or end is None:
                continue
            days = _day_set(meeting.get("days", ""))
            for day in days:
                per_day[day].append((start, end))
                early += max(0, DEFAULT_EARLY - start)
                late += max(0, end - DEFAULT_LATE)

    campus_days = sum(bool(v) for v in per_day.values())
    gaps = 0
    campus_span = 0
    max_daily_span = 0
    excess_span = 0
    for values in per_day.values():
        values.sort()
        for (_, prev_end), (next_start, _) in zip(values, values[1:]):
            gaps += max(0, next_start - prev_end)
        if values:
            span = max(end for _, end in values) - min(start for start, _ in values)
            campus_span += span
            max_daily_span = max(max_daily_span, span)
            if max_campus_span_minutes is not None:
                excess_span += max(0.0, span - float(max_campus_span_minutes))

    return {
        "campus_days": float(campus_days),
        "gap_minutes": float(gaps),
        "early_minutes": float(early),
        "late_minutes": float(late),
        "campus_span_minutes": float(campus_span),
        "max_daily_span_minutes": float(max_daily_span),
        "excess_span_minutes": float(excess_span),
    }



def normalize_weights(weights: Optional[Mapping[str, float]]) -> Optional[Dict[str, float]]:
    """Validate preference weights and normalize them to sum to 1.

    Input values must already be bounded to [0, 1]. ``None`` or all-zero weights means
    neutral mode: all feasible schedules rank equally and deterministic search order wins.
    """
    if not weights:
        return None
    unknown = set(weights) - SUPPORTED_WEIGHTS
    if unknown:
        raise ValueError(f"unknown optimizer weight(s): {', '.join(sorted(unknown))}")
    clean = {name: float(weights.get(name, 0.0)) for name in SUPPORTED_WEIGHTS}
    if any(value < 0 or value > 1 for value in clean.values()):
        raise ValueError("optimizer weights must be between 0 and 1")
    total = sum(clean.values())
    if total == 0:
        return None
    return {name: value / total for name, value in clean.items()}


def _cost(
    metrics: Mapping[str, float],
    weights: Mapping[str, float],
    max_campus_span_minutes: Optional[float] = None,
) -> float:
    """Weighted human-scale cost used only for ordering feasible schedules."""
    span_cost = metrics["excess_span_minutes"] if max_campus_span_minutes is not None else metrics["campus_span_minutes"]
    return (
        weights["campus_days"] * metrics["campus_days"]
        + weights["gap_minutes"] * metrics["gap_minutes"] / 60.0
        + weights["early_minutes"] * metrics["early_minutes"] / 60.0
        + weights["late_minutes"] * metrics["late_minutes"] / 60.0
        + weights["campus_span_minutes"] * span_cost / 60.0
    )



def optimize(
    course_sections: Mapping[str, Sequence[Mapping]],
    *,
    limit: int = 10,
    offset: int = 0,
    weights: Optional[Mapping[str, float]] = None,
    open_only: bool = True,
    ranking_pool: int = 1000,
    max_campus_span_minutes: Optional[float] = None,
) -> Dict:
    """Return a page of feasible schedules.

    Weighted mode ranks an explored pool; a soft campus-day maximum changes
    only the span penalty and never makes an otherwise feasible schedule invalid.
    """
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    if ranking_pool < limit + offset:
        ranking_pool = limit + offset
    if max_campus_span_minutes is not None and max_campus_span_minutes <= 0:
        raise ValueError("max campus span must be positive")

    normalized = normalize_weights(weights)
    stream = feasible_schedules(course_sections, open_only=open_only)

    if normalized is None:
        results = []
        seen = 0
        has_more = False
        for schedule in stream:
            if seen < offset:
                seen += 1
                continue
            if len(results) < limit:
                results.append(_serialize(schedule, None, None))
                seen += 1
                continue
            has_more = True
            break
        return {
            "schedules": results,
            "offset": offset,
            "next_offset": offset + len(results) if has_more else None,
            "weights": None,
            "ranking": "neutral",
        }

    explored = []
    for i, schedule in enumerate(stream):
        if i >= ranking_pool:
            break
        metrics = schedule_metrics(schedule, max_campus_span_minutes)
        explored.append((_cost(metrics, normalized, max_campus_span_minutes), schedule, metrics))
    explored.sort(key=lambda item: (item[0], _stable_key(item[1])))
    page = explored[offset:offset + limit]
    return {
        "schedules": [_serialize(schedule, cost, metrics) for cost, schedule, metrics in page],
        "offset": offset,
        "next_offset": offset + len(page) if offset + len(page) < len(explored) else None,
        "weights": normalized,
        "ranking": "weighted-explored-pool",
        "explored": len(explored),
    }



def _stable_key(schedule: Sequence[Choice]) -> Tuple:
    return tuple(sorted((c.course_id, str(c.section.get("sec", ""))) for c in schedule))


def _serialize(
    schedule: Sequence[Choice],
    cost: Optional[float],
    metrics: Optional[Mapping[str, float]],
) -> Dict:
    ordered = sorted(schedule, key=lambda c: c.course_id)
    out = {
        "sections": [{"course_id": c.course_id, "section": dict(c.section)} for c in ordered],
    }
    if cost is not None:
        out["cost"] = round(cost, 6)
        out["metrics"] = dict(metrics or {})
    return out



def _section_family(sec: object) -> str:
    """Best-effort enrollment-family key: 221, 221A, 221B -> 221; TH16A -> TH16.

    Global Search does not expose CUNYfirst's enrollment/link id in our current scrape, so
    we deliberately use only the conservative section-number pattern we can verify from
    the public rows. If a course advertises LAB/REC rows but they cannot be linked to a
    lecture family, ``bundle_components`` returns no choices rather than pretending that
    a lecture alone is a complete registration.
    """
    import re

    value = str(sec or "").strip()
    m = re.match(r"^([A-Za-z]*\d+)", value)
    return (m.group(1) if m else value).upper()


def bundle_components(sections: Sequence[Mapping]) -> List[Mapping]:
    """Turn linked LEC + LAB/REC rows into complete schedulable choices.

    CUNY Global Search renders required components as separate rows (for example CSCI 111
    221-LEC with 221A/221B/...-LAB). The optimizer must select a complete registration
    bundle, not a naked lab or naked lecture. A bundle keeps the lecture as its primary
    meeting and flattens every required component meeting into ``extra`` so the existing
    conflict checker sees the whole registration.

    Only the verified section-family convention is inferred. Courses without both a LEC
    and LAB/REC remain one-row-per-choice. Courses that do have required secondary
    components but whose rows cannot be linked are rejected conservatively.
    """
    import itertools

    rows = [dict(s) for s in sections]
    if not rows:
        return []
    secondary_types = sorted({str(s.get("component", "")).upper() for s in rows} & {"LAB", "REC", "DIS"})
    lectures = [s for s in rows if str(s.get("component", "")).upper() == "LEC"]
    if not secondary_types or not lectures:
        return rows

    out: List[Mapping] = []
    for lecture in lectures:
        family = _section_family(lecture.get("sec"))
        by_type = []
        for component in secondary_types:
            matches = [s for s in rows
                       if str(s.get("component", "")).upper() == component
                       and _section_family(s.get("sec")) == family]
            if not matches:
                by_type = []
                break
            by_type.append(matches)
        if not by_type:
            continue

        for secondary in itertools.product(*by_type):
            parts = [lecture, *secondary]
            # A malformed linkage should never create an internally conflicting option.
            if any(sections_conflict(parts[i], parts[j])
                   for i in range(len(parts)) for j in range(i + 1, len(parts))):
                continue
            bundle = dict(lecture)
            bundle["sec"] = " + ".join(str(p.get("sec", "")) for p in parts)
            bundle["component"] = "+".join(str(p.get("component", "")) for p in parts)
            bundle["components"] = [dict(p) for p in parts]
            extras = list(lecture.get("extra") or [])
            for part in secondary:
                extras.append({"days": part.get("days", ""), "start": part.get("start"), "end": part.get("end")})
                extras.extend(list(part.get("extra") or []))
            bundle["extra"] = extras
            out.append(bundle)
    return out


def explore_course_schedules(
    course_sections: Mapping[str, Sequence[Mapping]],
    course_meta: Mapping[str, Mapping],
    *,
    required: Sequence[str] = (),
    min_courses: int = 5,
    target_credits: float = 15,
    max_credits: float = 20,
    max_courses: int = 7,
    limit: int = 20,
    valid_course_set=None,
    open_only: bool = True,
    weights: Optional[Mapping[str, float]] = None,
    max_campus_span_minutes: Optional[float] = None,
    ranking_pool: int = 80,
) -> List[Dict]:
    """Explore distinct course sets and rank them when preferences are supplied."""
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    if ranking_pool < limit:
        ranking_pool = limit

    normalized = normalize_weights(weights)
    ids = [cid for cid in course_sections if cid in course_meta and course_sections[cid]]
    required_ids = list(dict.fromkeys(required))
    if any(cid not in ids for cid in required_ids):
        return []

    chosen = list(required_ids)
    used_keys: Dict[str, int] = {}
    per_subject: Dict[str, int] = {}
    credits = 0.0
    for cid in chosen:
        meta = course_meta[cid]
        key = str(meta.get("key", cid))
        allowed = 2 if key == "W" else 1
        if used_keys.get(key, 0) >= allowed:
            return []
        used_keys[key] = used_keys.get(key, 0) + 1
        subject = str(meta.get("subject", ""))
        per_subject[subject] = per_subject.get(subject, 0) + 1
        if per_subject[subject] > 5:
            return []
        credits += float(meta.get("credits", 0) or 0)
    if credits > max_credits or len(chosen) > max_courses:
        return []

    optional = [cid for cid in ids if cid not in set(required_ids)]
    results: List[Dict] = []
    target_results = limit if normalized is None else ranking_pool

    def try_emit(total_credits: float) -> bool:
        if len(chosen) < min_courses or total_credits < target_credits:
            return False
        if valid_course_set is not None and not valid_course_set(tuple(chosen)):
            return False
        fixed = {cid: course_sections[cid] for cid in chosen}
        solved = optimize(
            fixed,
            limit=1,
            open_only=open_only,
            weights=weights,
            ranking_pool=100,
            max_campus_span_minutes=max_campus_span_minutes,
        )
        if not solved["schedules"]:
            return False
        result = solved["schedules"][0]
        choices = [Choice(item["course_id"], item["section"]) for item in result["sections"]]
        result["credits"] = total_credits
        result["metrics"] = schedule_metrics(choices, max_campus_span_minutes)
        if normalized is not None and "cost" not in result:
            result["cost"] = round(_cost(result["metrics"], normalized, max_campus_span_minutes), 6)
        results.append(result)
        return True

    if try_emit(credits) or len(results) >= target_results:
        if normalized is not None:
            results.sort(key=lambda r: (float(r.get("cost", 0.0)), tuple(sorted(x["course_id"] for x in r["sections"]))))
        return results[:limit]

    def dfs(start: int, total_credits: float) -> None:
        if len(results) >= target_results:
            return
        if len(chosen) >= max_courses or total_credits >= max_credits:
            return
        if len(chosen) + (len(optional) - start) < min_courses:
            return

        for i in range(start, len(optional)):
            if len(results) >= target_results:
                return
            cid = optional[i]
            meta = course_meta[cid]
            cr = float(meta.get("credits", 0) or 0)
            if total_credits + cr > max_credits:
                continue
            key = str(meta.get("key", cid))
            allowed = 2 if key == "W" else 1
            if used_keys.get(key, 0) >= allowed:
                continue
            subject = str(meta.get("subject", ""))
            if per_subject.get(subject, 0) >= 5:
                continue

            chosen.append(cid)
            used_keys[key] = used_keys.get(key, 0) + 1
            per_subject[subject] = per_subject.get(subject, 0) + 1
            new_credits = total_credits + cr
            emitted = try_emit(new_credits)
            if not emitted:
                dfs(i + 1, new_credits)
            per_subject[subject] -= 1
            if not per_subject[subject]:
                del per_subject[subject]
            used_keys[key] -= 1
            if not used_keys[key]:
                del used_keys[key]
            chosen.pop()

    dfs(0, credits)
    if normalized is not None:
        results.sort(key=lambda r: (float(r.get("cost", 0.0)), tuple(sorted(x["course_id"] for x in r["sections"]))))
    return results[:limit]

