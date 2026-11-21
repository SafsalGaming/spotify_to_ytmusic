"""Offline artist-name variants and provenance. This module never uses HTTP."""
import re
import unicodedata
from functools import cache

from rapidfuzz.fuzz import ratio


MAX_ENGINE_VARIANTS = 128
GENERIC_ARTIST_WORDS = {"official", "topic", "music", "records", "vevo", "dj", "unknown"}


def normalize_name(text):
    text = unicodedata.normalize("NFKC", text or "").casefold()
    text = "".join(c for c in unicodedata.normalize("NFD", text)
                   if unicodedata.category(c) != "Mn")
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())


def equivalent_names(left, right, fuzzy=False):
    if not left or not right:
        return False
    if left == right:
        return True
    short, long = sorted((left, right), key=len)
    if (len(short) >= 3 and set(short.split()) - GENERIC_ARTIST_WORDS
            and re.search(r"(?<!\w)" + re.escape(short) + r"(?!\w)", long)):
        return True
    # Full-name comparison only: no fuzzy partial/token-set matching.
    if fuzzy and len(left.split()) == len(right.split()) and min(len(left), len(right)) >= 5:
        pairs = zip(left.split(), right.split())
        return ratio(left, right) >= 90 and all(
            a == b or (min(len(a), len(b)) >= 4 and a[0] == b[0] and ratio(a, b) >= 80)
            for a, b in pairs
        )
    return False


def _table_variants(text, table):
    """Bounded local execution of translit-me's longest-match language tables.

    Its public helper expands every ambiguous character into a Cartesian
    product. Bound each step to avoid exponential memory growth on long names.
    Unknown characters are preserved, so they cannot silently disappear.
    """
    phrases = [""]
    for word in text.split():
        variants, index = [""], 0
        while index < len(word):
            key = next((word[index:index + size] for size in (4, 3, 2, 1)
                        if word[index:index + size] in table), word[index])
            value = table.get(key, key)
            if callable(value):
                value = value(word, index)
            options = value if isinstance(value, list) else [value]
            variants = list(dict.fromkeys(prefix + suffix for prefix in variants for suffix in options))[:MAX_ENGINE_VARIANTS]
            index += len(key)
        phrases = list(dict.fromkeys((prefix + " " + word).strip()
                                    for prefix in phrases for word in variants))[:MAX_ENGINE_VARIANTS]
    return phrases


@cache
def engines():
    available, unavailable = {}, {}
    try:
        from icu import Transliterator
        transforms = {True: Transliterator.createInstance("Hebrew-Latin"),
                      False: Transliterator.createInstance("Latin-Hebrew")}
        available["PyICU"] = lambda text, hebrew: [transforms[hebrew].transliterate(text)]
    except Exception as error:
        unavailable["PyICU"] = str(error)
    try:
        from transliterate import translit, get_available_language_codes
        if "he" not in get_available_language_codes():
            from transliterate.base import TranslitLanguagePack, registry

            class HebrewArtistPack(TranslitLanguagePack):
                # The released package has no Hebrew pack. Register a local,
                # phonetic pack using its documented extension API.
                language_code, language_name = "he", "Hebrew artist names (local)"
                mapping = ("bvgdhwzyklmnsfpqrtiou", "בבגדהוזיקלמנספפקרתיוו")
                pre_processor_mapping = {"sh": "ש", "ch": "ח", "kh": "ח", "ts": "צ",
                                         "tz": "צ", "ph": "פ", "a": "", "e": ""}
                reversed_specific_mapping = ("ךםןף", "kmnp")
                reversed_specific_pre_processor_mapping = {"ץ": "ts"}

            registry.register(HebrewArtistPack)

        def transliterate_variants(text, hebrew):
            result = translit(text, "he", reversed=hebrew)
            return [result] if hebrew else [result, result.replace("ת", "ט")]

        available["transliterate"] = transliterate_variants
    except Exception as error:
        unavailable["transliterate"] = str(error)
    try:
        from gimeltra import tr
        available["gimeltra"] = lambda text, hebrew: [
            tr(text, sc="Hebr" if hebrew else "Latn", to_sc="Latn" if hebrew else "Hebr")
        ]
    except Exception as error:
        unavailable["gimeltra"] = str(error)
    try:
        from translit_me.lang_tables import HE_EN, EN_AR, AR_HE
        # EN_AR lacks the English p/v consonants; preserve them through Arabic
        # locally rather than dropping them, then apply its Hebrew table.
        english = {**EN_AR, "p": "پ", "v": "ڤ"}
        hebrew_table = {**AR_HE, "ڤ": "ב"}

        def translit_me_variants(text, hebrew):
            if hebrew:
                return _table_variants(text, HE_EN)
            return [variant for arabic in _table_variants(text, english)
                    for variant in _table_variants(arabic, hebrew_table)][:MAX_ENGINE_VARIANTS]

        available["translit-me (local)"] = translit_me_variants
    except Exception as error:
        unavailable["translit-me (local)"] = str(error)
    return available, unavailable


