import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
source = Path(__file__).with_name("match_results_qwen.json")
data = json.loads(source.read_text(encoding="utf-8"))
local = ROOT / "match_results_qwen.json"
print("SAME_AS_LOCAL", hashlib.sha256(source.read_bytes()).hexdigest() == hashlib.sha256(local.read_bytes()).hexdigest())
print("ROWS", len(data))
for field, getter in {
    "decision": lambda r: r.get("decision", {}).get("status"),
    "addition": lambda r: r.get("addition_status"),
    "selection": lambda r: r.get("decision", {}).get("notes"),
    "AI_status": lambda r: r.get("search", {}).get("ai", {}).get("status"),
    "AI_model": lambda r: r.get("search", {}).get("ai", {}).get("model"),
    "AI_decision": lambda r: (r.get("search", {}).get("ai", {}).get("response") or {}).get("decision"),
    "candidates": lambda r: len(r.get("search", {}).get("checked_candidates", [])),
}.items():
    print(field, json.dumps(Counter(getter(row) for row in data), ensure_ascii=False))

def summary(index, row):
    spotify, youtube = row.get("spotify", {}), row.get("youtube", {})
    search, decision = row.get("search", {}), row.get("decision", {})
    ai = search.get("ai", {})
    return {
        "row": index, "title": spotify.get("title"), "artists": spotify.get("artists"),
        "target": youtube.get("title"), "target_artists": youtube.get("artists"),
        "channel": youtube.get("channel"), "type": youtube.get("type"), "rank": youtube.get("rank"),
        "delta": row.get("scores", {}).get("duration_delta_ms"),
        "decision": decision, "addition": row.get("addition_status"),
        "addition_error": row.get("addition_error"), "AI": ai,
    }

for label, predicate in (
    ("NOT_MATCHED", lambda r: r.get("decision", {}).get("status") != "matched"),
    ("MATCHED_WARNINGS", lambda r: r.get("decision", {}).get("status") == "matched" and (
        not r.get("decision", {}).get("checks", {}).get("artist_match")
        or (r.get("scores", {}).get("duration_delta_ms") or 0) > 10000
        or r.get("decision", {}).get("notes") == "selected_by_ai_rescue"
        or r.get("addition_status") not in ("added", "skipped_duplicate_video_id"))),
):
    print(label)
    for index, row in enumerate(data, 1):
        if predicate(row):
            print(json.dumps(summary(index, row), ensure_ascii=False))

print("AI_ERRORS", json.dumps(Counter(r.get("search", {}).get("ai", {}).get("error") for r in data), ensure_ascii=False))
print("ADD_ERRORS", json.dumps(Counter(r.get("addition_error") for r in data), ensure_ascii=False))
print("SEARCH_ERRORS", json.dumps([{"row":i,"errors":r.get("search",{}).get("errors")} for i,r in enumerate(data,1) if r.get("search",{}).get("errors")], ensure_ascii=False))
print("LAST_SAVED", json.dumps([summary(i, data[i-1]) for i in range(max(1,len(data)-3),len(data)+1)], ensure_ascii=False))
temporary = ROOT / "match_results_qwen.json.tmp"
try:
    next_data = json.loads(temporary.read_text(encoding="utf-8"))
    print("TEMP_ROWS", len(next_data))
    print("TEMP_EXTENDS_SAVED", next_data[:len(data)] == data)
    print("TEMP_LAST", json.dumps([summary(i, next_data[i-1]) for i in range(max(1,len(next_data)-1), len(next_data)+1)], ensure_ascii=False))
except Exception as error:
    print("TEMP_READ_ERROR", repr(error))

with (ROOT / "spotifyplaylist.csv").open(encoding="utf-8-sig", newline="") as stream:
    tracks = list(csv.DictReader(stream))
print("CSV_ROWS", len(tracks))
for row in (data[-1], next_data[-1] if "next_data" in globals() else data[-1]):
    spotify = row.get("spotify", {})
    positions = [i for i,track in enumerate(tracks,1) if track.get("Track URI", "").endswith(spotify.get("id") or "__missing__")]
    print("CSV_POSITION", spotify.get("title"), positions)
    for position in positions:
        print("CSV_NEXT", json.dumps([{key:track.get(key) for key in ("Track Name", "Artist Name(s)", "Duration (ms)")} for track in tracks[position:position+2]], ensure_ascii=False))
