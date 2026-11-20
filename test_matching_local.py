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


ROOT = Path(__file__).resolve().parent


def track(title="Song Name", artist="First Artist", duration=180000):
    return app.build_spotify_track({
        "Track Name": title, "Artist Name(s)": artist,
        "Duration (ms)": duration, "Track URI": "spotify:track:source123",
    })


def raw(title="Song Name", artist="First Artist", duration=180, video_id="candidate"):
    result = {"videoId": video_id, "title": title, "artists": [{"name": artist}]}
    if duration is not None:
        result["duration_seconds"] = duration
    return result


def evaluate(source=None, target=None):
    return app.evaluate_candidate_check(
        "song", source or track(), app.extract_candidate(target or raw(), "song", 1),
    )


class OfflineTests(unittest.TestCase):
    def setUp(self):
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
        client.search.side_effect = lambda query, filter, limit: list(songs if filter == "songs" else videos)
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

    def test_matching_artist_allows_far_or_missing_duration(self):
        for duration in (900, None, 0):
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
                self.assertTrue(evaluate(track(artist=source), raw(artist=target, duration=900))["strict_accepted"])

    def test_artist_boundaries(self):
        self.assertFalse(evaluate(track(artist="Art"), raw(artist="Artist", duration=900))["strict_accepted"])

    def test_artist_from_title_or_channel(self):
        title_result = evaluate(target=raw("First Artist - Song Name", "Unknown", 900))
        self.assertTrue(title_result["strict_accepted"])
        target = raw(artist="Unknown", duration=900)
        target["author"] = "First Artist Official"
        self.assertTrue(evaluate(target=target)["strict_accepted"])

    def test_any_spotify_artist_can_match(self):
        self.assertTrue(evaluate(track(artist="Other;First Artist"), raw(duration=900))["strict_accepted"])

    def test_versions_are_checked_separately(self):
        for source, target, expected in (
            ("Song Name", "Song Name (Live)", False),
            ("Song Name", "Song Name Remix", False),
            ("Song Name (Live)", "Song Name (Remix)", False),
            ("Song Name Remix", "Song Name Live", False),
            ("Song Name Live", "Song Name Live Remix", False),
            ("Song Name Live", "Song Name Live", True),
            ("Song Name Remix", "Song Name Remix", True),
            ("Song Name", "Song Name Alive", True),
            ("Song Name", "Song Name - רמיקס", False),
            ("Song Name", "Song Name בהופעה חיה", False),
        ):
            with self.subTest(source=source, target=target):
                self.assertEqual(evaluate(track(source), raw(target))["strict_accepted"], expected)

    def test_hebrew_and_unicode_names(self):
        self.assertTrue(evaluate(track("שיר אהבה", "אמן"), raw("אמן — שיר אהבה (Official Audio)", "אחר", 900))["strict_accepted"])
        self.assertTrue(evaluate(track("Déjà Vu", "Beyoncé"), raw("DEJA VU", "Beyonce", 900))["strict_accepted"])

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
        self.assertEqual(client.search.call_count, 2)

    def test_video_fallback(self):
        result = app.match_track(self.fake_search([raw("Wrong")], [raw(video_id="video")]), track())
        self.assertEqual(result["youtube"]["type"], "video")

    def test_empty_search(self):
        result = app.match_track(self.fake_search(), track())
        self.assertEqual(result["decision"]["status"], "no_match")
        self.assertEqual(result["search"]["checked_candidates"], [])

    def test_partial_search_failure_retains_candidates_and_error(self):
        client = self.fake_search()
        client.search.side_effect = [RuntimeError("search failed"), [raw(video_id="video")]]
        result = app.match_track(client, track())
        self.assertEqual(result["youtube"]["videoId"], "video")
        self.assertEqual(result["search"]["errors"][0]["stage"], "songs")

    def test_ai_uses_existing_candidates_without_research(self):
        client = self.fake_search([raw(video_id="first"), raw(video_id="second")])
        response = {"decision": "match", "videoId": "second", "reason": "same recording"}
        with patch.object(app, "AI_MATCH_ENABLED", True), patch.object(
            app, "run_lm_studio_json_prompt", return_value=(response, None),
        ) as ai:
            result = app.match_track(client, track())
        self.assertEqual(client.search.call_count, 2)
        self.assertEqual(result["youtube"]["videoId"], "second")
        prompt, model = ai.call_args.args
        self.assertEqual(model, "qwen/qwen3.8-27b")
        self.assertEqual(len(json.loads(prompt)["candidates"]), 2)
        self.assertEqual(result["search"]["checked_candidates"][1]["ai_candidate_decision"], "chosen")

    def test_invalid_ai_selection_none_or_outage_falls_back(self):
        for response, error in (
            ({"decision": "match", "videoId": "bad", "reason": "wrong"}, None),
            ({"decision": "match", "videoId": "invented", "reason": "wrong"}, None),
            ({"decision": "none", "videoId": None, "reason": "uncertain"}, None),
            (None, "offline"),
        ):
            with self.subTest(response=response, error=error), patch.object(app, "AI_MATCH_ENABLED", True), patch.object(
                app, "run_lm_studio_json_prompt", return_value=(response, error),
            ):
                result = app.match_track(self.fake_search([raw("Wrong", video_id="bad"), raw(video_id="good")]), track())
                self.assertEqual(result["youtube"]["videoId"], "good")
                self.assertEqual(result["search"]["ai"]["response"], response)

    def test_ai_not_called_without_eligible_candidates(self):
        with patch.object(app, "AI_MATCH_ENABLED", True), patch.object(app, "run_lm_studio_json_prompt") as ai:
            result = app.match_track(self.fake_search([raw("Song Name Remix")]), track())
            ai.assert_not_called()
            self.assertEqual(result["search"]["ai"]["status"], "skipped_no_eligible_candidates")

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
                return [raw(title, video_id="failed" if title == "faulty" else "shared")]

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

    def test_report_write_failure_prevents_playlist_initialization(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as temporary:
            fixed_path = Path(temporary) / "fixed.json"
            fixed_path.write_text("[]", encoding="utf-8")
            with patch.multiple(app, MATCH_FIXED_JSON=str(fixed_path), OAUTH_FILE=str(fixed_path)), patch.object(
                app, "persist_results", side_effect=PermissionError("report locked"),
            ), patch.object(app, "YTMusic") as client:
                with self.assertRaises(PermissionError):
                    app.main()
                client.assert_not_called()

    def test_import_does_not_run_main(self):
        self.assertTrue(callable(app.main))
        self.assertFalse(app.AI_MATCH_REASONING_ENABLED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
