import hashlib
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
path = Path(__file__).with_name("latest_match_results.json")
data = json.loads(path.read_text(encoding="utf-8"))
old = json.loads((ROOT / "tmp/audit_library_20261120/match_results_qwen.json").read_text(encoding="utf-8"))
matches = [(i, row) for i, row in enumerate(data, 1) if row.get("decision", {}).get("status") == "matched"]

def show(label, value):
    print(label, json.dumps(value, ensure_ascii=False))

def short(index, row):
    source, target = row.get("spotify", {}), row.get("youtube", {})
    decision = row.get("decision", {})
    ai = row.get("search", {}).get("ai", {})
    return {
        "row": index, "source": source.get("title"), "artist": source.get("artists"),
        "source_ms": source.get("duration_ms"), "target": target.get("title"),
        "target_artists": target.get("artists"), "target_ms": target.get("duration_ms"),
        "id": target.get("videoId"), "type": target.get("type"), "rank": target.get("rank"),
        "checks": decision.get("checks"), "notes": decision.get("notes"),
        "delta_ms": row.get("scores", {}).get("duration_delta_ms"),
        "AI": ai.get("response"), "AI_error": ai.get("error"),
    }

show("SAME_AS_LOCAL", hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256((ROOT / "match_results_qwen.json").read_bytes()).digest())
show("ROWS", len(data))
for label, getter in {
    "DECISIONS": lambda row: row.get("decision", {}).get("status"),
    "ADDITIONS": lambda row: row.get("addition_status"),
    "SELECTIONS": lambda row: row.get("decision", {}).get("notes"),
    "AI_STATUSES": lambda row: row.get("search", {}).get("ai", {}).get("status"),
    "POOL_SIZE": lambda row: len(row.get("search", {}).get("checked_candidates", [])),
    "AI_MODEL": lambda row: row.get("search", {}).get("ai", {}).get("model"),
}.items():
    show(label, Counter(getter(row) for row in data))
show("MATCH_CHECKS", Counter(str(row["decision"]["checks"]) for _, row in matches))
show("ADD_ERRORS", Counter(re.sub(r"failed_to_add_video_id=[^;]+; ", "", row.get("addition_error", "")) for _, row in matches))
show("RESCUED", [short(i, row) for i, row in matches if "ai" in row["decision"].get("notes", "")])
show("WARNINGS", [short(i, row) for i, row in matches if row["decision"].get("red_flags") or (row.get("scores", {}).get("duration_delta_ms") or 0) > 5000])
show("MISSING_ARTIST", [short(i, row) for i, row in matches if not row["decision"].get("checks", {}).get("artist_match")])
markers = r"(?i)(?<!\w)(?:live|remix|cover|karaoke|instrumental|acoustic|unplugged|slowed|sped|revisited|remaster\w*|רמיקס|לייב|בהופעה|חי\s+באולפן|אקוסטי|אנפלגד|קאבר|מחרוזת|ריביזיטד)(?!\w)"
show("VERSION_NAMES", [short(i, row) for i, row in matches if re.search(markers, row["youtube"]["title"])])
show("NO_MATCH_TITLES", [{"row": i, "title": row["spotify"]["title"], "artists": row["spotify"]["artists"],
                          "note": row["decision"].get("notes"), "AI": row.get("search", {}).get("ai", {}).get("response")}
                         for i, row in enumerate(data, 1) if row["decision"]["status"] == "no_match"])
old_by_id = {row["spotify"].get("id"): row for row in old if row["decision"]["status"] != "skipped_duplicate"}
changes = []
for i, row in enumerate(data, 1):
    before = old_by_id.get(row["spotify"].get("id"))
    if before and row["decision"]["status"] != "skipped_duplicate" and (
        before["decision"]["status"] != row["decision"]["status"]
        or before["youtube"].get("videoId") != row["youtube"].get("videoId")
    ):
        changes.append({"row": i, "source": row["spotify"]["title"],
                        "old_status": before["decision"]["status"], "old_title": before["youtube"].get("title"),
                        "new_status": row["decision"]["status"], "new_title": row["youtube"].get("title")})
show("CHANGED_FROM_PREVIOUS", changes)
show("LAST_ROW", short(len(data), data[-1]))
