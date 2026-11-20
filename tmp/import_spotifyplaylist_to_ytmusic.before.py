import csv
import json
import re
import time
import urllib.error
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

from ytmusicapi import YTMusic

# =======================
# CONFIG
# =======================
CSV_FILE = "spotifyplaylist.csv"
OAUTH_FILE = "oauth.json"
MATCH_FIXED_JSON = "match_fixed.json"

AI_MODEL_CHOICE = "qwen"
AI_MODEL_PRESETS = {
    "qwen": {
        "model_id": "qwen/qwen3.5-35b-a3b",
        "results_json": "match_results_qwen.json",
    },
    "gpt": {
        "model_id": "openai/gpt-oss-20b",
        "results_json": "match_results_gpt.json",
    },
}

YTM_PLAYLIST_ID = "PLA-1o7Z7atMV0I-RbPelr_yrgBE5bKNG-"
I_UNDERSTAND_THIS_WILL_DELETE_THE_PLAYLIST = True

SLEEP_BETWEEN_REQUESTS = 0.35
REMOVE_BATCH = 50
ADD_RETRIES = 3
ADD_RETRY_SLEEP = 1.0

TOP_RESULTS = 3
DURATION_DELTA_MAX_MS = 5000
VERY_CLOSE_DURATION_MS = 3000
RESCUE_RESULTS = 5
SONG_DURATION_HARD_REJECT_MS = 28000
VIDEO_DURATION_HARD_REJECT_MS = 22000

AI_MATCH_ENABLED = True
AI_MATCH_BASE_URL = "http://127.0.0.1:1234/v1"
AI_MATCH_TOP_RESULTS = 3
AI_MATCH_POOL_SIZE = 6
AI_MATCH_TIMEOUT_SEC = 90
AI_MATCH_MAX_DURATION_DELTA_MS = 25000
PERFECT_MATCH_TITLE_SIM_MIN = 0.98
PERFECT_MATCH_ARTIST_OR_CHANNEL_MIN = 0.90
PERFECT_MATCH_DURATION_DELTA_MS = 1500
AI_PREFER_SONG_SCORE_GAP_MAX = 0.06
AI_PREFER_SONG_TITLE_SIM_MIN = 0.88
OBVIOUS_NO_MATCH_TITLE_MAX = 0.42
OBVIOUS_NO_MATCH_ARTIST_OR_CHANNEL_MAX = 0.34
OBVIOUS_NO_MATCH_SCORE_MAX = 0.38
OBVIOUS_NO_MATCH_DURATION_MS = 45000

# Duration policy (stricter, to reduce false positives)
# Song: strict 0-5s only.
# Video: default 0-10s, allow 10-20s only if strong title + official/strong channel and no version markers.
SONG_MAX_DELTA_MS = 5000
VIDEO_BASE_MAX_DELTA_MS = 10000
VIDEO_EXT_MAX_DELTA_MS = 20000
VIDEO_EXT_TITLE_SIM_MIN = 0.90
VIDEO_EXT_CHANNEL_SIM_MIN = 0.70
VIDEO_CHANNEL_SIM_STRONG = 0.60

# Algorithm-check policy knobs (keep in sync with algorithm_check.py)
CHECK_SONG_TITLE_MIN = 0.62
CHECK_VIDEO_TITLE_MIN = 0.66
CHECK_SONG_SCORE_MIN = 0.56
CHECK_VIDEO_SCORE_MIN = 0.58
CHECK_ARTIST_STRONG_SONG = 0.34
CHECK_ARTIST_STRONG_VIDEO = 0.32
CHECK_CHANNEL_STRONG = 0.55
CHECK_CHANNEL_MIN_VIDEO = 0.42
CHECK_AMBIGUOUS_SCORE_CUTOFF = 0.70

# If duration is very close and Spotify title tokens are well-covered in the candidate title,
# allow weaker artist/channel checks (helps cases where uploader/channel name differs).
CHECK_TITLE_DURATION_COVERAGE_RELAX = 0.88

SONG_TITLE_SIM_MIN = 0.72
VIDEO_TITLE_SIM_MIN = 0.82
SONG_ARTIST_SIM_MIN = 0.30
VIDEO_ARTIST_SIM_MIN = 0.35
VIDEO_CHANNEL_SIM_MIN = 0.45
SHORT_TITLE_MAX_TOKENS = 2

TITLE_NOISE_WORDS = [
    "official", "video", "lyrics", "lyric", "audio", "prod", "performance", "session",
]
VERSION_MARKERS = [
    "remix", "edit", "version", "live", "sped up", "nightcore", "reupload",
    "cover", "instrumental", "slowed", "reverb",
    # Hebrew
    "לייב", "הופעה", "בהופעה", "רמיקס", "קאבר", "כיסוי", "אינסטרומנטל", "אינסטרומנטלי",
    "גרסה", "גרסת", "וורסן", "הקלטה", "סשן",
]
FEAT_PATTERN = r"\b(feat|ft|featuring)\b"

HEBREW_TO_LATIN = {
    "א": "a", "ב": "b", "ג": "g", "ד": "d", "ה": "a", "ו": "u", "ז": "z",
    "ח": "h", "ט": "t", "י": "i", "כ": "k", "ך": "k", "ל": "l", "מ": "m",
    "ם": "m", "נ": "n", "ן": "n", "ס": "s", "ע": "a", "פ": "p", "ף": "p",
    "צ": "ts", "ץ": "ts", "ק": "k", "ר": "r", "ש": "sh", "ת": "t",
}

ARTIST_ALIAS_GROUPS = [
    ["ננח קלאסי", "classy nanach", "nanach classy"],
    ["tuna", "טונה"],
    ["ravid plotnik", "רביד פלוטניק"],
    ["balulu", "בלולו"],
]


# =======================
# Generic Helpers
# =======================
def clean(s):
    return (s or "").strip()


def ratio(a, b):
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def normalize_spaces(s):
    return re.sub(r"\s+", " ", s).strip()


def transliterate_hebrew_to_latin(s):
    out = []
    for ch in s:
        out.append(HEBREW_TO_LATIN.get(ch, ch))
    return normalize_spaces("".join(out))


def normalize_basic_text(s):
    s = clean(s).lower()
    s = re.sub(r"[^0-9a-z\u0590-\u05ff\s]", " ", s)
    s = normalize_spaces(s)
    return s


def normalize_artist_text(s):
    s = clean(s).lower()
    s = re.sub(r"[\.\u200f\u200e]", " ", s)
    s = re.sub(r"[^0-9a-z\u0590-\u05ff\s]", " ", s)
    s = normalize_spaces(s)
    return s


def normalize_title_text(s):
    s = clean(s).lower()
    # Remove brackets and content inside brackets.
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", " ", s)
    # Remove "feat/ft/featuring ..."
    s = re.sub(FEAT_PATTERN + r".*$", " ", s)
    # Remove generic noisy words.
    for w in TITLE_NOISE_WORDS:
        s = re.sub(r"\b" + re.escape(w) + r"\b", " ", s)
    # Remove separators.
    s = re.sub(r"[|\-:/]+", " ", s)
    # Remove punctuation / symbols.
    s = re.sub(r"[^0-9a-z\u0590-\u05ff\s]", " ", s)
    s = normalize_spaces(s)
    return s


def contains_any_marker(text_raw, markers):
    t = clean(text_raw).lower()
    return any(m in t for m in markers)


def split_artists_field(artists_raw):
    raw = clean(artists_raw)
    if not raw:
        return []
    parts = re.split(r"[;,/]| & ", raw)
    return [clean(p) for p in parts if clean(p)]


def build_alias_group_lookup():
    groups = []
    for g in ARTIST_ALIAS_GROUPS:
        cleaned = []
        for name in g:
            c = normalize_artist_text(name)
            if c:
                cleaned.append(c)
                tr = normalize_artist_text(transliterate_hebrew_to_latin(c))
                if tr and tr != c:
                    cleaned.append(tr)
        uniq = sorted(set(cleaned))
        if uniq:
            groups.append(uniq)
    return groups


ALIAS_GROUPS_LOOKUP = build_alias_group_lookup()


def artist_variants(artist_clean):
    variants = set()
    if not artist_clean:
        return variants
    variants.add(artist_clean)
    tr = normalize_artist_text(transliterate_hebrew_to_latin(artist_clean))
    if tr:
        variants.add(tr)
    for group in ALIAS_GROUPS_LOOKUP:
        if artist_clean in group:
            for item in group:
                variants.add(item)
    return variants


def extract_spotify_id(track_uri):
    uri = clean(track_uri)
    if not uri:
        return None
    if ":" in uri:
        return uri.split(":")[-1]
    return uri


