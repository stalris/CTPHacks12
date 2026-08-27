"""Production smoke check for server.py -> optimizer.py wiring."""
from optimizer import sections_conflict
from server import MIN_COURSES, by_code, merge_availability, sanitize_schedule_profile, suggest

probe = suggest({"program": "CSCI-BS", "terms": [], "term": "Fall", "fresh": True})
assert "optimizer" in probe
info = probe["optimizer"]
assert info["limit"] == 20
assert info["poolCourses"] > 0 and info["poolSections"] > 0
assert info["applied"], info
assert 1 <= len(info["schedules"]) <= 20

chosen = [p["sections"][0] for p in probe["suggested"]]
assert len(chosen) >= MIN_COURSES
assert all(str(s.get("status", "")).lower() == "open" for s in chosen)
assert all(not sections_conflict(chosen[i], chosen[j])
           for i in range(len(chosen)) for j in range(i + 1, len(chosen)))

# Every returned alternative represents a different course set rather than
# spending the first 20 results on section permutations of the same courses.
sets = [frozenset(x["course_id"] for x in schedule["sections"])
        for schedule in info["schedules"]]
assert len(sets) == len(set(sets))

# Pins stay first in the proposal after section solving.
pinned = suggest({"program": "CSCI-BS", "terms": [], "term": "Fall",
                  "pins": [by_code["MATH 141"]], "fresh": True})
assert pinned["suggested"][0]["id"] == by_code["MATH 141"]

# Once a term is approved, subsequent terms are academic projections. They
# must retain the existing picker instead of dropping catalog-only courses.
future = suggest({"program": "CSCI-BS", "terms": [[probe["suggested"][0]["id"]]], "term": "Spring", "fresh": True})
assert not future["optimizer"]["applied"]
assert future["optimizer"]["reason"].startswith("Future pattern term")
print("optimizer production integration OK")


profile = sanitize_schedule_profile({
    "summary": "commute matters", "weights": {"campus_days": 2, "gap_minutes": -1, "campus_span_minutes": 0.75},
    "availability": {"busy": [["Tu", 540, 780], ["XX", 1, 2]], "earliest": 480, "latest": 1200},
    "commuteMinutes": 999, "maxCampusSpanMinutes": 360, "source": "gemini",
})
assert profile["weights"]["campus_days"] == 1.0 and profile["weights"]["gap_minutes"] == 0.0
assert profile["availability"]["busy"] == [("Tu", 540, 780)] and profile["commuteMinutes"] == 360
combined = merge_availability({"busy": [["Mo", 600, 720]], "earliest": 540, "latest": 1320}, profile["availability"])
assert combined["earliest"] == 540 and combined["latest"] == 1200 and ("Mo", 600, 720) in combined["busy"] and ("Tu", 540, 780) in combined["busy"]
print("schedule profile sanitization OK")
