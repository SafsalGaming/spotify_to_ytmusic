"""Offline regression tests. No credentials, LM Studio, or playlist access."""
import contextlib
import csv
import io
import json
import tempfile
import unittest
from itertools import product
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import load_workbook

import import_spotifyplaylist_to_ytmusic as app
import artist_transliteration as transliteration


ROOT = Path(__file__).resolve().parent


def track(title="Song Name", artist="First Artist", duration=180000):
    return app.build_spotify_track({
        "Track Name": title, "Artist Name(s)": artist,
        "Duration (ms)": duration, "Track URI": "spotify:track:source123",
    })


def raw(title="Song Name", artist="First Artist", duration=180, video_id="candidate"):
    result = {"videoId": video_id, "title": title, "artists": [{"name": artist}], "resultType": "song"}
    if duration is not None:
        result["duration_seconds"] = duration
    return result


def evaluate(source=None, target=None):
    return app.evaluate_candidate_check(
        "song", source or track(), app.extract_candidate(target or raw(), "song", 1),
    )


class OfflineTests(unittest.TestCase):
    def setUp(self):
        transliteration.artist_variants.cache_clear()
        self.addCleanup(transliteration.artist_variants.cache_clear)
        for target in ("urllib.request.urlopen", "socket.create_connection", "socket.socket.connect"):
            blocker = patch(target, side_effect=AssertionError("Network is forbidden in local tests"))
            blocker.start()
            self.addCleanup(blocker.stop)
        constructor = patch.object(app, "YTMusic", side_effect=AssertionError("Real YTMusic is forbidden"))
        constructor.start()
        self.addCleanup(constructor.stop)
        ai = patch.object(app, "AI_MATCH_ENABLED", False)
        ai.start()
        self.addCleanup(ai.stop)

    def fake_search(self, songs=(), videos=()):
        client = Mock(spec=["search"])
        client.search.return_value = ([{**item, "resultType": "song"} for item in songs]
                                      + [{**item, "resultType": "video"} for item in videos])
        return client

    def test_acceptance_truth_table(self):
        for version, title, artist, duration in product((False, True), repeat=4):
            with self.subTest(version=version, title=title, artist=artist, duration=duration):
                title_text = "Song Name" if title else "Other Track"
                if not version:
                    title_text += " (Remix)"
                result = evaluate(target=raw(
                    title_text, "First Artist" if artist else "Someone Else", 185 if duration else 190,
                ))
                self.assertEqual(result["strict_accepted"], version and title and (artist or duration))

    def test_duration_boundary(self):
        for duration, expected in ((175, True), (185, True), (174.999, False), (185.001, False)):
            with self.subTest(duration=duration):
                result = evaluate(target=raw(artist="Unknown", duration=duration))
                self.assertEqual(result["strict_accepted"], expected)

    def test_matching_artist_allows_modest_or_missing_duration(self):
        for duration in (190, None, 0):
            self.assertTrue(evaluate(target=raw(duration=duration))["strict_accepted"])

    def test_missing_and_zero_duration_do_not_match_unknown_artist(self):
        for duration in (None, 0):
            result = evaluate(target=raw(artist="Unknown", duration=duration))
            self.assertFalse(result["strict_accepted"])
            self.assertFalse(result["checks"]["duration_match"])

    def test_punctuation_and_official_labels(self):
        self.assertTrue(evaluate(
            source=track("Don't Stop!"), target=raw("First Artist - Don't Stop! [Official Audio]"),
        )["strict_accepted"])

    def test_word_boundaries_and_meaningful_brackets(self):
        self.assertFalse(evaluate(source=track("Love"), target=raw("Lover"))["strict_accepted"])
        self.assertFalse(evaluate(source=track("Love Story"), target=raw("Love"))["strict_accepted"])
        self.assertFalse(evaluate(source=track("Song (Part Two)"), target=raw("Song"))["strict_accepted"])
        self.assertTrue(evaluate(source=track("(Song Name)"))["strict_accepted"])

    def test_repeated_title_words_are_required(self):
        self.assertFalse(evaluate(source=track("Go Go"), target=raw("Go"))["strict_accepted"])

    def test_artist_name_inside_song_title_is_preserved(self):
        self.assertTrue(evaluate(track("Killer Queen", "Queen"), raw("Queen - Killer Queen", "Queen"))["strict_accepted"])
        self.assertFalse(evaluate(track("Killer Queen", "Queen"), raw("Killer", "Queen"))["strict_accepted"])
        self.assertFalse(evaluate(track("Queen", "Queen"), raw("Queen - Another Song", "Queen"))["strict_accepted"])

    def test_title_consisting_only_of_version_word(self):
        self.assertTrue(evaluate(track("Live"), raw("Live"))["strict_accepted"])

    def test_artist_containment_both_directions(self):
        for source, target in (("First Artist", "First Artist Official"), ("First Artist", "Artist")):
            with self.subTest(source=source, target=target):
                self.assertTrue(evaluate(track(artist=source), raw(artist=target, duration=190))["strict_accepted"])

    def test_artist_boundaries(self):
        self.assertFalse(evaluate(track(artist="Art"), raw(artist="Artist", duration=190))["strict_accepted"])

    def test_artist_from_title_or_channel(self):
        title_result = evaluate(target=raw("First Artist - Song Name", "Unknown", 190))
        self.assertTrue(title_result["strict_accepted"])
        target = raw(artist="Unknown", duration=190)
        target["author"] = "First Artist Official"
        self.assertTrue(evaluate(target=target)["strict_accepted"])

    def test_any_spotify_artist_can_match(self):
        self.assertTrue(evaluate(track(artist="Other;First Artist"), raw(duration=190))["strict_accepted"])

    def test_million_dollar_spelling_and_artist_aliases(self):
        source = track("מליון דולר", "Tuna", 173660)
        client = self.fake_search([
            raw("מיליון דולר", "Noa Kirel", 174, "other_artist"),
            raw("מיליון דולר", "טונה", 174, "tuna"),
        ])
        with patch.object(app, "AI_MATCH_ENABLED", True), patch.object(app, "run_lm_studio_json_prompt") as ai:
            result = app.match_track(client, source)
        ai.assert_not_called()
        self.assertEqual(result["youtube"]["videoId"], "tuna")
        self.assertEqual(result["scores"]["duration_delta_ms"], 340)
        self.assertEqual(result["search"]["ai"]["status"], "skipped_full_match")
        self.assertTrue(result["decision"]["checks"]["title_match"])
        self.assertTrue(result["decision"]["checks"]["artist_match"])

    def test_extra_words_cannot_hide_a_substantially_different_title(self):
        result = evaluate(target=raw("First Artist - Song Name Revisited"))
        self.assertFalse(result["metadata_match"])
        self.assertFalse(result["strict_accepted"])
        self.assertEqual(result["extra_title_words"], ["revisited"])
        self.assertEqual(result["red_flags"], ["extra_title_words"])

    def test_bracketed_annotations_do_not_raise_extra_word_warning(self):
        result = evaluate(target=raw("First Artist-Song Name (feat. Another Artist) [bonus notes]"))
        self.assertEqual(result["extra_title_words"], [])
        self.assertTrue(result["strict_accepted"])

    def test_nested_brackets_are_ignored_only_for_extra_words(self):
        result = evaluate(target=raw("Song Name (additional [nested] description)"))
        self.assertEqual(result["extra_title_words"], [])
        self.assertTrue(result["strict_accepted"])
        live = evaluate(target=raw("Song Name (Live)"))
        self.assertEqual(live["extra_title_words"], [])
        self.assertFalse(live["hard_allowed"])

    def test_repeated_extra_title_words_are_detected(self):
        self.assertEqual(evaluate(target=raw("Song Name Song"))["extra_title_words"], ["song"])

    def test_artist_prefix_with_no_space_does_not_count_as_song_title(self):
        result = evaluate(track("ננח", "ננח קלאסי", 141686), raw(
            "ננח קלאסי- פלקסים בימית2000", "classy nanach", 218,
        ))
        self.assertFalse(result["checks"]["title_match"])
        self.assertFalse(result["strict_accepted"])

    def test_relative_duration_guard_in_both_directions_and_at_boundary(self):
        for duration, allowed in ((80, False), (80.001, True), (79.999, False),
                                  (120, False), (119.999, True), (120.001, False)):
            with self.subTest(duration=duration):
                result = evaluate(track(duration=100000), raw(duration=duration))
                self.assertEqual(result["hard_allowed"], allowed)
                self.assertEqual(result["strict_accepted"], allowed)

    def test_ai_cannot_override_relative_duration_guard(self):
        response = {"decision": "match", "videoId": "candidate", "reason": "same title"}
        with patch.object(app, "AI_MATCH_ENABLED", True), patch.object(
            app, "run_lm_studio_json_prompt", return_value=(response, None),
        ) as ai:
            result = app.match_track(self.fake_search([raw(duration=250)]), track())
        ai.assert_not_called()
        self.assertEqual(result["decision"]["status"], "no_match")
        self.assertEqual(result["search"]["ai"]["status"], "skipped_no_viable_candidates")
        self.assertFalse(result["search"]["checked_candidates"][0]["final_accepted"])

    def test_hebrew_live_studio_annotation_is_a_hard_rejection(self):
        result = evaluate(target=raw("Song Name (חי באולפן גלגלצ)"))
        self.assertFalse(result["checks"]["version_allowed"])
        self.assertFalse(result["hard_allowed"])

    def test_clear_match_does_not_request_an_ai_decision(self):
        output = io.StringIO()
        with patch.object(app, "AI_MATCH_ENABLED", True), patch.object(app, "run_lm_studio_json_prompt") as ai, contextlib.redirect_stdout(output):
            result = app.match_track(self.fake_search([raw()]), track())
        ai.assert_not_called()
        self.assertEqual(result["decision"]["notes"], "selected_by_rules")
        self.assertIn("AI not needed", output.getvalue())
        self.assertNotIn("AI: finished", output.getvalue())

    def test_ai_none_is_final_for_uncertain_artist_and_duration(self):
        response = {"decision": "none", "videoId": None, "reason": "different version"}
        with patch.object(app, "AI_MATCH_ENABLED", True), patch.object(
            app, "run_lm_studio_json_prompt", return_value=(response, None),
        ) as ai:
            result = app.match_track(self.fake_search([raw(artist="Unknown", duration=190)]), track())
        ai.assert_called_once()
        self.assertEqual(result["decision"]["status"], "no_match")
        self.assertIsNone(result["youtube"]["videoId"])
        self.assertEqual(result["decision"]["notes"], "ai_returned_none")
        item = result["search"]["checked_candidates"][0]
        self.assertFalse(item["evaluation"]["metadata_match"])
        self.assertFalse(item["final_accepted"])
        prompt = json.loads(ai.call_args.args[0])
        self.assertEqual(prompt["candidates"][0]["extra_title_words"], [])

    def test_search_query_combines_artist_and_song_with_one_space(self):
        source = track("  כנפיים  ", "  Tuna  ")
        self.assertEqual(app.build_queries(source), ["Tuna כנפיים"])
        client = self.fake_search()
        app.match_track(client, source)
        client.search.assert_called_once_with("Tuna כנפיים", filter=None, limit=6)

    def test_versions_are_checked_separately(self):
        for source, target, expected in (
            ("Song Name", "Song Name (Live)", False),
            ("Song Name", "Song Name Remix", False),
            ("Song Name (Live)", "Song Name (Remix)", False),
            ("Song Name Remix", "Song Name Live", False),
            ("Song Name Live", "Song Name Live Remix", False),
            ("Song Name Live", "Song Name Live", True),
            ("Song Name Remix", "Song Name Remix", True),
            ("Song Name", "Song Name Alive", False),
            ("Song Name", "Song Name - רמיקס", False),
            ("Song Name", "Song Name בהופעה חיה", False),
        ):
            with self.subTest(source=source, target=target):
                self.assertEqual(evaluate(track(source), raw(target))["strict_accepted"], expected)

    def test_hebrew_and_unicode_names(self):
        self.assertTrue(evaluate(track("שיר אהבה", "אמן"), raw("אמן — שיר אהבה (Official Audio)", "אחר", 190))["strict_accepted"])
        self.assertTrue(evaluate(track("Déjà Vu", "Beyoncé"), raw("DEJA VU", "Beyonce", 190))["strict_accepted"])

    def test_next_candidate_after_rejection_and_complete_log(self):
        client = self.fake_search(
            [raw("Song Name Remix", video_id="bad"), raw(video_id="good"), raw("Wrong", video_id="wrong")],
            [raw("Song Name Live", video_id="live")],
        )
        result = app.match_track(client, track())
        self.assertEqual(result["youtube"]["videoId"], "good")
        checked = result["search"]["checked_candidates"]
        self.assertEqual(len(checked), 4)
        self.assertEqual([item["selected"] for item in checked], [False, True, False, False])
        self.assertEqual(client.search.call_count, 1)

    def test_video_fallback(self):
        result = app.match_track(self.fake_search([raw("Wrong")], [raw(video_id="video")]), track())
        self.assertEqual(result["youtube"]["type"], "video")

    def test_fuzzy_titles_accept_small_spelling_and_wording_changes(self):
        for source_title, candidate_title in (("Song Name", "Song Nme"), ("Love Story", "Love Stroy"),
                                               ("A Brand New Day", "Brand New Day"), ("יום חדש", "יום חד")):
            with self.subTest(source=source_title, candidate=candidate_title), patch.object(
                app, "AI_MATCH_ENABLED", True,
            ), patch.object(app, "run_lm_studio_json_prompt") as ai:
                result = app.match_track(self.fake_search([raw(candidate_title)]), track(source_title))
                self.assertEqual(result["decision"]["status"], "matched")
                self.assertGreaterEqual(result["scores"]["title_sim"], 90)
                self.assertLess(result["scores"]["title_sim"], 100)
                ai.assert_not_called()

    def test_closest_duration_wins_regardless_of_category(self):
        for song_duration, video_duration, expected in ((182, 180, "video"), (180, 182, "song"),
                                                        (180, 180, "song"), (None, 180, "video")):
            with self.subTest(song=song_duration, video=video_duration):
                result = app.match_track(self.fake_search(
                    [raw(duration=song_duration, video_id="song")],
                    [raw(duration=video_duration, video_id="video")],
                ), track())
                self.assertEqual(result["youtube"]["videoId"], expected)
                self.assertTrue(all(item["evaluation"]["strict_accepted"]
                                    for item in result["search"]["checked_candidates"]))

    def test_later_acceptable_candidate_with_closer_duration_wins(self):
        result = app.match_track(self.fake_search([
            raw(duration=182, video_id="first"), raw(duration=180, video_id="second"),
            raw(duration=181, video_id="third"),
        ]), track())
        self.assertEqual(result["youtube"]["videoId"], "second")
        self.assertEqual(len(result["search"]["checked_candidates"]), 3)
        self.assertEqual(result["scores"]["duration_delta_ms"], 0)

    def test_exact_video_title_wins_over_fuzzy_song_with_equal_duration(self):
        result = app.match_track(self.fake_search([raw("Song Nme", video_id="song")],
                                                 [raw(video_id="video")]), track())
        self.assertEqual(result["youtube"]["videoId"], "video")

    def test_fuzzy_title_preserves_artist_words_that_belong_to_title(self):
        result = evaluate(track("Queen of Hearts", "Queen"), raw("Queen of Heart", "Queen"))
        self.assertTrue(result["strict_accepted"])
        self.assertGreaterEqual(result["scores"]["title_sim"], 90)

    def test_ai_sees_both_types_and_cannot_override_wrong_title(self):
        for selected, expected in (("wrong", "no_match"), ("video", "matched")):
            response = {"decision": "match", "videoId": selected, "reason": "same recording"}
            with self.subTest(selected=selected), patch.object(app, "AI_MATCH_ENABLED", True), patch.object(
                app, "run_lm_studio_json_prompt", return_value=(response, None),
            ) as ai:
                client = self.fake_search([raw("Completely Different", video_id="wrong")],
                                           [raw(artist="Unknown", duration=190, video_id="video")])
                result = app.match_track(client, track())
                self.assertEqual(result["decision"]["status"], expected)
                self.assertEqual({c["type"] for c in json.loads(ai.call_args.args[0])["candidates"]},
                                 {"song", "video"})
                client.search.assert_called_once()
                if selected == "video":
                    self.assertEqual(result["youtube"]["type"], "video")

    def test_confirmed_video_artist_precedes_unconfirmed_song_artist(self):
        source = track("יום חדש", "Ravid Plotnik", 208653)
        # Translation may fail or leave a proper name unchanged; song still wins.
        with patch.object(transliteration, "engines", return_value=({}, {})):
            result = app.match_track(self.fake_search(
                [raw("יום חדש", "רביד פלוטניק", 209, "K80dDBDVsBE")],
                [raw("יום חדש", "Ravid Plotnik", 209, "Jg7YL6N5bOs")],
            ), source)
        self.assertEqual(result["youtube"]["videoId"], "Jg7YL6N5bOs")
        checked = result["search"]["checked_candidates"]
        self.assertTrue(all(item["evaluation"]["strict_accepted"] for item in checked))
        self.assertFalse(checked[0]["evaluation"]["checks"]["artist_match"])
        self.assertTrue(checked[1]["evaluation"]["checks"]["artist_match"])

    def test_transliterated_artist_matches_metadata_and_title_in_both_directions(self):
        for original, translated in (
            ("Ravid Plotnik", "רביד פלוטניק"),
            ("רביד פלוטניק", "Ravid Plotnik"),
        ):
            for in_title in (False, True):
                with self.subTest(original=original, in_title=in_title):
                    transliteration.artist_variants.cache_clear()
                    source = track("יום חדש", original, 208653)
                    target = raw(f"{translated} - יום חדש" if in_title else "יום חדש",
                                 "Unknown" if in_title else translated, 218, "translated")
                    client = self.fake_search([target])
                    result = app.match_track(client, source)
                    self.assertEqual(result["youtube"]["videoId"], "translated")
                    self.assertTrue(result["decision"]["checks"]["artist_match"])
                    self.assertFalse(result["decision"]["checks"]["duration_match"])
                    self.assertTrue(result["decision"]["artist_comparison"]["transliteration_used"])
                    client.search.assert_called_once_with(f"{original} יום חדש", filter=None, limit=6)

    def test_direct_matches_do_not_run_transliteration(self):
        with patch.object(transliteration, "artist_variants", side_effect=AssertionError("unnecessary transliteration")):
            for title in ("First Song", "Second Song"):
                result = app.match_track(self.fake_search([raw(title)]), track(title))
                self.assertEqual(result["decision"]["status"], "matched")

    def test_transliteration_starts_after_search_and_can_match_another_source_artist(self):
        client = self.fake_search([raw(artist="רביד פלוטניק")])
        real_variants = transliteration.artist_variants

        def variants(name):
            self.assertGreater(client.search.call_count, 0)
            return real_variants(name)

        with patch.object(transliteration, "artist_variants", side_effect=variants):
            result = app.match_track(client, track(artist="Other Artist;Ravid Plotnik"))
        self.assertTrue(result["decision"]["checks"]["artist_match"])

    def test_ai_prompt_uses_general_rules(self):
        source = track("יום חדש", "Ravid Plotnik")
        prompt = json.loads(app.build_ai_match_prompt(source, []))
        rules = " ".join(prompt["rules"])
        self.assertNotIn("Tuna", rules)
        self.assertNotIn("מליון דולר", rules)
        self.assertIn("prefer a song only when equally good", rules)
        self.assertIn("ravid plotnik", prompt["spotify"]["known_artist_names"])

    def test_new_playlist_csv_columns(self):
        source = app.build_spotify_track({
            "Song": "יום חדש", "Artist": "Ravid Plotnik", "Duration": "03:29",
            "Album": "Album Name", "Spotify Track Id": "track123",
        })
        self.assertEqual(source["title"], "יום חדש")
        self.assertEqual(source["artists"], ["Ravid Plotnik"])
        self.assertEqual(source["duration_ms"], 209000)
        self.assertEqual(source["album"], "Album Name")
        self.assertEqual(source["id"], "track123")

    def test_main_imports_new_csv_format_and_adds_song_after_empty_check(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temporary:
            directory = Path(temporary)
            csv_path = directory / "playlist.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["Song", "Artist", "Duration", "Spotify Track Id"])
                writer.writeheader()
                writer.writerow({"Song": "יום חדש", "Artist": "Ravid Plotnik",
                                 "Duration": "03:28", "Spotify Track Id": "source123"})
            auth_path = directory / "auth.json"
            auth_path.write_text("{}", encoding="utf-8")
            client = Mock()
            client.get_playlist.return_value = {"owned": True, "tracks": [], "trackCount": 0}
            client.search.return_value = [raw("יום חדש", "רביד פלוטניק", 209, "song"),
                                          {**raw("יום חדש", "Ravid Plotnik", 209, "video"), "resultType": "video"}]

            def add(playlist_id, video_ids):
                client.get_playlist.assert_called_once_with(app.YTM_PLAYLIST_ID, limit=None)
                self.assertEqual(video_ids, ["song"])

            client.add_playlist_items.side_effect = add
            with patch.multiple(app, CSV_FILE=str(csv_path), OAUTH_FILE=str(auth_path),
                                MATCH_FIXED_JSON=str(directory / "missing.json"),
                                MATCH_RESULTS_JSON=str(directory / "results.json"),
                                MATCH_RESULTS_XLSX=str(directory / "report.xlsx")), patch.object(
                app, "YTMusic", return_value=client,
            ), patch.object(app.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                app.main()
            rows = json.loads((directory / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(rows[0]["spotify"]["title"], "יום חדש")
            self.assertEqual(rows[0]["spotify"]["duration_ms"], 208000)
            self.assertEqual(rows[0]["youtube"]["type"], "song")
            self.assertEqual(rows[0]["addition_status"], "added")
            client.add_playlist_items.assert_called_once()
            client.remove_playlist_items.assert_not_called()

    def test_oversized_search_batches_are_capped_before_evaluation_and_ai(self):
        client = self.fake_search(
            [raw(artist="Unknown", duration=190, video_id=f"song_{index}") for index in range(20)],
            [raw(artist="Unknown", duration=190, video_id=f"video_{index}") for index in range(20)],
        )
        response = {"decision": "none", "videoId": None, "reason": "no preference"}
        with patch.object(app, "AI_MATCH_ENABLED", True), patch.object(
            app, "run_lm_studio_json_prompt", return_value=(response, None),
        ) as ai:
            result = app.match_track(client, track())
        expected_ids = ["song_0", "song_1", "song_2", "video_0", "video_1", "video_2"]
        checked = result["search"]["checked_candidates"]
        self.assertEqual([item["candidate"]["videoId"] for item in checked], expected_ids)
        prompt = json.loads(ai.call_args.args[0])
        self.assertEqual([item["videoId"] for item in prompt["candidates"]], expected_ids)
        self.assertEqual(client.search.call_count, 1)
        self.assertEqual(client.search.call_args.kwargs, {"filter": None, "limit": 6})

    def test_fourth_result_cannot_be_selected(self):
        client = self.fake_search(
            [raw("Wrong", video_id=f"wrong_{index}") for index in range(3)]
            + [raw(video_id="fourth_match")],
        )
        result = app.match_track(client, track())
        self.assertEqual(result["decision"]["status"], "no_match")
        self.assertEqual(len(result["search"]["checked_candidates"]), 3)

    def test_empty_search(self):
        result = app.match_track(self.fake_search(), track())
        self.assertEqual(result["decision"]["status"], "no_match")
        self.assertEqual(result["search"]["checked_candidates"], [])

    def test_search_failure_does_not_retry(self):
        client = self.fake_search()
        client.search.side_effect = [RuntimeError("search failed"),
                                     [{**raw(video_id="video"), "resultType": "video"}]]
        result = app.match_track(client, track())
        self.assertIsNone(result["youtube"]["videoId"])
        self.assertEqual(result["search"]["errors"][0]["stage"], "search")
        client.search.assert_called_once()

    def test_ai_uses_existing_candidates_without_research(self):
        client = self.fake_search([raw(artist="Unknown", duration=190, video_id="first")],
                                  [raw(artist="Unknown", duration=190, video_id="second")])
        response = {"decision": "match", "videoId": "second", "reason": "same recording"}
        with patch.object(app, "AI_MATCH_ENABLED", True), patch.object(
            app, "run_lm_studio_json_prompt", return_value=(response, None),
        ) as ai:
            result = app.match_track(client, track())
        self.assertEqual(client.search.call_count, 1)
        self.assertEqual(result["youtube"]["videoId"], "second")
        prompt, model = ai.call_args.args
        self.assertEqual(model, "qwen/qwen3.5-9b")
        self.assertEqual(len(json.loads(prompt)["candidates"]), 2)
        self.assertEqual(result["search"]["checked_candidates"][1]["ai_candidate_decision"], "chosen")

    def test_invalid_ai_selection_none_or_outage_never_falls_back(self):
        for response, error in (
            ({"decision": "match", "videoId": "bad", "reason": "wrong"}, None),
            ({"decision": "match", "videoId": "invented", "reason": "wrong"}, None),
            ({"decision": "none", "videoId": None, "reason": "uncertain"}, None),
            (None, "offline"),
        ):
            with self.subTest(response=response, error=error), patch.object(app, "AI_MATCH_ENABLED", True), patch.object(
                app, "run_lm_studio_json_prompt", return_value=(response, error),
            ):
                result = app.match_track(self.fake_search([
                    raw("Song Name Live", video_id="bad"), raw(artist="Unknown", duration=190, video_id="good"),
                ]), track())
                self.assertEqual(result["decision"]["status"], "no_match")
                self.assertIsNone(result["youtube"]["videoId"])
                self.assertEqual(result["search"]["ai"]["response"], response)

    def test_ai_skips_clearly_incompatible_candidates(self):
        response = {"decision": "none", "videoId": None, "reason": "wrong version"}
        with patch.object(app, "AI_MATCH_ENABLED", True), patch.object(
            app, "run_lm_studio_json_prompt", return_value=(response, None),
        ) as ai:
            result = app.match_track(self.fake_search([raw("Song Name Remix")]), track())
            ai.assert_not_called()
            self.assertEqual(result["search"]["ai"]["status"], "skipped_no_viable_candidates")
            self.assertEqual(result["decision"]["status"], "no_match")

    def test_ai_cannot_override_title_mismatch_and_report_records_rejection(self):
        source = track("כנפיים", "Tuna")
        response = {"decision": "match", "videoId": "transliterated", "reason": "same title in transliteration"}
        with patch.object(app, "AI_MATCH_ENABLED", True), patch.object(
            app, "run_lm_studio_json_prompt", return_value=(response, None),
        ) as ai:
            result = app.match_track(self.fake_search([raw("Knafaim", "Tuna", video_id="transliterated")]), source)
        ai.assert_not_called()
        item = result["search"]["checked_candidates"][0]
        self.assertFalse(item["evaluation"]["strict_accepted"])
        self.assertFalse(item["evaluation"]["checks"]["title_match"])
        self.assertFalse(item["accepted_by_ai"])
        self.assertFalse(item["final_accepted"])
        self.assertEqual(result["decision"]["status"], "no_match")
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temporary:
            destination = Path(temporary) / "rescued.xlsx"
            app.export_excel_report([{"spotify": source, **result}], destination)
            workbook = load_workbook(destination)
            try:
                self.assertEqual(workbook.worksheets[1]["P2"].value, "rejected")
                self.assertFalse(workbook.worksheets[1]["M2"].value)
                self.assertIn("title_mismatch", workbook.worksheets[1]["Q2"].value)
                self.assertIn("title_mismatch", workbook.worksheets[0]["Q2"].value)
            finally:
                workbook.close()

    def test_ai_cannot_override_version_restrictions_in_rescue(self):
        for version in ("Live", "Remix"):
            response = {"decision": "match", "videoId": "bad", "reason": "same title"}
            with self.subTest(version=version), patch.object(app, "AI_MATCH_ENABLED", True), patch.object(
                app, "run_lm_studio_json_prompt", return_value=(response, None),
            ) as ai:
                result = app.match_track(self.fake_search([raw(f"Song Name {version}", video_id="bad")]), track())
                ai.assert_not_called()
                self.assertEqual(result["decision"]["status"], "no_match")
                self.assertEqual(result["search"]["ai"]["status"], "skipped_no_viable_candidates")

    def test_empty_search_has_no_candidates_to_send_to_ai(self):
        with patch.object(app, "AI_MATCH_ENABLED", True), patch.object(app, "run_lm_studio_json_prompt") as ai:
            result = app.match_track(self.fake_search(), track())
            ai.assert_not_called()
            self.assertEqual(result["search"]["ai"]["status"], "skipped_no_candidates")

    def test_failed_ai_rescue_does_not_accept_rejected_candidates(self):
        for response, error in (
            ({"decision": "none", "videoId": None, "reason": "uncertain"}, None),
            ({"decision": "match", "videoId": "not_in_search", "reason": "invented"}, None),
            (None, "timeout"),
        ):
            with self.subTest(response=response), patch.object(app, "AI_MATCH_ENABLED", True), patch.object(
                app, "run_lm_studio_json_prompt", return_value=(response, error),
            ) as ai:
                result = app.match_track(self.fake_search([raw(artist="Unknown", duration=190)]), track())
                ai.assert_called_once()
                self.assertEqual(result["decision"]["status"], "no_match")

    def mock_response(self, message, finish_reason="stop"):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"choices": [{"message": message, "finish_reason": finish_reason}]}).encode()
        return response

    def test_structured_json_and_reasoning_disabled_request(self):
        decision = {"decision": "match", "videoId": "candidate", "reason": "same"}
        with patch("urllib.request.urlopen", return_value=self.mock_response({"content": json.dumps(decision)})) as request:
            parsed, error = app.run_lm_studio_json_prompt("prompt", app.AI_MATCH_MODEL)
        self.assertIsNone(error)
        self.assertEqual(parsed, decision)
        body = json.loads(request.call_args.args[0].data)
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertFalse(body["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(body["response_format"]["type"], "json_schema")

    def test_bad_ai_responses_rejected(self):
        valid = {"decision": "none", "videoId": None, "reason": "no match"}
        for message, finish in (
            ({"content": "not JSON"}, "stop"),
            ({"content": json.dumps({**valid, "extra": 1})}, "stop"),
            ({"content": json.dumps({**valid, "decision": "match"})}, "stop"),
            ({"content": json.dumps(valid), "reasoning_content": "thinking"}, "stop"),
            ({"content": "<think>reason</think>" + json.dumps(valid)}, "stop"),
            ({"content": json.dumps(valid)}, "length"),
        ):
            with self.subTest(message=message), patch("urllib.request.urlopen", return_value=self.mock_response(message, finish)):
                parsed, error = app.run_lm_studio_json_prompt("prompt", app.AI_MATCH_MODEL)
                self.assertIsNone(parsed)
                self.assertTrue(error)

    def test_excel_and_json_checkpoint(self):
        result = {"spotify": track(), **app.match_track(
            self.fake_search([raw("Song Name Remix", video_id="bad"), raw(video_id="good")]), track(),
        ), "addition_status": "not_attempted"}
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temporary:
            json_path, excel_path = Path(temporary) / "results.json", Path(temporary) / "report.xlsx"
            with patch.object(app, "MATCH_RESULTS_JSON", str(json_path)), patch.object(app, "MATCH_RESULTS_XLSX", str(excel_path)):
                app.persist_results([result])
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))[0]["addition_status"], "not_attempted")
            workbook = load_workbook(excel_path)
            try:
                self.assertEqual(workbook.sheetnames, ["שירים", "כל המועמדים שנבדקו"])
                songs, candidates = workbook.worksheets
                self.assertEqual(songs.max_row, 2)
                self.assertEqual(candidates.max_row, 3)
                self.assertEqual(candidates["P2"].value, "rejected")
                self.assertEqual(candidates["P3"].value, "accepted")
                self.assertEqual(candidates["J3"].value, 0)
                self.assertIn("good", candidates["K3"].hyperlink.target)
                self.assertEqual(songs["V2"].value, "not_attempted")
                self.assertTrue(songs.sheet_view.rightToLeft)
                self.assertEqual(songs.freeze_panes, "F2")
            finally:
                workbook.close()

    def test_empty_report_and_spreadsheet_formula_safety(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temporary:
            destination = Path(temporary) / "report.xlsx"
            app.export_excel_report([], destination)
            workbook = load_workbook(destination)
            self.assertEqual(workbook.worksheets[0].max_row, 1)
            workbook.close()
        self.assertEqual(app.excel_text("=1+1"), "'=1+1")
        self.assertEqual(app.excel_text("safe\x00text"), "safetext")

    def test_manual_choices_are_revalidated_and_audited(self):
        bad = app.candidate_public_payload(app.extract_candidate(raw("Song Name Live", video_id="bad"), "song", 1))
        good = app.candidate_public_payload(app.extract_candidate(raw(video_id="good"), "song", 2))
        result = {"spotify": track(), "search": {"choise": "song", "candidates": {"song_first": bad, "song_second": good}}}
        _, selected = app.apply_manual_choice_on_result(result)
        self.assertEqual(selected["videoId"], "good")
        self.assertEqual(len(result["search"]["checked_candidates"]), 2)

    def test_main_with_synthetic_data_and_mocked_playlist_operations(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temporary:
            directory = Path(temporary)
            csv_path = directory / "synthetic.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["Track Name", "Artist Name(s)", "Duration (ms)"])
                writer.writeheader()
                for title in ("Song Name", "Another Name", "Faulty", "Song Name", ""):
                    writer.writerow({"Track Name": title, "Artist Name(s)": "First Artist", "Duration (ms)": 180000})
            oauth_path = directory / "fake_oauth.json"
            oauth_path.write_text("{}", encoding="utf-8")
            client = self.fake_search()

            def search(query, filter, limit):
                if filter == "videos":
                    return []
                title = query.removeprefix("First Artist ")
                return [raw(title, video_id="failed" if title == "Faulty" else "shared")]

            client.search.side_effect = search

            def add(client, playlist_id, video_id):
                if video_id == "failed":
                    raise RuntimeError("synthetic add failure")

            with patch.multiple(app, CSV_FILE=str(csv_path), OAUTH_FILE=str(oauth_path),
                                MATCH_FIXED_JSON=str(directory / "missing.json"),
                                MATCH_RESULTS_JSON=str(directory / "results.json"),
                                MATCH_RESULTS_XLSX=str(directory / "report.xlsx")), patch.object(
                app, "YTMusic", return_value=client,
            ), patch.object(app, "remove_all_from_playlist", return_value=0) as remove, patch.object(
                app, "add_to_playlist_with_retry", side_effect=add,
            ) as adding, patch.object(app.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                app.main()
            rows = json.loads((directory / "results.json").read_text(encoding="utf-8"))
            self.assertEqual([row["addition_status"] for row in rows], [
                "added", "skipped_duplicate_video_id", "add_failed_or_unknown",
                "skipped_duplicate_csv", "not_attempted",
            ])
            self.assertEqual(adding.call_count, 2)
            remove.assert_called_once()
            workbook = load_workbook(directory / "report.xlsx")
            self.assertEqual(workbook.worksheets[0].max_row, 6)
            self.assertEqual(workbook.worksheets[1].max_row, 4)
            workbook.close()

    def test_clear_playlist_verifies_empty_and_counts_duplicate_entries(self):
        client = Mock()
        entries = [
            {"videoId": "same-song", "setVideoId": "entry-1"},
            {"videoId": "same-song", "setVideoId": "entry-2"},
        ]
        client.get_playlist.side_effect = [
            {"owned": True, "tracks": entries, "trackCount": 2},
            {"owned": True, "tracks": [], "trackCount": 0},
        ]
        client.remove_playlist_items.return_value = "STATUS_SUCCEEDED"
        with patch.object(app.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.remove_all_from_playlist(client, "playlist"), 2)
        client.remove_playlist_items.assert_called_once_with("playlist", entries)
        self.assertEqual(client.get_playlist.call_count, 2)
        client.get_playlist.assert_called_with("playlist", limit=None)

    def test_clear_playlist_rejects_noneditable_or_unverifiable_contents(self):
        entry = {"videoId": "song", "setVideoId": "entry"}
        cases = [
            ({"owned": False, "tracks": [entry]}, "owned=False"),
            ({"tracks": [{"videoId": "song"}]}, "lack videoId or setVideoId"),
            ({"tracks": [entry, {"setVideoId": "unavailable"}]}, "lack videoId or setVideoId"),
            ({"tracks": [], "trackCount": 487}, "reports remaining items"),
            ({"trackCount": 0}, "tracks list is missing"),
        ]
        for playlist, message in cases:
            with self.subTest(playlist=playlist):
                client = Mock()
                client.get_playlist.return_value = playlist
                with self.assertRaisesRegex(RuntimeError, message):
                    app.remove_all_from_playlist(client, "playlist")
                client.remove_playlist_items.assert_not_called()

    def test_clear_playlist_accepts_repeated_empty_reads_with_stale_count(self):
        entries = [{"videoId": "song", "setVideoId": "entry"}]
        empty = {"owned": True, "tracks": [], "trackCount": 487}
        # Cover both the deletion run and restarting after deletion succeeded.
        for initial in ([{"owned": True, "tracks": entries}], []):
            with self.subTest(restart=not initial):
                client = Mock()
                client.get_playlist.side_effect = initial + [empty, empty, empty]
                client.remove_playlist_items.return_value = "STATUS_SUCCEEDED"
                with patch.object(app.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(app.remove_all_from_playlist(client, "playlist"), len(initial))
                self.assertEqual(client.get_playlist.call_count, len(initial) + 3)
                self.assertEqual(client.remove_playlist_items.call_count, len(initial))

    def test_clear_playlist_waits_for_header_count_to_update(self):
        client = Mock()
        client.get_playlist.side_effect = [
            {"owned": True, "tracks": [], "trackCount": 487},
            {"owned": True, "tracks": [], "trackCount": 0},
        ]
        with patch.object(app.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.remove_all_from_playlist(client, "playlist"), 0)
        self.assertEqual(client.get_playlist.call_count, 2)
        client.remove_playlist_items.assert_not_called()

    def test_clear_playlist_removes_tracks_that_reappear_during_verification(self):
        client = Mock()
        entries = [{"videoId": "song", "setVideoId": "entry"}]
        client.get_playlist.side_effect = [
            {"owned": True, "tracks": [], "trackCount": 1},
            {"owned": True, "tracks": entries, "trackCount": 1},
            {"owned": True, "tracks": [], "trackCount": 0},
        ]
        client.remove_playlist_items.return_value = "STATUS_SUCCEEDED"
        with patch.object(app.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.remove_all_from_playlist(client, "playlist"), 1)
        client.remove_playlist_items.assert_called_once_with("playlist", entries)

    def test_clear_playlist_revalidates_each_empty_read(self):
        for invalid in ({"owned": False, "tracks": []}, {"owned": True, "trackCount": 0},
                        {"tracks": [], "trackCount": 487}):
            with self.subTest(invalid=invalid):
                client = Mock()
                client.get_playlist.side_effect = [
                    {"owned": True, "tracks": [], "trackCount": 487}, invalid,
                ]
                with patch.object(app.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(RuntimeError):
                        app.remove_all_from_playlist(client, "playlist")
                client.remove_playlist_items.assert_not_called()

    def test_clear_playlist_checks_response_and_actual_removal(self):
        playlist = {"tracks": [{"videoId": "song", "setVideoId": "entry"}]}
        for response in ("STATUS_FAILED", {"status": "STATUS_FAILED"}, {}, None, "STATUS_SUCCEEDED"):
            with self.subTest(response=response):
                client = Mock()
                client.get_playlist.return_value = playlist
                client.remove_playlist_items.return_value = response
                with patch.object(app.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(RuntimeError, "not confirm|not confirmed"):
                        app.remove_all_from_playlist(client, "playlist")
                client.remove_playlist_items.assert_called_once()

    def test_clear_playlist_retries_conflicting_batch_individually(self):
        client = Mock()
        entries = [{"videoId": "song", "setVideoId": "entry"}]
        client.get_playlist.side_effect = [{"tracks": entries}, {"tracks": []}]
        client.remove_playlist_items.side_effect = [
            RuntimeError("HTTP 409"), {"status": "STATUS_SUCCEEDED"},
        ]
        with patch.object(app.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.remove_all_from_playlist(client, "playlist"), 1)
        self.assertEqual(client.remove_playlist_items.call_count, 2)

    def test_clear_playlist_verifies_the_last_allowed_round(self):
        client = Mock()
        entries = [{"videoId": "song", "setVideoId": str(index)} for index in range(8)]
        client.get_playlist.side_effect = [{"tracks": entries[index:]} for index in range(9)]
        client.remove_playlist_items.return_value = "STATUS_SUCCEEDED"
        with patch.object(app.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.remove_all_from_playlist(client, "playlist"), 8)
        self.assertEqual(client.remove_playlist_items.call_count, 8)

    def test_main_stops_before_adding_when_playlist_cannot_be_cleared(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temporary:
            directory = Path(temporary)
            fixed_path = directory / "fixed.json"
            fixed_path.write_text("[]", encoding="utf-8")
            json_path = directory / "results.json"
            excel_path = directory / "report.xlsx"
            json_path.write_text('[{"previous": true}]', encoding="utf-8")
            app.export_excel_report([], excel_path)
            previous_json = json_path.read_bytes()
            previous_excel = excel_path.read_bytes()
            client = Mock()
            client.get_playlist.return_value = {"owned": False, "tracks": [{"videoId": "song"}]}
            with patch.multiple(app, MATCH_FIXED_JSON=str(fixed_path), OAUTH_FILE=str(fixed_path),
                                MATCH_RESULTS_JSON=str(json_path), MATCH_RESULTS_XLSX=str(excel_path)), patch.object(
                app, "persist_results",
            ) as persist, patch.object(app, "YTMusic", return_value=client), patch.object(
                app, "add_to_playlist_with_retry",
            ) as adding, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "owned=False"):
                    app.main()
            adding.assert_not_called()
            persist.assert_not_called()
            client.remove_playlist_items.assert_not_called()
            self.assertEqual(json_path.read_bytes(), previous_json)
            self.assertEqual(excel_path.read_bytes(), previous_excel)

    def test_report_write_failure_prevents_playlist_initialization(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temporary:
            fixed_path = Path(temporary) / "fixed.json"
            fixed_path.write_text("[]", encoding="utf-8")
            with patch.multiple(app, MATCH_FIXED_JSON=str(fixed_path), OAUTH_FILE=str(fixed_path)), patch.object(
                app, "verify_report_destinations", side_effect=PermissionError("report locked"),
            ), patch.object(app, "YTMusic") as client:
                with self.assertRaises(PermissionError):
                    app.main()
                client.assert_not_called()

    def test_main_never_adds_after_ai_returns_none(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temporary:
            directory = Path(temporary)
            csv_path = directory / "synthetic.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["Track Name", "Artist Name(s)", "Duration (ms)"])
                writer.writeheader()
                writer.writerow({"Track Name": "Song Name", "Artist Name(s)": "First Artist", "Duration (ms)": 180000})
            oauth_path = directory / "fake_oauth.json"
            oauth_path.write_text("{}", encoding="utf-8")
            client = self.fake_search([raw(artist="Unknown", duration=190)])
            response = {"decision": "none", "videoId": None, "reason": "different recording"}
            with patch.multiple(app, CSV_FILE=str(csv_path), OAUTH_FILE=str(oauth_path),
                                MATCH_FIXED_JSON=str(directory / "missing.json"),
                                MATCH_RESULTS_JSON=str(directory / "results.json"),
                                MATCH_RESULTS_XLSX=str(directory / "report.xlsx"), AI_MATCH_ENABLED=True), patch.object(
                app, "YTMusic", return_value=client,
            ), patch.object(app, "remove_all_from_playlist", return_value=0), patch.object(
                app, "add_to_playlist_with_retry",
            ) as adding, patch.object(app, "run_lm_studio_json_prompt", return_value=(response, None)) as ai, patch.object(
                app.time, "sleep",
            ), contextlib.redirect_stdout(io.StringIO()):
                app.main()
            ai.assert_called_once()
            adding.assert_not_called()
            rows = json.loads((directory / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(rows[0]["decision"]["status"], "no_match")
            self.assertEqual(rows[0]["addition_status"], "not_attempted")

    def test_import_does_not_run_main(self):
        self.assertTrue(callable(app.main))
        self.assertFalse(app.AI_MATCH_REASONING_ENABLED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
