"""Self-check for backend/optimizer.py: python backend/test_optimizer.py"""
from optimizer import (
    Choice,
    meetings_conflict,
    sections_conflict,
    feasible_schedules,
    normalize_weights,
    optimize,
    schedule_metrics,
    bundle_components,
    explore_course_schedules,
)


def sec(name, days, start, end, *, status="Open", extra=None):
    return {
        "sec": name,
        "component": "LEC",
        "days": days,
        "start": start,
        "end": end,
        "extra": extra or [],
        "status": status,
    }


# Basic overlap semantics: touching endpoints are fine; different days are fine.
a = sec("A", "MoWe", 10 * 60, 11 * 60)
b = sec("B", "Mo", 10 * 60 + 30, 11 * 60 + 30)
c = sec("C", "Mo", 11 * 60, 12 * 60)
d = sec("D", "TuTh", 10 * 60 + 30, 11 * 60 + 30)
assert meetings_conflict(a, b)
assert not meetings_conflict(a, c)
assert not meetings_conflict(a, d)

# Extra meetings matter too: lecture times do not clash, but the lab does.
labbed = sec("LABBED", "Tu", 9 * 60, 10 * 60,
             extra=[{"days": "Fr", "start": 13 * 60, "end": 15 * 60}])
friday = sec("FRI", "Fr", 14 * 60, 16 * 60)
assert sections_conflict(labbed, friday)
print("conflict checks OK")

# Backtracking must skip a tempting first section and find the second valid combination.
options = {
    "CSCI": [sec("01", "Mo", 9 * 60, 10 * 60), sec("02", "Tu", 9 * 60, 10 * 60)],
    "MATH": [sec("01", "Mo", 9 * 60 + 30, 10 * 60 + 30)],
}
found = list(feasible_schedules(options))
assert len(found) == 1
chosen = {x.course_id: x.section["sec"] for x in found[0]}
assert chosen == {"CSCI": "02", "MATH": "01"}, chosen
print("backtracking + pruning OK")

# Closed and wait-listed sections are rejected by default, but can be included explicitly.
closed_only = {"A": [sec("01", "Mo", 9 * 60, 10 * 60, status="Closed")]}
wait_only = {"A": [sec("01", "Mo", 9 * 60, 10 * 60, status="Wait List")]}
unknown_only = {"A": [sec("01", "Mo", 9 * 60, 10 * 60, status="")]}
assert list(feasible_schedules(closed_only)) == []
assert list(feasible_schedules(wait_only)) == []
assert list(feasible_schedules(unknown_only)) == []
assert len(list(feasible_schedules(closed_only, open_only=False))) == 1
assert len(list(feasible_schedules(wait_only, open_only=False))) == 1
assert len(list(feasible_schedules(unknown_only, open_only=False))) == 1
print("section-status guard OK")

# Neutral mode is deterministic and paginates without needing all combinations first.
paged = {
    "A": [sec("01", "Mo", 8 * 60, 9 * 60), sec("02", "Tu", 8 * 60, 9 * 60)],
    "B": [sec("01", "We", 10 * 60, 11 * 60), sec("02", "Th", 10 * 60, 11 * 60)],
}
first = optimize(paged, limit=2)
second = optimize(paged, limit=2, offset=first["next_offset"])
assert first["ranking"] == "neutral" and first["weights"] is None
assert len(first["schedules"]) == 2 and len(second["schedules"]) == 2
assert second["next_offset"] is None
assert first["schedules"] != second["schedules"]
print("neutral pagination OK")

# Weight validation: bounded inputs only, normalized internally.
w = normalize_weights({"campus_days": 1.0, "gap_minutes": 1.0})
assert round(w["campus_days"], 3) == 0.5 and round(w["gap_minutes"], 3) == 0.5
try:
    normalize_weights({"campus_days": 1.5})
    raise AssertionError("unbounded weight should fail")
except ValueError:
    pass
try:
    normalize_weights({"invented_metric": 0.5})
    raise AssertionError("unknown weight should fail")
except ValueError:
    pass
print("weight validation OK")

# Weighted mode should prefer two classes on one campus day over equivalent classes on two days.
weighted = {
    "A": [sec("MON", "Mo", 9 * 60, 10 * 60), sec("TUE", "Tu", 9 * 60, 10 * 60)],
    "B": [sec("MON", "Mo", 10 * 60, 11 * 60), sec("WED", "We", 10 * 60, 11 * 60)],
}
r = optimize(weighted, limit=1, weights={"campus_days": 1.0})
best = {x["course_id"]: x["section"]["sec"] for x in r["schedules"][0]["sections"]}
assert best == {"A": "MON", "B": "MON"}, best
assert r["schedules"][0]["metrics"]["campus_days"] == 1.0
print("weighted ranking OK")