def parse_int(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except Exception:
        return None


def parse_duration_to_ms(duration_text):
    txt = clean(duration_text)
    if not txt:
        return None
    if txt.isdigit():
        return int(txt) * 1000
    parts = txt.split(":")
    if not all(p.isdigit() for p in parts):
        return None
    nums = [int(x) for x in parts]
    if len(nums) == 2:
        seconds = nums[0] * 60 + nums[1]
    elif len(nums) == 3:
        seconds = nums[0] * 3600 + nums[1] * 60 + nums[2]
    else:
        return None
    return seconds * 1000


def get_ai_model_config():
    choice = clean(AI_MODEL_CHOICE).lower()
    cfg = AI_MODEL_PRESETS.get(choice)
    if not cfg:
        supported = ", ".join(sorted(AI_MODEL_PRESETS))
        raise ValueError(f"Unsupported AI_MODEL_CHOICE={AI_MODEL_CHOICE!r}. Expected one of: {supported}")
    return choice, cfg["model_id"], cfg["results_json"]


AI_MODEL_KEY, AI_MATCH_MODEL, MATCH_RESULTS_JSON = get_ai_model_config()


def extract_result_duration_ms(r):
    sec = r.get("duration_seconds")
    if sec is not None:
        try:
            return int(sec) * 1000
        except Exception:
            pass
    dur = r.get("duration")
    if dur:
        ms = parse_duration_to_ms(dur)
        if ms is not None:
            return ms
    length = r.get("length")
    if length:
        ms = parse_duration_to_ms(length)
        if ms is not None:
            return ms
    return None


def persist_results(results):
    target = Path(MATCH_RESULTS_JSON)
    temp = target.with_suffix(target.suffix + ".tmp")
    payload = json.dumps(results, ensure_ascii=False, indent=2)
    temp.write_text(payload, encoding="utf-8")
    temp.replace(target)


def extract_result_artists(r):
    arts = []
    for a in (r.get("artists") or []):
        n = clean(a.get("name", ""))
        if n:
            arts.append(n)
    return arts


def extract_result_channel(r):
    # ytmusicapi may expose channel-like data under different keys by result type.
    author = clean(r.get("author", ""))
    if author:
        return author
    byline = clean(r.get("byline", ""))
    if byline:
        return byline
    arts = extract_result_artists(r)
    if arts:
        return arts[0]
    return ""


def make_thumbnail_url(video_id):
    vid = clean(video_id)
    if not vid:
        return None
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"


def extract_thumbnail_url(r):
    thumbs = r.get("thumbnails") or []
    if isinstance(thumbs, list) and thumbs:
        best = thumbs[-1]
        if isinstance(best, dict):
            u = clean(best.get("url", ""))
            if u:
                return u
    thumb = r.get("thumbnail")
    if isinstance(thumb, dict):
        u = clean(thumb.get("url", ""))
        if u:
            return u
    if isinstance(thumb, str) and clean(thumb):
        return clean(thumb)
    return make_thumbnail_url(r.get("videoId"))


def token_overlap(a, b):
    at = [t for t in normalize_basic_text(a).split() if t]
    bt = set([t for t in normalize_basic_text(b).split() if t])
    if not at:
        return 0.0
    inter = sum(1 for t in at if t in bt)
    return inter / max(1, len(at))


def best_name_similarity(variants_a, variants_b):
    best = 0.0
    for a in variants_a:
        for b in variants_b:
            if not a or not b:
                continue
            if a == b:
                best = max(best, 1.0)
            if a in b or b in a:
                best = max(best, 0.95)
            best = max(best, ratio(a, b))
            best = max(best, token_overlap(a, b))
    return best


def calc_artist_similarity(spotify_artists_clean, yt_artist_names_clean, yt_title_clean, yt_channel_clean):
    spotify_variants = set()
    for a in spotify_artists_clean:
        spotify_variants.update(artist_variants(a))
    if not spotify_variants:
        return 0.0

    yt_variants = set()
    for a in yt_artist_names_clean:
        if a:
            yt_variants.update(artist_variants(a))
    if yt_channel_clean:
        yt_variants.update(artist_variants(yt_channel_clean))

    base = best_name_similarity(spotify_variants, yt_variants) if yt_variants else 0.0

    # Also treat artist appearing in title as signal.
    title_signal = 0.0
    for sa in spotify_variants:
        if not sa:
            continue
        if sa in yt_title_clean:
            title_signal = max(title_signal, 1.0)
        else:
            title_signal = max(title_signal, token_overlap(sa, yt_title_clean))

    return max(base, title_signal * 0.85)


def calc_channel_similarity(spotify_artists_clean, channel_clean):
    if not channel_clean:
        return 0.0
    spotify_variants = set()
    for a in spotify_artists_clean:
        spotify_variants.update(artist_variants(a))
    if not spotify_variants:
        return 0.0
    return best_name_similarity(spotify_variants, artist_variants(channel_clean))


def is_official_or_topic(title_clean, channel_clean):
    combined = f"{title_clean} {channel_clean}".lower()
    markers = [
        "official", "official artist channel", "topic", "vevo",
    ]
    return any(m in combined for m in markers)


def build_spotify_track(row):
    title_raw = clean(row.get("Track Name"))
    artists_raw = clean(row.get("Artist Name(s)"))
    album_raw = clean(row.get("Album Name"))
    duration_ms = parse_int(row.get("Duration (ms)"))
    spotify_id = extract_spotify_id(row.get("Track URI"))

    artists_list = split_artists_field(artists_raw)
    artists_clean = [normalize_artist_text(a) for a in artists_list if normalize_artist_text(a)]

    title_clean = normalize_title_text(title_raw)
    title_alt = None
    if " - " in title_raw:
        parts = title_raw.split(" - ", 1)
        right = normalize_title_text(parts[1])
        if right and right != title_clean:
            title_alt = right

    spotify_has_version = contains_any_marker(title_raw, VERSION_MARKERS)
    artist_primary = artists_list[0] if artists_list else ""

    return {
        "id": spotify_id,
        "title": title_raw,
        "title_clean": title_clean,
        "title_alt": title_alt,
        "artists": artists_list,
        "artists_clean": artists_clean,
        "album": album_raw,
        "duration_ms": duration_ms,
        "artist_primary": artist_primary,
        "spotify_has_version": spotify_has_version,
    }


def build_queries(track):
    primary = normalize_spaces(f"{track['artist_primary']} {track['title_clean']}")
    queries = [primary] if primary else []
    if track["title_alt"]:
        alt = normalize_spaces(f"{track['artist_primary']} {track['title_alt']}")
        if alt and alt not in queries:
            queries.append(alt)
    return queries


def build_ai_queries(track, base_queries):
    queries = []
    for q in base_queries or []:
        q = normalize_spaces(q)
        if q and q not in queries:
            queries.append(q)

    title_variants = [track.get("title_clean")]
    if track.get("title_alt"):
        title_variants.append(track.get("title_alt"))

    for title_variant in title_variants:
        title_variant = normalize_spaces(title_variant or "")
        if not title_variant:
            continue
        if title_variant not in queries:
            queries.append(title_variant)
        if track.get("artist_primary"):
            reversed_query = normalize_spaces(f"{title_variant} {track['artist_primary']}")
            if reversed_query and reversed_query not in queries:
                queries.append(reversed_query)

    return queries


def extract_candidate(r, candidate_type, rank=None):
    if not r or not r.get("videoId"):
        return None
    title = clean(r.get("title", ""))
    artists = extract_result_artists(r)
    channel = extract_result_channel(r)
    duration_ms = extract_result_duration_ms(r)

    return {
        "type": candidate_type,  # "song" | "video"
        "rank": rank,
        "videoId": r.get("videoId"),
        "title": title,
        "title_clean": normalize_title_text(title),
        "artists": artists,
        "artists_clean": [normalize_artist_text(a) for a in artists if normalize_artist_text(a)],
        "channel": channel,
        "channel_clean": normalize_artist_text(channel),
        "url": f"https://music.youtube.com/watch?v={r.get('videoId')}",
        "thumbnail_url": extract_thumbnail_url(r),
        "duration_ms": duration_ms,
        "has_version_marker": contains_any_marker(title, VERSION_MARKERS),
    }


def candidate_public_payload(cand):
    if not cand:
        return None
    return {
        "type": cand.get("type"),
        "rank": cand.get("rank"),
        "videoId": cand.get("videoId"),
        "title": cand.get("title"),
        "title_clean": cand.get("title_clean"),
        "artists": cand.get("artists") or [],
        "channel": cand.get("channel"),
        "url": cand.get("url"),
        "thumbnail_url": cand.get("thumbnail_url"),
        "duration_ms": cand.get("duration_ms"),
    }


def youtube_payload_from_candidate(cand, forced_type=None):
    if not cand:
        return {
            "type": None,
            "videoId": None,
            "title": None,
            "title_clean": None,
            "channel": None,
            "url": None,
            "thumbnail_url": None,
            "duration_ms": None,
        }
    return {
        "type": forced_type or cand.get("type"),
        "rank": cand.get("rank"),
        "videoId": cand.get("videoId"),
        "title": cand.get("title"),
        "title_clean": cand.get("title_clean"),
        "channel": cand.get("channel"),
        "url": cand.get("url"),
        "thumbnail_url": cand.get("thumbnail_url"),
        "duration_ms": cand.get("duration_ms"),
    }


def format_duration_ms(duration_ms):
    if duration_ms is None:
        return "unknown"
    total_seconds = max(0, int(round(duration_ms / 1000)))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def is_ai_matching_enabled():
    return AI_MATCH_ENABLED and bool(AI_MATCH_MODEL) and bool(AI_MATCH_BASE_URL)


def extract_json_object(text):
    raw = clean(text)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.S)
    if fenced:
        try:
            parsed = json.loads(fenced.group(1))
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def fetch_lm_studio_models():
    req = urllib.request.Request(
        f"{AI_MATCH_BASE_URL}/models",
        headers={"Content-Type": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=min(AI_MATCH_TIMEOUT_SEC, 20)) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        return None, f"lm_studio_models_http_{err.code}:{clean(body) or err.reason}"
    except urllib.error.URLError as err:
        return None, f"lm_studio_models_connection_error:{err.reason}"
    except Exception as err:
        return None, f"lm_studio_models_error:{err}"

    try:
        payload = json.loads(raw)
    except Exception:
        return None, "lm_studio_models_invalid_response_json"

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None, "lm_studio_models_missing_data"

    ids = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = clean(item.get("id"))
        if model_id:
            ids.append(model_id)
    return ids, None


def resolve_lm_studio_model_id(requested_model):
    requested = clean(requested_model)
    if not requested:
        return None, "lm_studio_model_empty"

    model_ids, err = fetch_lm_studio_models()
    if err:
        return requested, err

    requested_lc = requested.lower()
    for model_id in model_ids:
        if model_id.lower() == requested_lc:
            return model_id, None

    requested_tail = requested_lc.split("/")[-1]
    for model_id in model_ids:
        model_lc = model_id.lower()
        if model_lc.split("/")[-1] == requested_tail or model_lc.endswith("/" + requested_tail):
            return model_id, None

    return requested, f"lm_studio_model_not_loaded:{requested}"


def ensure_ai_backend_ready():
    if not is_ai_matching_enabled():
        raise RuntimeError("AI matching is disabled in code.")

    resolved_model, err = resolve_lm_studio_model_id(AI_MATCH_MODEL)
    if err:
        raise RuntimeError(
            "LM Studio is not ready for AI matching. "
            f"base_url={AI_MATCH_BASE_URL} model={AI_MATCH_MODEL} error={err}"
        )
    return resolved_model


def run_lm_studio_json_prompt(prompt, model_name):
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": "Return a single JSON object and no extra text."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 220,
    }
    req = urllib.request.Request(
        f"{AI_MATCH_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=AI_MATCH_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        return None, f"lm_studio_http_{err.code}:{clean(body) or err.reason}"
    except urllib.error.URLError as err:
        return None, f"lm_studio_connection_error:{err.reason}"
    except Exception as err:
        return None, f"lm_studio_error:{err}"

    try:
        response_obj = json.loads(raw)
    except Exception:
        return None, "lm_studio_invalid_response_json"

    content = (
        (((response_obj.get("choices") or [{}])[0]).get("message") or {}).get("content")
        if isinstance(response_obj, dict)
        else None
    )
    parsed = extract_json_object(content or "")
    if not parsed:
        return None, "lm_studio_invalid_model_json"
    return parsed, None


def candidate_eval_summary(track, kind, cand):
    res = evaluate_candidate_check(kind, track, cand)
    res = second_phase_validate_check(res, kind)
    return {
        "kind": kind,
        "cand": cand,
        "eval": res,
    }


def is_perfect_metadata_match(eval_res):
    if not eval_res.get("strict_accepted"):
        return False
    scores = eval_res.get("scores") or {}
    title_sim = scores.get("title_sim") or 0.0
    artist_sim = scores.get("artist_sim") or 0.0
    channel_sim = scores.get("channel_sim") or 0.0
    duration_delta = scores.get("duration_delta_ms")
    return (
        title_sim >= PERFECT_MATCH_TITLE_SIM_MIN
        and duration_delta is not None
        and duration_delta <= PERFECT_MATCH_DURATION_DELTA_MS
        and (artist_sim >= PERFECT_MATCH_ARTIST_OR_CHANNEL_MIN or channel_sim >= PERFECT_MATCH_ARTIST_OR_CHANNEL_MIN)
    )


def artist_alias_payload(names):
    payload = []
    for raw_name in names or []:
        normalized = normalize_artist_text(raw_name)
        payload.append(
            {
                "raw": raw_name,
                "normalized": normalized,
                "aliases": sorted(artist_variants(normalized)) if normalized else [],
            }
        )
    return payload


def candidate_prompt_payload(item):
    cand = item["cand"]
    res = item["eval"]
    scores = res.get("scores") or {}
    return {
        "kind": item["kind"],
        "rank": cand.get("rank"),
        "videoId": cand.get("videoId"),
        "title": cand.get("title"),
        "title_clean": cand.get("title_clean"),
        "artists": cand.get("artists") or [],
        "artists_clean": cand.get("artists_clean") or [],
        "artist_aliases": artist_alias_payload(cand.get("artists") or []),
        "channel": cand.get("channel"),
        "channel_clean": cand.get("channel_clean"),
        "duration": format_duration_ms(cand.get("duration_ms")),
        "duration_ms": cand.get("duration_ms"),
        "url": cand.get("url"),
        "algorithm": {
            "score": scores.get("score"),
            "title_sim": scores.get("title_sim"),
            "artist_sim": scores.get("artist_sim"),
            "channel_sim": scores.get("channel_sim"),
            "duration_delta_ms": scores.get("duration_delta_ms"),
            "artist_identity_match": (scores.get("artist_sim") or 0.0) >= 0.95 or (scores.get("channel_sim") or 0.0) >= 0.95,
            "strict_accepted": res.get("strict_accepted"),
            "official_like": res.get("official_like"),
            "failed": res.get("reasons_failed") or [],
            "passed": res.get("reasons_passed") or [],
        },
    }


def build_ai_match_prompt(track, prompt_candidates):
    spotify_payload = {
        "title": track.get("title"),
        "title_clean": track.get("title_clean"),
        "artists": track.get("artists") or [],
        "artists_clean": track.get("artists_clean") or [],
        "artist_aliases": artist_alias_payload(track.get("artists") or []),
        "album": track.get("album"),
        "duration_ms": track.get("duration_ms"),
        "spotify_has_version": bool(track.get("spotify_has_version")),
    }

    instructions = {
        "task": "Pick the single candidate that is the same original recording as the Spotify track. If no candidate is a clear match, return none.",
        "rules": [
            "Match the same original recording, not just a similar song name.",
            "Reject live, remix, cover, instrumental, karaoke, sped up, slowed, reverb, edit, teaser, clip, compilation, or alternate versions unless the Spotify track itself clearly indicates that version.",
            "If both a song candidate and a video candidate match the same recording, choose the song candidate.",
            "Use title, artist identity, channel, and duration together.",
            "Do not reject a candidate only because artist text appears in a different script, transliteration, or spelling variant.",
            "Treat artist identity as matched when normalized artist/channel evidence is strong, for example Tuna and טונה.",
            "Spotify metadata can be imperfect or inconsistent. If the Spotify title itself explicitly contains a performer name or artist-like prefix, treat that as strong evidence about artist identity.",
            "If the candidate title, channel, or artist matches the performer name explicitly written inside the Spotify title, treat that as meaningful positive evidence even when the Spotify artists field is incomplete, generic, collective, or different.",
            "If title and duration are very strong and the candidate matches the performer explicitly named inside the Spotify title, do not return none just because the Spotify artists field differs.",
            "If a candidate has very strong agreement on title, artist identity, and duration, do not return none.",
            "Return none only when the candidates are genuinely conflicting, weak, or ambiguous.",
            "Return only one JSON object."
        ],
        "response_schema": {
            "decision": "match|none",
            "videoId": "chosen candidate videoId or null",
            "confidence": "high|medium|low",
            "reason_code": "exact_match|artist_title_duration_match|spotify_metadata_inconsistent|wrong_version|wrong_artist|wrong_title|duration_mismatch|uncertain",
            "reason": "short explanation"
        }
    }

    return "\n".join([
        "You are validating Spotify to YouTube Music track matches.",
        json.dumps(instructions, ensure_ascii=False),
        "Spotify track:",
        json.dumps(spotify_payload, ensure_ascii=False),
        "Candidates:",
        json.dumps(prompt_candidates, ensure_ascii=False),
        'Return exactly one JSON object, for example: {"decision":"match","videoId":"abc123","confidence":"high","reason_code":"artist_title_duration_match","reason":"same title, same artist, compatible duration"}'
    ])


def ai_selected_candidate_is_safe(track, kind, cand, eval_res, ai_confidence):
    scores = eval_res.get("scores") or {}
    failed = set(eval_res.get("reasons_failed") or [])
    duration_delta = scores.get("duration_delta_ms")
    title_sim = scores.get("title_sim") or 0.0
    algo_score = scores.get("score") or 0.0
    short_title = len(tokenize_simple(track.get("title_clean") or "")) <= 2

    if "duration_hard_fail" in failed or "version_mismatch" in failed:
        return False, "ai_rejected_by_hard_guardrail"
    if duration_delta is not None and duration_delta > AI_MATCH_MAX_DURATION_DELTA_MS:
        return False, "ai_rejected_by_duration_guardrail"

    min_title = 0.60 if kind == "song" else 0.64
    if short_title:
        min_title = max(min_title, 0.72)
    if title_sim < min_title:
        return False, "ai_rejected_by_title_guardrail"

    if ai_confidence == "high":
        return True, "ai_guardrails_passed_high_confidence"
    if ai_confidence == "medium" and algo_score >= 0.50:
        return True, "ai_guardrails_passed_medium_confidence"
    return False, "ai_rejected_by_confidence_guardrail"


def best_safe_song_candidate(track, pool):
    best = None
    for item in pool:
        if item.get("kind") != "song":
            continue
        cand = item.get("cand") or {}
        eval_res = item.get("eval") or {}
        safe, _ = ai_selected_candidate_is_safe(track, "song", cand, eval_res, "high")
        if not safe:
            continue
        score = (eval_res.get("scores") or {}).get("score") or 0.0
        title_sim = (eval_res.get("scores") or {}).get("title_sim") or 0.0
        if title_sim < AI_PREFER_SONG_TITLE_SIM_MIN:
            continue
        if best is None or score > ((best.get("eval") or {}).get("scores") or {}).get("score", 0.0):
            best = item
    return best


def maybe_prefer_song_over_video(track, chosen, pool):
    if not chosen or chosen.get("kind") != "video":
        return chosen, None

    chosen_score = ((chosen.get("eval") or {}).get("scores") or {}).get("score") or 0.0
    best_song = best_safe_song_candidate(track, pool)
    if not best_song:
        return chosen, None

    song_score = ((best_song.get("eval") or {}).get("scores") or {}).get("score") or 0.0
    if song_score + AI_PREFER_SONG_SCORE_GAP_MAX >= chosen_score:
        return best_song, "ai_video_overridden_by_song_preference"
    return chosen, None


def is_obvious_no_match_eval(eval_res):
    if not eval_res or not eval_res.get("exists"):
        return True

    scores = eval_res.get("scores") or {}
    failed = set(eval_res.get("reasons_failed") or [])
    title_sim = scores.get("title_sim") or 0.0
    artist_sim = scores.get("artist_sim") or 0.0
    channel_sim = scores.get("channel_sim") or 0.0
    score = scores.get("score") or 0.0
    duration_delta = scores.get("duration_delta_ms")

    if eval_res.get("strict_accepted"):
        return False

    very_far_duration = (
        "duration_hard_fail" in failed
        or (duration_delta is not None and duration_delta >= OBVIOUS_NO_MATCH_DURATION_MS)
    )
    weak_title = title_sim <= OBVIOUS_NO_MATCH_TITLE_MAX
    weak_identity = max(artist_sim, channel_sim) <= OBVIOUS_NO_MATCH_ARTIST_OR_CHANNEL_MAX
    very_low_score = score <= OBVIOUS_NO_MATCH_SCORE_MAX

    return (very_far_duration and very_low_score) or (weak_title and weak_identity and very_low_score)


def should_skip_ai_as_obvious_no_match(candidate_evals):
    existing = [res for res in (candidate_evals or []) if res and res.get("exists")]
    if not existing:
        return True
    return all(is_obvious_no_match_eval(res) for res in existing)


def collect_ai_candidates(yt, track, queries):
    pool = []
    seen = set()
    first_snapshot = {"song_first": None, "video_first": None}
    ai_queries = build_ai_queries(track, queries)

    for query_idx, query in enumerate(ai_queries, start=1):
        song_raw_results = search_results(yt, query, "songs", AI_MATCH_TOP_RESULTS)
        video_raw_results = search_results(yt, query, "videos", AI_MATCH_TOP_RESULTS)

        song_candidates = [extract_candidate(r, "song", rank=i) for i, r in enumerate(song_raw_results, start=1)]
        video_candidates = [extract_candidate(r, "video", rank=i) for i, r in enumerate(video_raw_results, start=1)]

        if query_idx == 1:
            first_snapshot = {
                "song_first": candidate_public_payload(song_candidates[0]) if song_candidates else None,
                "song_second": candidate_public_payload(song_candidates[1]) if len(song_candidates) > 1 else None,
                "song_third": candidate_public_payload(song_candidates[2]) if len(song_candidates) > 2 else None,
                "video_first": candidate_public_payload(video_candidates[0]) if video_candidates else None,
                "video_second": candidate_public_payload(video_candidates[1]) if len(video_candidates) > 1 else None,
                "video_third": candidate_public_payload(video_candidates[2]) if len(video_candidates) > 2 else None,
            }

        for kind, candidates in (("song", song_candidates), ("video", video_candidates)):
            for cand in candidates:
                if not cand:
                    continue
                key = (kind, cand.get("videoId"))
                if key in seen:
                    continue
                seen.add(key)
                item = candidate_eval_summary(track, kind, cand)
                item["query"] = query
                pool.append(item)

    pool.sort(
        key=lambda item: (
            item["eval"].get("strict_accepted"),
            item["eval"].get("scores", {}).get("score") or 0.0,
            -((item["cand"] or {}).get("rank") or 999),
        ),
        reverse=True,
    )
    return pool[:AI_MATCH_POOL_SIZE], first_snapshot, ai_queries


def try_ai_match(yt, track, queries):
    if not is_ai_matching_enabled():
        return None, "ai_disabled"

    resolved_model, model_err = resolve_lm_studio_model_id(AI_MATCH_MODEL)
    if model_err and model_err.startswith("lm_studio_model_not_loaded:"):
        return None, model_err

    pool, first_snapshot, ai_queries = collect_ai_candidates(yt, track, queries)
    if not pool:
        return None, "ai_no_candidates"

    prompt_candidates = [candidate_prompt_payload(item) for item in pool]
    parsed, err = run_lm_studio_json_prompt(build_ai_match_prompt(track, prompt_candidates), resolved_model)
    if err:
        return None, err

    decision_value = clean(parsed.get("decision")).lower()
    choice = normalize_choise(parsed.get("choice"))
    rank = parse_int(parsed.get("rank"))
    video_id = clean(parsed.get("videoId"))
    ai_confidence = clean(parsed.get("confidence")).lower()
    if ai_confidence not in ("high", "medium", "low"):
        ai_confidence = "low"

    chosen = None
    if decision_value == "none":
        choice = "none"
    elif video_id:
        for item in pool:
            cand = item["cand"]
            if cand.get("videoId") == video_id:
                chosen = item
                choice = item["kind"]
                break

    if not chosen and choice in ("song", "video"):
        for item in pool:
            cand = item["cand"]
            if item["kind"] != choice:
                continue
            if video_id and cand.get("videoId") == video_id:
                chosen = item
                break
            if not video_id and rank is not None and cand.get("rank") == rank:
                chosen = item
                break

    chosen, prefer_song_reason = maybe_prefer_song_over_video(track, chosen, pool)

    search_payload = {
        "query_used": queries[0] if queries else "",
        "stage_checked": ["songs", "videos", "ai_rescue"],
        "selected_stage": "songs" if choice == "song" else ("videos" if choice == "video" else "none"),
        "selected_rank": rank if chosen else None,
        "choise": choice if chosen else "none",
        "candidates": {
            "song_first": first_snapshot.get("song_first"),
            "video_first": first_snapshot.get("video_first"),
            "ai_pool": prompt_candidates,
        },
        "ai": {
            "model": resolved_model,
            "base_url": AI_MATCH_BASE_URL,
            "queries_used": ai_queries,
            "model_resolve_warning": model_err,
            "confidence": ai_confidence,
            "videoId": video_id or None,
            "song_preference_override": prefer_song_reason,
            "reason": clean(parsed.get("reason")),
        },
    }

    if chosen:
        search_payload["selected_stage"] = "songs" if chosen["kind"] == "song" else "videos"
        search_payload["selected_rank"] = chosen["cand"].get("rank")
        search_payload["choise"] = chosen["kind"]

    if not chosen:
        return {
            "search": search_payload,
            "youtube": youtube_payload_from_candidate(None),
            "decision": {
                "status": "no_match",
                "reasons_passed": [],
                "reasons_failed": ["ai_no_selection"],
                "notes": "local_ai_returned_none_or_invalid_selection",
            },
            "scores": {
                "title_sim": None,
                "artist_sim": None,
                "channel_sim": None,
                "duration_delta_ms": None,
                "score": None,
            },
        }, None

    safe, safe_reason = ai_selected_candidate_is_safe(track, chosen["kind"], chosen["cand"], chosen["eval"], ai_confidence)
    if not safe:
        return {
            "search": search_payload,
            "youtube": youtube_payload_from_candidate(None),
            "decision": {
                "status": "no_match",
                "reasons_passed": chosen["eval"].get("reasons_passed") or [],
                "reasons_failed": (chosen["eval"].get("reasons_failed") or []) + [safe_reason],
                "notes": "local_ai_selection_rejected_by_guardrails",
            },
            "scores": chosen["eval"].get("scores") or {},
        }, None

    return {
        "search": search_payload,
        "youtube": youtube_payload_from_candidate(chosen["cand"], forced_type=chosen["kind"]),
        "decision": {
            "status": "matched",
            "reasons_passed": (chosen["eval"].get("reasons_passed") or []) + ([prefer_song_reason] if prefer_song_reason else []) + [safe_reason],
            "reasons_failed": chosen["eval"].get("reasons_failed") or [],
            "notes": "matched by local_ai_rescue",
        },
        "scores": chosen["eval"].get("scores") or {},
    }, None


def normalize_choise(value):
    if isinstance(value, str):
        v = clean(value).lower()
    else:
        v = clean(str(value or "")).lower()
    if v in ("song", "songs"):
        return "song"
    if v in ("video", "videos"):
        return "video"
    return "none"


def calc_title_similarity(track, yt_title_clean):
    def one_side_sim(base, other):
        if not base or not other:
            return 0.0
        seq = ratio(base, other)
        cov = token_overlap(base, other)

        # Weaken "containment" so short/generic titles don't get an inflated score
        # just because they appear as a substring.
        bt = [t for t in normalize_basic_text(base).split() if t]
        ot = [t for t in normalize_basic_text(other).split() if t]
        token_ratio = min(len(bt), len(ot)) / max(1, max(len(bt), len(ot)))
        containment = (base in other) or (other in base)
        containment_bonus = 0.0
        if containment and (token_ratio >= 0.7 or cov >= 0.8):
            containment_bonus = 0.08 if seq < 0.90 else 0.03

        base_sim = max(seq, cov * 0.98)
        return max(0.0, min(1.0, base_sim + containment_bonus))

    sims = [one_side_sim(track["title_clean"], yt_title_clean)] if track["title_clean"] else [0.0]
    if track["title_alt"]:
        sims.append(one_side_sim(track["title_alt"], yt_title_clean))
    return max(sims) if sims else 0.0


def tokenize_simple(text):
    return [t for t in clean(text).split() if t]


def coverage_ratio_simple(source_text, target_text):
    src = tokenize_simple(source_text)
    tgt = set(tokenize_simple(target_text))
    if not src:
        return 0.0
    inter = sum(1 for tok in src if tok in tgt)
    return inter / max(1, len(src))


def compute_title_metrics_check(track, yt_title_clean):
    variants = [track["title_clean"]]
    if track.get("title_alt"):
        variants.append(track["title_alt"])

    best = None
    for sp_title in variants:
        if not sp_title:
            continue
        seq = ratio(sp_title, yt_title_clean)
        tok_cov_sp = coverage_ratio_simple(sp_title, yt_title_clean)
        tok_cov_yt = coverage_ratio_simple(yt_title_clean, sp_title)

        sp_tokens = tokenize_simple(sp_title)
        yt_tokens = tokenize_simple(yt_title_clean)
        token_ratio = min(len(sp_tokens), len(yt_tokens)) / max(1, max(len(sp_tokens), len(yt_tokens)))

        containment = (sp_title in yt_title_clean) or (yt_title_clean in sp_title)
        containment_bonus = 0.0
        if containment:
            if token_ratio >= 0.7 or tok_cov_sp >= 0.8:
                containment_bonus = 0.08 if seq < 0.90 else 0.03

        base = max(seq, tok_cov_sp * 0.95)
        title_sim = max(0.0, min(1.0, base + containment_bonus))
        containment_led = containment and ((title_sim - base) >= 0.05)

        cur = {
            "title_sim": title_sim,
            "seq": seq,
            "token_cov_sp": tok_cov_sp,
            "token_cov_yt": tok_cov_yt,
            "token_ratio": token_ratio,
            "containment": containment,
            "containment_bonus": containment_bonus,
            "containment_led": containment_led,
            "base_variant": sp_title,
        }
        if best is None or cur["title_sim"] > best["title_sim"]:
            best = cur

    if not best:
        return {
            "title_sim": 0.0,
            "seq": 0.0,
            "token_cov_sp": 0.0,
            "token_cov_yt": 0.0,
            "token_ratio": 0.0,
            "containment": False,
            "containment_bonus": 0.0,
            "containment_led": False,
            "base_variant": "",
        }
    return best


def is_mostly_numeric_check(title_clean):
    toks = tokenize_simple(title_clean)
    if not toks:
        return False
    numeric = 0
    for t in toks:
        if re.fullmatch(r"[0-9]+", t):
            numeric += 1
    return (numeric / max(1, len(toks))) >= 0.6


def detect_risk_mode_check(track):
    title_clean = track["title_clean"]
    short_or_generic = len(tokenize_simple(title_clean)) <= 1
    mostly_num = is_mostly_numeric_check(title_clean)
    no_clear_artist = not track.get("artists_clean") or not any(track.get("artists_clean") or [])
    risk = short_or_generic or mostly_num or no_clear_artist
    reasons = []
    if short_or_generic:
        reasons.append("risk_short_or_generic_title")
    if mostly_num:
        reasons.append("risk_numeric_title")
    if no_clear_artist:
        reasons.append("risk_no_clear_artist")
    return risk, reasons


def rank_bonus_check(rank):
    r = int(rank or 1)
    return max(0.0, 0.035 - (r - 1) * 0.008)


def duration_assessment_check(kind, track_duration_ms, cand_duration_ms, *, title_sim, channel_sim, official_like, short_title, has_version_marker):
    if track_duration_ms is None:
        return {
            "delta_ms": None,
            "duration_ok": False,
            "very_close": False,
            "hard_fail": False,
            "status": "duration_unknown_track",
            "adjust": 0.0,
        }
    if cand_duration_ms is None:
        return {
            "delta_ms": None,
            "duration_ok": False,
            "very_close": False,
            "hard_fail": True,
            "status": "duration_missing_candidate",
            "adjust": -0.06 if kind == "video" else -0.12,
        }

    delta = abs(int(track_duration_ms) - int(cand_duration_ms))
    very_close = delta <= VERY_CLOSE_DURATION_MS

    if kind == "song":
        if delta > SONG_MAX_DELTA_MS:
            return {
                "delta_ms": delta,
                "duration_ok": False,
                "very_close": very_close,
                "hard_fail": True,
                "status": "duration_too_far_song",
                "adjust": -0.50,
            }
        return {
            "delta_ms": delta,
            "duration_ok": True,
            "very_close": very_close,
            "hard_fail": False,
            "status": "duration_very_close" if very_close else "duration_within_threshold",
            "adjust": 0.20 if very_close else 0.10,
        }

    max_allowed = VIDEO_BASE_MAX_DELTA_MS
    if short_title:
        max_allowed = min(max_allowed, VIDEO_BASE_MAX_DELTA_MS)

    allow_extension = (
        delta > VIDEO_BASE_MAX_DELTA_MS
        and delta <= VIDEO_EXT_MAX_DELTA_MS
        and (title_sim >= VIDEO_EXT_TITLE_SIM_MIN)
        and (official_like or channel_sim >= VIDEO_EXT_CHANNEL_SIM_MIN)
        and (not has_version_marker)
        and (not short_title)
    )
    if allow_extension:
        max_allowed = VIDEO_EXT_MAX_DELTA_MS

    if delta > max_allowed:
        return {
            "delta_ms": delta,
            "duration_ok": False,
            "very_close": very_close,
            "hard_fail": True,
            "status": "duration_too_far_video",
            "adjust": -0.50,
        }
    if very_close:
        return {
            "delta_ms": delta,
            "duration_ok": True,
            "very_close": True,
            "hard_fail": False,
            "status": "duration_very_close",
            "adjust": 0.20,
        }
    if delta <= VIDEO_BASE_MAX_DELTA_MS:
        return {
            "delta_ms": delta,
            "duration_ok": True,
            "very_close": False,
            "hard_fail": False,
            "status": "duration_within_threshold",
            "adjust": 0.08,
        }
    return {
        "delta_ms": delta,
        "duration_ok": True,
        "very_close": False,
        "hard_fail": False,
        "status": "duration_within_extended",
        "adjust": -0.06,
    }


def evaluate_candidate_check(kind, track, cand):
    if not cand or not cand.get("videoId"):
        return {
            "exists": False,
            "kind": kind,
            "cand": None,
            "strict_accepted": False,
            "gate_passed": False,
            "risk_mode": False,
            "risk_reasons": [],
            "reasons_passed": [],
            "reasons_failed": ["no_candidate"],
            "title_metrics": {},
            "official_like": False,
            "duration_policy": None,
            "scores": {
                "title_sim": None,
                "artist_sim": None,
                "channel_sim": None,
                "duration_delta_ms": None,
                "score": 0.0,
            },
        }

    title_metrics = compute_title_metrics_check(track, cand["title_clean"])
    title_sim = title_metrics["title_sim"]

    artist_sim = calc_artist_similarity(track["artists_clean"], cand["artists_clean"], cand["title_clean"], cand["channel_clean"])
    channel_sim = calc_channel_similarity(track["artists_clean"], cand["channel_clean"])
    official_like = is_official_or_topic(cand["title_clean"], cand["channel_clean"])

    short_title = len(tokenize_simple(track["title_clean"])) <= 2
    risk_mode, risk_reasons = detect_risk_mode_check(track)

    dur = duration_assessment_check(
        kind,
        track.get("duration_ms"),
        cand.get("duration_ms"),
        title_sim=title_sim,
        channel_sim=channel_sim,
        official_like=official_like,
        short_title=short_title,
        has_version_marker=bool(cand.get("has_version_marker")),
    )

    reasons_passed = []
    reasons_failed = []
    if dur["status"]:
        reasons_passed.append(dur["status"])
    if dur["hard_fail"]:
        reasons_failed.append("duration_hard_fail")

    if cand.get("has_version_marker") and not track.get("spotify_has_version"):
        reasons_failed.append("version_mismatch")
    else:
        reasons_passed.append("version_ok")

    title_min = CHECK_SONG_TITLE_MIN if kind == "song" else CHECK_VIDEO_TITLE_MIN
    if risk_mode:
        title_min += 0.04
    if title_sim >= title_min:
        reasons_passed.append("strong_title_match")
    else:
        reasons_failed.append("weak_title_match")

    if kind == "song":
        support = (artist_sim >= CHECK_ARTIST_STRONG_SONG) or (channel_sim >= CHECK_CHANNEL_STRONG) or official_like
    else:
        title_artist_overlap = token_overlap(" ".join(track["artists_clean"]), cand["title_clean"])
        support = (
            artist_sim >= CHECK_ARTIST_STRONG_VIDEO
            or channel_sim >= CHECK_CHANNEL_MIN_VIDEO
            or official_like
            or title_artist_overlap >= 0.35
        )

    title_duration_lock = (dur["very_close"] and title_sim >= 0.72) or (dur["duration_ok"] and title_sim >= 0.88)
    title_duration_coverage_relax = (
        title_duration_lock
        and (title_metrics.get("token_cov_sp", 0.0) >= CHECK_TITLE_DURATION_COVERAGE_RELAX)
        and (not is_mostly_numeric_check(track["title_clean"]))
        and (len(tokenize_simple(track["title_clean"])) >= 3)
    )

    if support:
        reasons_passed.append("artist_or_channel_support")
    elif title_duration_coverage_relax:
        reasons_passed.append("support_relaxed_by_title_duration_coverage")
        support = True
    elif title_duration_lock:
        reasons_passed.append("support_relaxed_by_title_and_duration")
        support = True
    else:
        reasons_failed.append("artist_channel_support_weak")

    if kind == "video" and short_title:
        if not (official_like or channel_sim >= CHECK_CHANNEL_STRONG or artist_sim >= CHECK_ARTIST_STRONG_VIDEO):
            reasons_failed.append("short_title_requires_strong_channel_or_artist")

    if risk_mode and not (support or title_duration_coverage_relax or (title_duration_lock and channel_sim >= CHECK_CHANNEL_MIN_VIDEO)):
        reasons_failed.append("risk_mode_requires_artist_or_channel")

    if kind == "song":
        score = (
            0.63 * title_sim
            + 0.22 * artist_sim
            + 0.10 * channel_sim
            + dur["adjust"]
            + rank_bonus_check(cand.get("rank"))
        )
    else:
        score = (
            0.60 * title_sim
            + 0.18 * artist_sim
            + 0.18 * channel_sim
            + dur["adjust"]
            + rank_bonus_check(cand.get("rank"))
        )

    if risk_mode:
        score -= 0.03
    if title_metrics.get("containment_led") and not support:
        score -= 0.08

    score = max(0.0, min(1.0, score))
    score_min = CHECK_SONG_SCORE_MIN if kind == "song" else CHECK_VIDEO_SCORE_MIN
    if score < score_min:
        reasons_failed.append("score_too_low")

    gate_failed = any(
        r
        in (
            "duration_hard_fail",
            "version_mismatch",
            "weak_title_match",
            "artist_channel_support_weak",
            "risk_mode_requires_artist_or_channel",
            "short_title_requires_strong_channel_or_artist",
        )
        for r in reasons_failed
    )
    gate_passed = not gate_failed
    strict_accepted = len(reasons_failed) == 0
    return {
        "exists": True,
        "kind": kind,
        "cand": cand,
        "gate_passed": gate_passed,
        "strict_accepted": strict_accepted,
        "risk_mode": risk_mode,
        "risk_reasons": risk_reasons,
        "reasons_passed": reasons_passed,
        "reasons_failed": reasons_failed,
        "title_metrics": title_metrics,
        "official_like": official_like,
        "duration_policy": dur["status"],
        "scores": {
            "title_sim": round(title_sim, 4),
            "artist_sim": round(artist_sim, 4),
            "channel_sim": round(channel_sim, 4),
            "duration_delta_ms": dur["delta_ms"],
            "score": round(score, 4),
        },
    }


def second_phase_validate_check(res, kind):
    if not res.get("exists") or not res.get("strict_accepted"):
        res["phase2"] = "skipped_not_passed"
        return res

    score = res["scores"].get("score") or 0.0
    ambiguous = (score < CHECK_AMBIGUOUS_SCORE_CUTOFF) or (res.get("risk_mode") and score < 0.80)
    if not ambiguous:
        res["phase2"] = "skipped_strong"
        return res

    title_sim = res["scores"].get("title_sim") or 0.0
    artist_sim = res["scores"].get("artist_sim") or 0.0
    channel_sim = res["scores"].get("channel_sim") or 0.0
    delta = res["scores"].get("duration_delta_ms")

    strong = 0
    if title_sim >= 0.82:
        strong += 1
    if delta is not None and delta <= VERY_CLOSE_DURATION_MS:
        strong += 1
    if artist_sim >= (CHECK_ARTIST_STRONG_SONG if kind == "song" else CHECK_ARTIST_STRONG_VIDEO):
        strong += 1
    if channel_sim >= CHECK_CHANNEL_STRONG or res.get("official_like"):
        strong += 1

    if res.get("title_metrics", {}).get("containment_led") and title_sim < 0.75:
        res["strict_accepted"] = False
        res["reasons_failed"].append("phase2_containment_ambiguous")
        res["phase2"] = "failed"
        return res

    if strong < 2:
        res["strict_accepted"] = False
        res["reasons_failed"].append("phase2_not_enough_signals")
        res["phase2"] = "failed"
    else:
        res["reasons_passed"].append("phase2_confirmed")
        res["phase2"] = "passed"
    return res


def evaluate_song_gate(track, cand):
    res = evaluate_candidate_check("song", track, cand)
    res = second_phase_validate_check(res, "song")
    return res.get("strict_accepted"), res.get("reasons_passed", []), res.get("reasons_failed", []), res.get("scores", {})


def evaluate_video_gate(track, cand):
    res = evaluate_candidate_check("video", track, cand)
    res = second_phase_validate_check(res, "video")
    return res.get("strict_accepted"), res.get("reasons_passed", []), res.get("reasons_failed", []), res.get("scores", {})


def search_results(yt, query, filter_name, limit):
    results = yt.search(query, filter=filter_name, limit=limit) or []
    return [r for r in results if r.get("videoId")]


def best_accepted_from_candidates(track, candidates, gate_fn):
    best = None
    for cand in candidates:
        passed, reasons_passed, reasons_failed, scores = gate_fn(track, cand)
        if not passed:
            continue
        if best is None or scores["score"] > best["scores"]["score"]:
            best = {
                "cand": cand,
                "reasons_passed": reasons_passed,
                "reasons_failed": reasons_failed,
                "scores": scores,
            }
    return best


def match_track(yt, track):
    queries = build_queries(track)
    stage_checked = []
    collected_failed = []
    all_candidate_evals = []
    query_used = queries[0] if queries else ""
    last_candidates = {"song_first": None, "video_first": None}

    for query in queries:
        query_used = query

        stage_checked.append("songs")
        stage_checked.append("videos")

        # Evaluate only the top 3 song results and top 3 video results.
        song_raw_results = search_results(yt, query, "songs", TOP_RESULTS)
        video_raw_results = search_results(yt, query, "videos", TOP_RESULTS)

        song_candidates = [extract_candidate(r, "song", rank=i) for i, r in enumerate(song_raw_results, start=1)]
        video_candidates = [extract_candidate(r, "video", rank=i) for i, r in enumerate(video_raw_results, start=1)]

        last_candidates = {
            "song_first": candidate_public_payload(song_candidates[0]) if song_candidates else None,
            "song_second": candidate_public_payload(song_candidates[1]) if len(song_candidates) > 1 else None,
            "song_third": candidate_public_payload(song_candidates[2]) if len(song_candidates) > 2 else None,
            "video_first": candidate_public_payload(video_candidates[0]) if video_candidates else None,
            "video_second": candidate_public_payload(video_candidates[1]) if len(video_candidates) > 1 else None,
            "video_third": candidate_public_payload(video_candidates[2]) if len(video_candidates) > 2 else None,
        }

        song_res = None
        song_cand = None
        if song_candidates:
            for cand in song_candidates:
                cur_eval = candidate_eval_summary(track, "song", cand)
                cur_res = cur_eval["eval"]
                all_candidate_evals.append(cur_res)
                if song_res is None or (cur_res.get("scores", {}).get("score") or 0.0) > (song_res.get("scores", {}).get("score") or 0.0):
                    song_res = cur_res
                    song_cand = cand
                if cur_res.get("strict_accepted") and is_perfect_metadata_match(cur_res):
                    song_res = cur_res
                    song_cand = cand
                    break

            if song_res.get("strict_accepted") and is_perfect_metadata_match(song_res):
                return {
                    "search": {
                        "query_used": query,
                        "stage_checked": stage_checked.copy(),
                        "selected_stage": "songs",
                        "selected_rank": song_cand.get("rank"),
                        "choise": "song",
                        "candidates": last_candidates,
                    },
                    "youtube": youtube_payload_from_candidate(song_cand, forced_type="song"),
                    "decision": {
                        "status": "matched",
                        "reasons_passed": song_res.get("reasons_passed", []),
                        "reasons_failed": song_res.get("reasons_failed", []),
                        "notes": "matched in songs stage (perfect_metadata_lock)",
                    },
                    "scores": song_res.get("scores", {}),
                }
            if song_res.get("strict_accepted"):
                collected_failed.append("songs:accepted_but_not_perfect_requires_ai")
            else:
                collected_failed.extend([f"songs:{x}" for x in (song_res.get("reasons_failed") or [])])
        else:
            collected_failed.append("songs:no_result")

        video_res = None
        video_cand = None
        if video_candidates:
            for cand in video_candidates:
                cur_eval = candidate_eval_summary(track, "video", cand)
                cur_res = cur_eval["eval"]
                all_candidate_evals.append(cur_res)
                if video_res is None or (cur_res.get("scores", {}).get("score") or 0.0) > (video_res.get("scores", {}).get("score") or 0.0):
                    video_res = cur_res
                    video_cand = cand
                if cur_res.get("strict_accepted") and is_perfect_metadata_match(cur_res):
                    video_res = cur_res
                    video_cand = cand
                    break

            song_blocks_auto_video = bool(song_res and song_res.get("strict_accepted"))
            if (
                video_res.get("strict_accepted")
                and is_perfect_metadata_match(video_res)
                and not song_blocks_auto_video
            ):
                return {
                    "search": {
                        "query_used": query,
                        "stage_checked": stage_checked.copy(),
                        "selected_stage": "videos",
                        "selected_rank": video_cand.get("rank"),
                        "choise": "video",
                        "candidates": last_candidates,
                    },
                    "youtube": youtube_payload_from_candidate(video_cand, forced_type="video"),
                    "decision": {
                        "status": "matched",
                        "reasons_passed": video_res.get("reasons_passed", []),
                        "reasons_failed": video_res.get("reasons_failed", []),
                        "notes": "matched in videos stage (perfect_metadata_lock)",
                    },
                    "scores": video_res.get("scores", {}),
                }
            if video_res.get("strict_accepted") and song_blocks_auto_video:
                collected_failed.append("videos:perfect_but_song_candidate_exists_requires_ai")
            if video_res.get("strict_accepted"):
                collected_failed.append("videos:accepted_but_not_perfect_requires_ai")
            else:
                collected_failed.extend([f"videos:{x}" for x in (video_res.get("reasons_failed") or [])])
        else:
            collected_failed.append("videos:no_result")

    if should_skip_ai_as_obvious_no_match(all_candidate_evals):
        collected_failed.append("obvious_no_match_skip_ai")
        return {
            "search": {
                "query_used": query_used,
                "stage_checked": stage_checked,
                "selected_stage": "none",
                "selected_rank": None,
                "choise": "none",
                "candidates": last_candidates,
            },
            "youtube": youtube_payload_from_candidate(None),
            "decision": {
                "status": "no_match",
                "reasons_passed": [],
                "reasons_failed": sorted(set(collected_failed)),
                "notes": "obvious_no_match_without_ai",
            },
            "scores": {
                "title_sim": None,
                "artist_sim": None,
                "channel_sim": None,
                "duration_delta_ms": None,
            },
        }

    ai_result, ai_err = try_ai_match(yt, track, queries)
    if ai_result:
        ai_result["decision"]["reasons_failed"] = sorted(
            set((ai_result["decision"].get("reasons_failed") or []) + collected_failed)
        )
        return ai_result
    if ai_err and ai_err != "ai_disabled":
        stage_checked.append("ai_rescue")
        collected_failed.append(ai_err)

    # No stage passed gate.
    return {
        "search": {
            "query_used": query_used,
            "stage_checked": stage_checked,
            "selected_stage": "none",
            "selected_rank": None,
            "choise": "none",
            "candidates": last_candidates,
        },
        "youtube": youtube_payload_from_candidate(None),
        "decision": {
            "status": "no_match",
            "reasons_passed": [],
            "reasons_failed": sorted(set(collected_failed)),
            "notes": "no candidate passed songs/videos gate",
        },
        "scores": {
            "title_sim": None,
            "artist_sim": None,
            "channel_sim": None,
            "duration_delta_ms": None,
        },
    }


def is_http_409_conflict(err):
    msg = str(err)
    return "HTTP 409" in msg or "409: Conflict" in msg


def is_transient_add_error(err):
    msg = str(err).lower()
    markers = [
        "http 409", "http 429", "http 500", "http 502", "http 503", "http 504",
        "timeout", "timed out", "temporarily unavailable", "connection reset",
    ]
    return any(m in msg for m in markers)


def add_to_playlist_with_retry(yt, playlist_id, video_id):
    last_err = None
    for attempt in range(1, ADD_RETRIES + 1):
        try:
            yt.add_playlist_items(playlist_id, [video_id])
            return
        except Exception as err:
            last_err = err
            if attempt >= ADD_RETRIES or not is_transient_add_error(err):
                break
            time.sleep(ADD_RETRY_SLEEP * attempt)
    raise RuntimeError(f"failed_to_add_video_id={video_id}; error={last_err}")


def remove_all_from_playlist(yt, playlist_id):
    removed = 0
    max_rounds = 8

    for round_idx in range(1, max_rounds + 1):
        pl = yt.get_playlist(playlist_id, limit=None)
        tracks = pl.get("tracks", []) or []
        payload = []
        for t in tracks:
            vid = t.get("videoId")
            svid = t.get("setVideoId")
            if vid and svid:
                payload.append({"videoId": vid, "setVideoId": svid})

        if not payload:
            return removed

        progress_this_round = 0
        print(f"Delete round {round_idx}: trying to remove {len(payload)} items...")

        for i in range(0, len(payload), REMOVE_BATCH):
            batch = payload[i : i + REMOVE_BATCH]
            try:
                yt.remove_playlist_items(playlist_id, batch)
                removed += len(batch)
                progress_this_round += len(batch)
            except Exception as batch_err:
                if not is_http_409_conflict(batch_err):
                    raise
                for item in batch:
                    try:
                        yt.remove_playlist_items(playlist_id, [item])
                        removed += 1
                        progress_this_round += 1
                    except Exception as item_err:
                        if not is_http_409_conflict(item_err):
                            raise
            time.sleep(SLEEP_BETWEEN_REQUESTS)

        if progress_this_round == 0:
            raise RuntimeError("Failed to remove playlist items due to repeated HTTP 409 conflicts.")

    raise RuntimeError("Reached max deletion rounds; playlist may still contain items.")


def dict_candidate_to_internal(raw_cand, forced_type):
    if not isinstance(raw_cand, dict):
        return None
    video_id = clean(raw_cand.get("videoId"))
    if not video_id:
        return None
    title = clean(raw_cand.get("title"))
    channel = clean(raw_cand.get("channel"))
    url = clean(raw_cand.get("url")) or f"https://music.youtube.com/watch?v={video_id}"
    title_clean = clean(raw_cand.get("title_clean")) or normalize_title_text(title)
    duration_ms = parse_int(raw_cand.get("duration_ms"))
    return {
        "type": forced_type,
        "rank": parse_int(raw_cand.get("rank")),
        "videoId": video_id,
        "title": title,
        "title_clean": title_clean,
        "artists": raw_cand.get("artists") or [],
        "artists_clean": [normalize_artist_text(a) for a in (raw_cand.get("artists") or []) if normalize_artist_text(a)],
        "channel": channel,
        "channel_clean": normalize_artist_text(channel),
        "url": url,
        "thumbnail_url": clean(raw_cand.get("thumbnail_url")) or make_thumbnail_url(video_id),
        "duration_ms": duration_ms,
        "has_version_marker": contains_any_marker(title, VERSION_MARKERS),
    }


def apply_manual_choice_on_result(result_obj):
    search = result_obj.setdefault("search", {})
    decision = result_obj.setdefault("decision", {})
    result_obj.setdefault("scores", {})

    raw_choice = search.get("choise") or search.get("choice") or result_obj.get("choise") or search.get("selected_stage")
    choise = normalize_choise(raw_choice)
    candidates = search.get("candidates") or {}

    song_cand = dict_candidate_to_internal(candidates.get("song_first"), "song")
    video_cand = dict_candidate_to_internal(candidates.get("video_first"), "video")
    yt_existing = result_obj.get("youtube") or {}
    if not song_cand and normalize_choise(yt_existing.get("type")) == "song":
        song_cand = dict_candidate_to_internal(yt_existing, "song")
    if not video_cand and normalize_choise(yt_existing.get("type")) == "video":
        video_cand = dict_candidate_to_internal(yt_existing, "video")

    selected = None
    if choise == "song":
        selected = song_cand
    elif choise == "video":
        selected = video_cand

    search["choise"] = choise
    search["choice"] = choise
    search["selected_stage"] = "songs" if choise == "song" else ("videos" if choise == "video" else "none")
    search["selected_rank"] = selected.get("rank") if selected else None
    search["candidates"] = {
        "song_first": candidate_public_payload(song_cand),
        "video_first": candidate_public_payload(video_cand),
    }
    result_obj["choise"] = choise

    if selected:
        result_obj["youtube"] = youtube_payload_from_candidate(selected, forced_type=choise)
        decision["status"] = "matched"
    else:
        result_obj["youtube"] = youtube_payload_from_candidate(None)
        decision["status"] = "no_match"

    decision.setdefault("reasons_passed", [])
    decision.setdefault("reasons_failed", [])
    decision["notes"] = clean(decision.get("notes"))
    return choise, selected


def append_note(decision_obj, note):
    current = clean(decision_obj.get("notes"))
    if current:
        decision_obj["notes"] = f"{current}; {note}"
    else:
        decision_obj["notes"] = note


def main():
    fixed_path = Path(MATCH_FIXED_JSON)
    use_fixed = fixed_path.exists()

    if not use_fixed and not Path(CSV_FILE).exists():
        raise FileNotFoundError(f"Missing {CSV_FILE} in current folder.")
    if not Path(OAUTH_FILE).exists():
        raise FileNotFoundError(f"Missing {OAUTH_FILE} in current folder.")

    yt = YTMusic(OAUTH_FILE)
    if is_ai_matching_enabled():
        resolved_model = ensure_ai_backend_ready()
        print(
            f"Local AI rescue enabled via LM Studio model: {resolved_model} "
            f"(choice={AI_MODEL_KEY}) @ {AI_MATCH_BASE_URL}"
        )

    if not I_UNDERSTAND_THIS_WILL_DELETE_THE_PLAYLIST:
        print("\nSTOP: deletion flag is False.")
        print("Set I_UNDERSTAND_THIS_WILL_DELETE_THE_PLAYLIST = True to actually wipe and rebuild.")
        return

    print("\nDeleting all items from playlist...")
    removed = remove_all_from_playlist(yt, YTM_PLAYLIST_ID)
    print(f"Removed: {removed}")

    results = []
    seen_keys = set()
    added_count = 0
    add_failed_count = 0
    no_match_count = 0
    skipped_duplicates_in_csv = 0
    skipped_duplicate_video_ids = 0
    added_video_ids = set()
    persist_results(results)
    print(f"Initialized live results file: {Path(MATCH_RESULTS_JSON).resolve()} (model={AI_MODEL_KEY})")

    if use_fixed:
        fixed_data = json.loads(fixed_path.read_text(encoding="utf-8"))
        if not isinstance(fixed_data, list):
            raise ValueError(f"{MATCH_FIXED_JSON} must be a JSON array.")
        print(f"Using manual choices from {fixed_path.resolve()} (items={len(fixed_data)})")

        for i, raw_item in enumerate(fixed_data, start=1):
            if not isinstance(raw_item, dict):
                result_obj = {
                    "spotify": {"id": None, "title": f"row_{i}", "title_clean": "", "title_alt": None, "artists": [], "album": "", "duration_ms": None},
                    "search": {"query_used": "", "stage_checked": [], "selected_stage": "none", "selected_rank": None, "choise": "none", "choice": "none", "candidates": {"song_first": None, "video_first": None}},
                    "youtube": youtube_payload_from_candidate(None),
                    "decision": {"status": "no_match", "reasons_passed": [], "reasons_failed": ["invalid_fixed_item"], "notes": "row is not an object"},
                    "scores": {"title_sim": None, "artist_sim": None, "channel_sim": None, "duration_delta_ms": None},
                    "choise": "none",
                }
                no_match_count += 1
                results.append(result_obj)
                persist_results(results)
                print(f"[{i}] NO_MATCH: invalid row in {MATCH_FIXED_JSON}")
                continue

            result_obj = raw_item
            result_obj.setdefault("spotify", {})
            result_obj.setdefault("search", {})
            result_obj.setdefault("decision", {})
            result_obj.setdefault("scores", {})

            choise, selected = apply_manual_choice_on_result(result_obj)
            spotify_title = clean(result_obj.get("spotify", {}).get("title")) or f"row_{i}"

            if choise in ("song", "video") and selected and selected.get("videoId"):
                try:
                    vid = selected["videoId"]
                    if vid in added_video_ids:
                        skipped_duplicate_video_ids += 1
                        append_note(result_obj["decision"], f"skipped_duplicate_videoId={vid}")
                        print(f"[{i}] MATCHED BUT DUPLICATE_SKIPPED: {spotify_title} -> {selected.get('title', '')} ({choise})")
                    else:
                        add_to_playlist_with_retry(yt, YTM_PLAYLIST_ID, vid)
                        added_video_ids.add(vid)
                        added_count += 1
                        append_note(result_obj["decision"], "added_to_playlist")
                        print(
                            f"[{i}] MATCHED+ADDED: {spotify_title} -> {selected.get('title', '')} "
                            f"({choise})"
                        )
                except Exception as add_err:
                    add_failed_count += 1
                    append_note(result_obj["decision"], f"add_failed={add_err}")
                    print(f"[{i}] MATCHED BUT ADD_FAILED: {spotify_title} | {add_err}")
            else:
                no_match_count += 1
                append_note(result_obj["decision"], "manual_choice_none_or_missing_candidate")
                print(f"[{i}] NO_MATCH: {spotify_title}")

            results.append(result_obj)
            persist_results(results)
            time.sleep(SLEEP_BETWEEN_REQUESTS)
    else:
        with open(CSV_FILE, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, start=1):
                track = build_spotify_track(row)
                if not track["title"]:
                    continue

                key = (track["title_clean"], tuple(track["artists_clean"]))
                if key in seen_keys:
                    skipped_duplicates_in_csv += 1
                    continue
                seen_keys.add(key)

                try:
                    match = match_track(yt, track)
                    result_obj = {
                        "spotify": {
                            "id": track["id"],
                            "title": track["title"],
                            "title_clean": track["title_clean"],
                            "title_alt": track["title_alt"],
                            "artists": track["artists"],
                            "album": track["album"],
                            "duration_ms": track["duration_ms"],
                        },
                        "search": match["search"],
                        "youtube": match["youtube"],
                        "decision": match["decision"],
                        "scores": match["scores"],
                        "choise": match["search"].get("choise", "none"),
                    }

                    if result_obj["decision"]["status"] == "matched":
                        try:
                            vid = result_obj["youtube"]["videoId"]
                            if vid in added_video_ids:
                                skipped_duplicate_video_ids += 1
                                append_note(result_obj["decision"], f"skipped_duplicate_videoId={vid}")
                                print(
                                    f"[{i}] MATCHED BUT DUPLICATE_SKIPPED: {track['title']} -> {result_obj['youtube']['title']} "
                                    f"({result_obj['search']['selected_stage']})"
                                )
                            else:
                                add_to_playlist_with_retry(yt, YTM_PLAYLIST_ID, vid)
                                added_video_ids.add(vid)
                                added_count += 1
                                append_note(result_obj["decision"], "added_to_playlist")
                                print(
                                    f"[{i}] MATCHED+ADDED: {track['title']} -> {result_obj['youtube']['title']} "
                                    f"({result_obj['search']['selected_stage']})"
                                )
                        except Exception as add_err:
                            add_failed_count += 1
                            append_note(result_obj["decision"], f"add_failed={add_err}")
                            print(f"[{i}] MATCHED BUT ADD_FAILED: {track['title']} | {add_err}")
                    else:
                        no_match_count += 1
                        print(f"[{i}] NO_MATCH: {track['title']} - {', '.join(track['artists'])}")

                    results.append(result_obj)
                    persist_results(results)
                    time.sleep(SLEEP_BETWEEN_REQUESTS)

                except Exception as e:
                    no_match_count += 1
                    results.append({
                        "spotify": {
                            "id": track["id"],
                            "title": track["title"],
                            "title_clean": track["title_clean"],
                            "title_alt": track["title_alt"],
                            "artists": track["artists"],
                            "album": track["album"],
                            "duration_ms": track["duration_ms"],
                        },
                        "search": {
                            "query_used": "",
                            "stage_checked": [],
                            "selected_stage": "none",
                            "selected_rank": None,
                            "choise": "none",
                            "choice": "none",
                            "candidates": {"song_first": None, "video_first": None},
                        },
                        "youtube": youtube_payload_from_candidate(None),
                        "decision": {
                            "status": "no_match",
                            "reasons_passed": [],
                            "reasons_failed": ["exception"],
                            "notes": str(e),
                        },
                        "scores": {
                            "title_sim": None,
                            "artist_sim": None,
                            "channel_sim": None,
                            "duration_delta_ms": None,
                        },
                        "choise": "none",
                    })
                    persist_results(results)
                    print(f"[{i}] ERROR: {track['title']} | {e}")
                    time.sleep(1.0)

    persist_results(results)
    print(f"\nSaved: {Path(MATCH_RESULTS_JSON).resolve()} (items={len(results)}, model={AI_MODEL_KEY})")
    print(f"Mode: {'match_fixed' if use_fixed else 'csv_match'}")
    print(
        f"Done. Added={added_count}, NoMatch={no_match_count}, "
        f"AddFailed={add_failed_count}, DuplicateVideoIdSkipped={skipped_duplicate_video_ids}, "
        f"CSV dupes skipped={skipped_duplicates_in_csv}"
    )


if __name__ == "__main__":
    main()