@cache
def artist_variants(original_name):
    """Cache every unique normalized variant with all producing engines."""
    normalized = normalize_name(original_name)
    variants = {normalized: {"original"}} if normalized else {}
    # Keep Hebrew vowel points/geresh for engines that understand them;
    # normalize the outputs only after transliteration.
    engine_input = unicodedata.normalize("NFKC", original_name).casefold()
    available, _ = engines()
    # Mixed-script names are converted in both directions, preserving other text.
    directions = ([True] if re.search(r"[\u05d0-\u05ea]", normalized) else [])
    if re.search(r"[a-z]", normalized):
        directions.append(False)
    errors = {}
    for engine, transform in available.items():
        for hebrew in directions:
            try:
                for text in transform(engine_input, hebrew):
                    value = normalize_name(text)
                    if value:
                        variants.setdefault(value, set()).add(engine)
            except Exception as error:
                errors[engine] = str(error)
    return {"variants": {key: tuple(sorted(value)) for key, value in variants.items()}, "errors": errors}


def match_artist_names(source_names, target_names, title, use_transliteration=False):
    """Return the first definitive artist comparison and its audit details."""
    targets = [(name, "artist") for name in dict.fromkeys(target_names) if normalize_name(name)]
    # Compare against title spans with the same number of words as the artist.
    # Also allow direct artist containment anywhere in the complete title.
    title_normalized = normalize_name(title)
    for source in source_names:
        left = normalize_name(source)
        for target, field in targets + [(title, "title")]:
            right = normalize_name(target)
            direct = equivalent_names(left, right) if field == "artist" else bool(
                left and re.search(r"(?<!\w)" + re.escape(left) + r"(?!\w)", right)
            )
            if direct:
                return {"matched": True, "method": "direct", "transliteration_used": False,
                        "engine": "original", "source_variant": left,
                        "target_variant": left if field == "title" else right,
                        "source_name": source, "target_name": source if field == "title" else target, "field": field}
    if not use_transliteration:
        return {"matched": False, "method": "none", "transliteration_used": False}

    for source in source_names:
        words = len(normalize_name(source).split())
        title_words = title_normalized.split()
        spans = [" ".join(title_words[i:i + words]) for i in range(len(title_words) - words + 1)] if words else []
        comparisons = targets + [(span, "title") for span in dict.fromkeys(spans)]
        left = artist_variants(source)["variants"]
        # Exact and whole-word matches precede fuzzy matches across all engines.
        for fuzzy in (False, True):
            for target, field in comparisons:
                right = artist_variants(target)["variants"]
                for a, a_engines in left.items():
                    for b, b_engines in right.items():
                        if equivalent_names(a, b, fuzzy=fuzzy):
                            source_changed = a != normalize_name(source)
                            target_changed = b != normalize_name(target)
                            producing = sorted(((set(a_engines) if source_changed else set())
                                                | (set(b_engines) if target_changed else set())) - {"original"})
                            used = source_changed or target_changed
                            return {"matched": True, "method": "fuzzy" if fuzzy else "transliteration",
                                    "transliteration_used": used, "engine": "; ".join(producing) if used else "RapidFuzz",
                                    "source_variant": a, "target_variant": b, "source_name": source,
                                    "target_name": target, "field": field,
                                    "similarity": ratio(a, b)}
    return {"matched": False, "method": "none", "transliteration_used": True, "engine": "",
            "source_variant": "", "target_variant": ""}