# Metric smoke test: a same-day gap is counted, and early time is a cost.
s = [
    Choice("A", sec("1", "Mo", 8 * 60, 9 * 60)),
    Choice("B", sec("1", "Mo", 10 * 60, 11 * 60)),
]
m = schedule_metrics(s)
assert m["campus_days"] == 1.0 and m["gap_minutes"] == 60.0 and m["early_minutes"] == 120.0, m
print("metrics OK")

print("optimizer OK")


# Linked CUNY components: 221-LEC must be paired with exactly one 221A/221B lab.
lec = sec("221", "TuTh", 13 * 60 + 40, 14 * 60 + 30)
lec["component"] = "LEC"
lab_a = sec("221A", "TuTh", 12 * 60 + 15, 13 * 60 + 5); lab_a["component"] = "LAB"
lab_b = sec("221B", "TuTh", 14 * 60 + 45, 15 * 60 + 35); lab_b["component"] = "LAB"
bundles = bundle_components([lec, lab_a, lab_b])
assert len(bundles) == 2, bundles
assert {b["sec"] for b in bundles} == {"221 + 221A", "221 + 221B"}
assert all(len(b["components"]) == 2 and len(b["extra"]) == 1 for b in bundles)
clash = sec("X", "Tu", 12 * 60 + 30, 12 * 60 + 45)
assert sections_conflict(next(b for b in bundles if b["sec"].endswith("221A")), clash)
print("linked component bundles OK")

# Broad exploration emits distinct course sets, honors a required/pinned course, and
# delegates actual time feasibility to the fixed-course optimizer.
pool = {
    "A": [sec("1", "Mo", 9 * 60, 10 * 60)],
    "B": [sec("1", "Tu", 9 * 60, 10 * 60)],
    "C": [sec("1", "We", 9 * 60, 10 * 60)],
    "D": [sec("1", "Th", 9 * 60, 10 * 60)],
    "E": [sec("1", "Fr", 9 * 60, 10 * 60)],
    "F": [sec("1", "Mo", 11 * 60, 12 * 60)],
}
meta = {cid: {"credits": 3, "subject": cid, "key": cid} for cid in pool}
broad = explore_course_schedules(pool, meta, required=["A"], limit=3)
assert len(broad) == 3, broad
sets = [frozenset(x["course_id"] for x in s["sections"]) for s in broad]
assert len(set(sets)) == len(sets)
assert all("A" in ids and len(ids) == 5 for ids in sets)
print("broad distinct schedule search OK")

# A two-day early class contributes twice to weekly early burden.
weekly = schedule_metrics([Choice("A", sec("1", "TuTh", 9 * 60, 10 * 60))])
assert weekly["early_minutes"] == 120.0, weekly
print("weekly preference metrics OK")


# A 9-2 campus day is five hours; a four-hour preferred maximum penalizes one hour.
span_sched = [Choice("A", sec("1", "Mo", 9 * 60, 10 * 60)), Choice("B", sec("1", "Mo", 13 * 60, 14 * 60))]
span = schedule_metrics(span_sched, max_campus_span_minutes=4 * 60)
assert span["campus_span_minutes"] == 5 * 60 and span["max_daily_span_minutes"] == 5 * 60 and span["excess_span_minutes"] == 60, span
print("campus span preference metrics OK")

weighted_pool = {
    "A": [sec("1", "Mo", 9 * 60, 10 * 60)], "B": [sec("1", "Mo", 10 * 60, 11 * 60)],
    "C": [sec("1", "Tu", 9 * 60, 10 * 60)], "D": [sec("1", "We", 9 * 60, 10 * 60)],
    "E": [sec("1", "Th", 9 * 60, 10 * 60)], "F": [sec("1", "Mo", 11 * 60, 12 * 60)],
}
weighted_meta = {cid: {"credits": 3, "subject": cid, "key": cid} for cid in weighted_pool}
ranked = explore_course_schedules(weighted_pool, weighted_meta, min_courses=5, target_credits=15, limit=2, weights={"campus_days": 1.0}, ranking_pool=10)
assert ranked and ranked[0]["cost"] <= ranked[-1]["cost"], ranked
print("weighted broad schedule ranking OK")
