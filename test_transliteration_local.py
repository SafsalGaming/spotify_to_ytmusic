"""Offline tests: one search, cached engines, guards and XLSX audit."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import load_workbook

import artist_transliteration as local
import import_spotifyplaylist_to_ytmusic as app
from test_matching_local import track, raw


class TransliterationTests(unittest.TestCase):
    def setUp(self):
        local.artist_variants.cache_clear()
        self.addCleanup(local.artist_variants.cache_clear)
        for target in ("socket.socket.connect", "urllib.request.urlopen", "requests.sessions.Session.request"):
            blocker = patch(target, side_effect=AssertionError("Network forbidden"))
            mocked = blocker.start()
            self.addCleanup(blocker.stop)
            self.addCleanup(mocked.assert_not_called)
        disabled_ai = patch.object(app, "AI_MATCH_ENABLED", False)
        disabled_ai.start()
        self.addCleanup(disabled_ai.stop)

    def match(self, source, results):
        yt = Mock(spec=["search"])
        yt.search.return_value = results
        with contextlib.redirect_stdout(io.StringIO()):
            result = app.match_track(yt, source)
        yt.search.assert_called_once_with(app.build_queries(source)[0], filter=None, limit=app.TOP_RESULTS * 2)
        return result

    def test_original_query_preserves_spelling_punctuation_and_case(self):
        source = track("Don't Stop! (Live)", "Beyoncé")
        self.assertEqual(app.build_queries(source), ["Beyoncé Don't Stop! (Live)"])
        self.match(source, [])

    def test_all_engines_generate_once_per_name_and_cache_target_names(self):
        def transform(text, hebrew):
            return ["רביד פלוטניק"] if not hebrew else ["ravid plotnik"]

        engine_mocks = {name: Mock(side_effect=transform)
                        for name in ("PyICU", "transliterate", "gimeltra", "translit-me (local)")}
        with patch.object(local, "engines", return_value=(engine_mocks, {})):
            for title in ("First Song", "Next Song"):
                result = self.match(track(title, "Ravid Plotnik"), [raw(title, "רביד פלוטניק")])
                self.assertTrue(result["decision"]["checks"]["artist_match"])
            for engine in engine_mocks.values():
                self.assertEqual(engine.call_count, 2)  # source and target, once each
            self.assertGreaterEqual(local.artist_variants.cache_info().hits, 2)

    def test_normalization_deduplication_and_engine_provenance(self):
        mock = Mock(return_value=["  RÁVID--PLOTNIK ", "ravid plotnik", "רָבִיד"])
        with patch.object(local, "engines", return_value=({"one": mock, "two": mock}, {})):
            result = local.artist_variants("Ravid Plotnik")
            self.assertEqual(set(result["variants"]), {"ravid plotnik", "רביד"})
            self.assertEqual(set(result["variants"]["ravid plotnik"]), {"original", "one", "two"})
            self.assertEqual(local.artist_variants("Ravid Plotnik"), result)
            self.assertEqual(mock.call_count, 2)

    def test_direct_identity_skips_all_engines_and_still_ranks_every_candidate(self):
        results = [{**raw(video_id="video"), "resultType": "video"}, raw(video_id="song")]
        with patch.object(local, "artist_variants", side_effect=AssertionError("not needed")):
            result = self.match(track(), results)
        self.assertEqual(result["youtube"]["videoId"], "song")
        self.assertEqual(len(result["search"]["checked_candidates"]), 2)
        self.assertFalse(result["decision"]["artist_comparison"]["transliteration_used"])

    def test_title_or_hard_duration_rejection_does_not_run_engines(self):
        for target in (raw("Different Song", "רביד פלוטניק"),
                       raw("Song Name", "רביד פלוטניק", 250)):
            with self.subTest(target=target), patch.object(
                local, "artist_variants", side_effect=AssertionError("rejected candidate"),
            ):
                result = self.match(track(artist="Ravid Plotnik"), [target])
                self.assertEqual(result["decision"]["status"], "no_match")

    def test_incompatible_versions_are_rejected_even_with_identical_artists(self):
        for version in ("Live", "Remix", "Cover", "Acoustic", "Instrumental", "Karaoke", "Sped Up", "Slowed",
                        "Demo", "Remastered", "Radio Edit", "Medley"):
            with self.subTest(version=version):
                result = self.match(track(), [raw(f"Song Name ({version})")])
                self.assertEqual(result["decision"]["status"], "no_match")
                same_version = self.match(track(f"Song Name ({version})"), [raw(f"Song Name ({version})")])
                self.assertEqual(same_version["decision"]["status"], "matched")
                missing_version = self.match(track(f"Song Name ({version})"), [raw()])
                self.assertEqual(missing_version["decision"]["status"], "no_match")

    def test_exact_match_stops_variant_comparisons(self):
        def equivalent(left, right, fuzzy=False):
            if left == right == "shared artist":
                return True
            if left == "never compare":
                raise AssertionError("comparison continued past definitive match")
            return False

        def variants(name):
            return {"variants": {"shared artist": ("engine",), "never compare": ("engine",)}}

        with patch.object(local, "artist_variants", side_effect=variants), patch.object(
            local, "equivalent_names", side_effect=equivalent,
        ):
            result = local.match_artist_names(["Source"], ["Target"], "Song Name", True)
        self.assertTrue(result["matched"])
        self.assertEqual(result["source_variant"], "shared artist")

    def test_word_containment_is_bounded_and_fuzzy_is_conservative(self):
        for left, right, expected in (("Ravid Plotnik", "Ravid Plotnik Official", True),
                                      ("Art", "Artist", False), ("Bob", "Bobby", False),
                                      ("DJ", "DJ Other Person", False),
                                      ("Ravid Plotnik", "Ravid Plotnick", True),
                                      ("Ravid Plotnik", "David Bowie", False)):
            with self.subTest(left=left, right=right):
                self.assertEqual(local.equivalent_names(local.normalize_name(left),
                                                       local.normalize_name(right), fuzzy=True), expected)

    def test_engine_failure_is_cached_and_other_engines_still_match(self):
        failed = Mock(side_effect=RuntimeError("engine failed"))
        working = Mock(return_value=["רביד פלוטניק"])
        with patch.object(local, "engines", return_value=({"broken": failed, "working": working}, {})):
            first = local.artist_variants("Ravid Plotnik")
            self.assertEqual(first["errors"], {"broken": "engine failed"})
            self.assertIn("רביד פלוטניק", first["variants"])
            self.assertEqual(first, local.artist_variants("Ravid Plotnik"))
        failed.assert_called_once()
        working.assert_called_once()

    def test_real_engines_work_offline_in_both_directions(self):
        available, unavailable = local.engines()
        self.assertIn("transliterate", available)
        self.assertIn("gimeltra", available)
        self.assertIn("translit-me (local)", available)
        self.assertEqual(set(available) | set(unavailable),
                         {"PyICU", "transliterate", "gimeltra", "translit-me (local)"})
        for original in ("Ravid Plotnik", "רביד פלוטניק"):
            result = local.artist_variants(original)
            self.assertFalse(result["errors"])
            produced = {engine for values in result["variants"].values() for engine in values}
            self.assertTrue(set(available) <= produced)
        self.assertTrue(local.match_artist_names(["Ravid Plotnik"], ["רביד פלוטניק"], "יום חדש", True)["matched"])
        self.assertTrue(local.match_artist_names(["רביד פלוטניק"], ["Ravid Plotnik"], "יום חדש", True)["matched"])

    def test_mixed_script_names_run_each_direction_once(self):
        engine = Mock(return_value=["test"])
        with patch.object(local, "engines", return_value=({"engine": engine}, {})):
            local.artist_variants("Ravid רביד")
            local.artist_variants("Ravid רביד")
        self.assertEqual([call.args[1] for call in engine.call_args_list], [True, False])

    def test_engines_receive_original_hebrew_vowels_before_output_normalization(self):
        engine = Mock(return_value=["RÁVID"])
        with patch.object(local, "engines", return_value=({"engine": engine}, {})):
            result = local.artist_variants("רָבִיד")
        engine.assert_called_once_with("רָבִיד", True)
        self.assertEqual(set(result["variants"]), {"רביד", "ravid"})

    def test_local_table_expansion_is_bounded_and_preserves_unknown_characters(self):
        result = local._table_variants("a" * 40 + "!", {"a": ["x", "y"]})
        self.assertEqual(len(result), local.MAX_ENGINE_VARIANTS)
        self.assertTrue(all(value.endswith("!") for value in result))

    def test_mixed_response_ignores_nonplayable_types_and_deduplicates(self):
        results = [raw(video_id="song"), raw(video_id="song"),
                   {**raw(video_id="not-a-song"), "resultType": "episode"},
                   {"resultType": "artist", "artist": "First Artist"},
                   {**raw(video_id="video"), "resultType": "video"}]
        result = self.match(track(), results)
        self.assertEqual([x["candidate"]["videoId"] for x in result["search"]["checked_candidates"]], ["song", "video"])

    def test_excel_records_engine_variants_checks_and_decision_for_every_candidate(self):
        source = track("יום חדש", "Ravid Plotnik", 208653)
        result = {"spotify": source, **self.match(source, [
            raw("יום חדש", "רביד פלוטניק", 209, "good"),
            raw("יום חדש (Cover)", "רביד פלוטניק", 209, "cover"),
        ])}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent / "tmp") as temporary:
            path = Path(temporary) / "audit.xlsx"
            app.export_excel_report([result], path)
            workbook = load_workbook(path)
            try:
                for sheet in workbook:
                    headers = {cell.value: cell.column for cell in sheet[1]}
                    self.assertTrue(sheet.cell(2, headers["תעתיק הופעל"]).value)
                    self.assertTrue(sheet.cell(2, headers["מנוע התאמת אמן"]).value)
                    self.assertTrue(sheet.cell(2, headers["וריאנט אמן מקור"]).value)
                    self.assertTrue(sheet.cell(2, headers["וריאנט אמן יעד"]).value)
                    self.assertTrue(sheet.cell(2, headers["שם תואם"]).value)
                    self.assertTrue(sheet.cell(2, headers["אמן תואם"]).value)
                    self.assertTrue(sheet.cell(2, headers["משך תואם"]).value)
                    self.assertEqual(sheet.cell(2, headers["דמיון שם (%)"]).value, 100)
                candidates = workbook.worksheets[1]
                self.assertEqual(candidates["P3"].value, "rejected")
                self.assertIn("version_not_in_spotify:cover", candidates["Q3"].value)
            finally:
                workbook.close()


if __name__ == "__main__":
    unittest.main()
