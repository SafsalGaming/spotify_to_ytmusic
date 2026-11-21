# Spotify to YouTube Music

Install the Python dependencies with `python -m pip install -r requirements.txt`.
Gimeltra is pinned to an upstream Git revision because its package is not currently
available from the configured package index.

The importer accepts both the original Spotify CSV columns and the attached
export's `Song`, `Artist`, `Duration`, `Album`, and `Spotify Track Id` columns.
Run `python import_spotifyplaylist_to_ytmusic.py` from this directory to import.
The importer clears the configured target playlist before adding matches.

## Matching

Each processed track makes one unfiltered `YTMusic.search` call using the original
Spotify artist and title, requesting `TOP_RESULTS * 2` (6) results. All Songs and
Videos actually returned are deduplicated and kept in memory, even when the API
returns more than requested. No artist spelling or
transliteration variant triggers another search, including after an empty or
failed request.

Direct title, artist and duration checks run first. If artist identity is still
uncertain and the title, duration and version guards permit further comparison,
the matcher generates local artist-name variants. It compares Spotify artists
with candidate artists, channels, and artist-sized spans of the video title.
It never uses the variants in a search query.

The offline adapters are:

- PyICU: Hebrew-Latin and Latin-Hebrew transforms, when installed.
- `transliterate`: a local Hebrew language pack registered through its extension
  API when the installed release has no Hebrew pack.
- `gimeltra`: Hebrew/Latin script conversion.
- `translit-me`: local language-table execution only, with no service client.
  Ambiguous table expansion is capped at 128 results per step to bound memory.

An in-memory cache, keyed by the original input name, stores unique normalized
variants and their producing engines. Both source and candidate names use this
cache, including failed engine attempts. Direct matches bypass it. Exact and
whole-word comparisons precede a conservative RapidFuzz fallback for minor
spelling differences; the first definitive artist match stops variant comparison.
Every cached candidate is evaluated. A title match is mandatory: exact or at least
90% RapidFuzz full-title similarity after removing presentation/artist labels.
Featured-artist credits can be ignored; other bracketed title text is preserved.
The initial artist-comparison check also recognizes a near-matching title inside
a video title with a translated artist label. Final acceptance still requires
the full title to match after removing a confirmed artist label.
Partial fuzzy matching is never used. Small title typos can be accepted without AI.
Matching titles need supporting artist identity or compatible duration, with the
existing hard duration guard retained. Among eligible candidates, confirmed artist
identity comes first, then closest duration, then title similarity. A Song is
preferred only when these match qualities tie. There is no weighted scoring system.

Transliteration does not change the title or duration rules. Incompatible version
markers, including live, remix, cover, acoustic, instrumental, sped-up, slowed,
demo, remaster, edit and medley, must agree with the Spotify title. The existing
local AI review runs only for uncertain candidates when at least one candidate has
a matching title and passes the version/hard duration guards. It sees both Songs
and Videos. Its selection must still pass those title/version/duration guards.

Excel and JSON checkpoints include artist comparison details: whether
transliteration was used, the producing engine(s), matching source/target variants,
comparison method, title/artist/duration checks, selection, and rejection reasons.
Console logs also explain the outcome for every candidate, including eligible
candidates that lose to a better match.
The existing two Excel sheets are retained.

## Optional PyICU installation

`python -m pip install -r requirements-icu.txt` installs PyICU in addition to the
base dependencies. On Windows, building PyICU requires native ICU headers/import
libraries and a C++ compiler. Set `ICU_VERSION`, `PYICU_INCLUDES`, and
`PYICU_LFLAGS` for that installation as described by the
[PyICU project](https://pypi.org/project/PyICU/). ICU runtime DLLs must also be
available to Python. Missing optional engines are reported at startup; the other
local engines continue to work.

On this Windows/Python 3.13 environment, PyICU 2.17 was built against the official
ICU 79.1 Win64 MSVC package. Its `icuin79.dll`, `icuuc79.dll`, and `icudt79.dll`
were placed beside the installed `icu` extension. When building on Windows, pass
absolute ICU import-library paths without the `.lib` suffix in
`PYICU_LIBRARIES` (separated by semicolons); this prevents the Windows SDK's
similarly named ICU libraries from taking precedence. All four adapters are
enabled in the current environment.

## Verification

`python -B -m unittest -q test_matching_local test_transliteration_local`

Tests prohibit network access and use synthetic playlist operations. They cover
one-search behavior, cache reuse, actual local engine execution, early exits,
selection order, version/duration guards, and Excel audit fields.
