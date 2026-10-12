"""Synthetic regression tests; no network, microphone, model or real clipboard."""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "windows-typeless-voice-input"
sys.path.insert(0, str(APP_ROOT / "src"))
sys.path.insert(0, str(APP_ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "benchmarks" / "scripts"))

from win_voice_input import app
import export_rime_dictionary as rime
import import_typeless_dictionary as importer
import run_asr_benchmark as asr
import run_rewrite_benchmark as rewrite
import smoke_volcengine_asr as protocol
import stream_volcengine_asr_benchmark as stream
import summarize_bad_feedback as feedback
import summarize_usage as usage


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="voice-input-test-", dir=os.environ.get("VOICE_INPUT_TEST_TMP")
        )
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.cfg = app.load_config()
        self.cfg["dictionary_paths"] = [str(self.work / "dictionary.local.jsonl")]
        self.cfg["dictionary_path"] = self.cfg["dictionary_paths"][0]
        for key in self.cfg["history"]:
            self.cfg["history"][key] = str(self.work / key)
        self.cfg["paste"]["suppress_output_when_disabled"] = True
        self.addCleanup(patch.stopall)
        patch.object(app, "log").start()
        patch.object(app.urllib.request, "urlopen", side_effect=AssertionError("Network forbidden")).start()
        patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden")).start()

    def test_deep_merge_preserves_other_fields(self):
        base = {"one": {"a": 1, "b": 2}}
        self.assertEqual(app.deep_merge(base, {"one": {"a": 3}}), {"one": {"a": 3, "b": 2}})
        self.assertEqual(base["one"]["a"], 1)

    def test_public_config_is_clipboard_first_and_env_only(self):
        cfg = app.load_config()
        self.assertEqual(cfg["paste"]["method"], "clipboard")
        self.assertIsNone(cfg["stt"]["secrets_file"])
        self.assertIsNone(cfg["llm"]["secrets_file"])
        for mode in cfg["modes"].values():
            self.assertTrue(app.resolve_path(mode["prompt_path"]).is_file())

    def test_public_vocabulary_is_synthetic(self):
        self.assertEqual([e["phrase"] for e in app.load_dictionary_entries(app.load_config())], ["示例项目", "示例设备"])

    def test_dictionary_create_update_delete(self):
        self.assertEqual(app.upsert_local_dictionary_entry(self.cfg, phrase="Demo", aliases=["dm"]), 1)
        self.assertEqual(app.upsert_local_dictionary_entry(self.cfg, phrase="demo", aliases=["new"]), 1)
        self.assertEqual(app.load_local_dictionary_entries(self.cfg)[0]["aliases"], ["new"])
        self.assertTrue(app.delete_local_dictionary_entry(self.cfg, "DEMO"))
        self.assertFalse(app.delete_local_dictionary_entry(self.cfg, "missing"))
        self.assertEqual(app.load_local_dictionary_entries(self.cfg), [])

    def test_dictionary_rejects_empty_phrase(self):
        with self.assertRaises(ValueError):
            app.upsert_local_dictionary_entry(self.cfg, phrase=" ")

    def test_dictionary_deduplicates(self):
        app.save_local_dictionary_entries(self.cfg, [{"phrase": "Demo"}, {"phrase": "demo"}])
        self.assertEqual(len(app.load_dictionary_entries(self.cfg)), 1)

    def test_dictionary_import_deduplicates_and_skips_comments(self):
        path = self.work / "input.txt"
        path.write_text("# Demo\nDemo\ndemo\nOther\n", encoding="utf-8")
        self.assertEqual([e["phrase"] for e in importer.read_entries(path, "demo")], ["Demo", "Other"])

    def test_rime_export_has_readback(self):
        output = self.work / "rime"
        rime.export_entries(
            [{"phrase": "示例项目", "aliases": ["demo"]}], output_dir=output,
            include_no_code=False, weight=100, formats={"dict-yaml", "custom-phrase"}
        )
        text = (output / rime.DICT_FILENAME).read_text(encoding="utf-8")
        self.assertIn("示例项目\tdemo\t100", text)
        self.assertTrue((output / rime.CUSTOM_PHRASE_FILENAME).exists())

    def test_result_recovery(self):
        store = app.ResultStore(self.cfg)
        store.save(text="Demo.", raw_transcript="Demo", mode="default", rewrite_degraded=False)
        self.assertEqual(store.load_text(), "Demo.")
        self.assertEqual(store.load()["raw_transcript"], "Demo")

    def test_usage_store_removes_top_level_text(self):
        store = app.UsageStore(self.cfg)
        store.append({"text": "demo", "transcript": "demo", "raw_transcript": "demo", "chars": 4})
        self.assertEqual(json.loads(store.path.read_text(encoding="utf-8")), {"chars": 4})

    def test_nested_secret_redaction(self):
        data = {"nested": [{"api_key": "demo", "password": "demo", "model": "demo"}]}
        redacted = app.redact_secrets(data)
        self.assertEqual(redacted["nested"][0]["api_key"], "***")
        self.assertEqual(redacted["nested"][0]["model"], "demo")

    def test_thinking_tag_removal(self):
        self.assertEqual(app.strip_qwen_thinking("<think>demo</think>正文"), "正文")

    def test_rewrite_rejects_empty_output(self):
        self.assertEqual(app.rewrite_safety_reasons("Demo", " "), ["empty_output"])

    def test_rewrite_rejects_answer_to_spoken_instruction(self):
        self.assertIn("source_instruction_output_answerish", app.rewrite_safety_reasons("请你写一个例子", "当然，以下是例子"))

    def test_rewrite_allows_edited_statement(self):
        self.assertEqual(app.rewrite_safety_reasons("示例项目已经完成", "示例项目已经完成。"), [])

    def test_disabled_enhancer_does_not_call_network(self):
        self.cfg["llm"]["enabled"] = False
        self.assertEqual(app.TextEnhancer(self.cfg).enhance("default", "Demo"), "Demo")
        app.urllib.request.urlopen.assert_not_called()

    def test_enhancer_uses_fallback_model(self):
        self.cfg["llm"]["model"] = "primary"
        self.cfg["llm"]["fallback_model"] = "fallback"
        enhancer = app.TextEnhancer(self.cfg)
        with patch.object(enhancer, "_chat", side_effect=[TimeoutError("demo"), "Demo."]) as chat:
            self.assertEqual(enhancer.enhance("default", "Demo"), "Demo.")
            self.assertEqual(chat.call_count, 2)
            self.assertEqual(enhancer.last_model, "fallback")

    def test_pipeline_failure_preserves_transcript_without_paste(self):
        pipeline = app.VoicePipeline(self.cfg, no_paste=True)
        with patch.object(pipeline.enhancer, "enhance", side_effect=TimeoutError("demo")):
            self.assertEqual(pipeline.process_text("default", "Demo"), "Demo")
        self.assertTrue(pipeline.result_store.load()["rewrite_degraded"])

    def test_text_pipeline_without_models(self):
        self.cfg["llm"]["enabled"] = False
        pipeline = app.VoicePipeline(self.cfg, no_paste=True)
        self.assertEqual(pipeline.process_text("default", "示例项目"), "示例项目")
        self.assertEqual(pipeline.result_store.load_text(), "示例项目")
        app.urllib.request.urlopen.assert_not_called()

    def test_asr_router_fallback_without_model_load(self):
        router = app.TranscriberRouter(self.cfg)
        with patch.object(router.primary, "transcribe", side_effect=TimeoutError("demo")), \
             patch.object(router.fallback, "transcribe", return_value="Demo"):
            self.assertEqual(router.transcribe(self.work / "fictional.wav"), "Demo")

    def test_hotkey_parse(self):
        self.assertEqual(app.parse_win32_hotkey("ctrl+m"), (0x4002, ord("M")))

    def test_hotkey_rejects_multiple_keys(self):
        with self.assertRaises(ValueError):
            app.parse_win32_hotkey("ctrl+m+n")

    def test_daemon_lock_readback_and_release(self):
        lock = app.DaemonLock(self.cfg)
        lock.acquire(mode="default")
        self.assertEqual(lock.load()["pid"], os.getpid())
        lock.release()
        self.assertFalse(lock.path.exists())

    def test_clipboard_only_uses_fake_clipboard(self):
        fake = Mock()
        with patch.dict(sys.modules, {"pyperclip": fake}):
            result = app.TextPaster(self.cfg).paste("Demo", clipboard_only=True)
        fake.copy.assert_called_once_with("Demo")
        self.assertFalse(result.pasted)

    def test_cer_normalization(self):
        self.assertEqual(asr.cer("你好，Demo。", "你好demo"), 0.0)
        self.assertAlmostEqual(asr.cer("甲乙", "甲丙"), 0.5)

    def test_cer_empty_reference(self):
        self.assertEqual(asr.cer("", ""), 0.0)
        self.assertEqual(asr.cer("", "Demo"), 1.0)

    def test_rewrite_score_synthetic_case(self):
        case = json.loads((ROOT / "examples/rewrite/cases.json").read_text(encoding="utf-8"))[0]
        source = (ROOT / "examples/rewrite/demo.txt").read_text(encoding="utf-8")
        score = rewrite.score_case(case, source, "示例项目的界面已完成。下一步测试示例设备的连接，并记录测试结果。")
        self.assertEqual(score["missing_keywords"], [])
        self.assertEqual(score["missing_terms"], [])
        self.assertGreater(score["score"], 0.9)

    def test_protocol_client_request_frame(self):
        frame = protocol.full_client_request({"demo": True})
        self.assertEqual(struct.unpack(">I", frame[4:8])[0], len(frame) - 8)
        self.assertEqual(json.loads(gzip.decompress(frame[8:])), {"demo": True})

    def test_protocol_server_frame(self):
        payload = gzip.compress(json.dumps({"result": {"text": "Demo"}}).encode())
        frame = protocol.frame_payload(protocol.make_header(9, 0, 1, 1), payload)
        self.assertEqual(protocol.extract_text(protocol.parse_server_message(frame)["payload"]), "Demo")

    def test_protocol_short_frame(self):
        self.assertIn("error", protocol.parse_server_message(b"demo"))

    def test_stream_chunking(self):
        self.assertEqual(stream.chunks(b"abcdefg", 3), [b"abc", b"def", b"g"])

    def test_usage_percentiles(self):
        self.assertEqual(usage.percentile([10, 20, 30], 0.5), 20)
        self.assertIsNone(usage.percentile([], 0.5))
        self.assertIsNone(usage.as_float("nan"))

    def test_feedback_summary_does_not_include_text(self):
        summary = feedback.summarize([{"category_slug": "punctuation", "text": "demo body", "chars": 4}], 1)
        self.assertEqual(summary["runs"], 1)
        self.assertEqual(summary["forbidden_key_rows"], 1)
        self.assertNotIn("demo body", json.dumps(summary))

    def test_cli_help_offline(self):
        with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as error:
            app.main(["--help"])
        self.assertEqual(error.exception.code, 0)
        self.assertIn("--dry-run", output.getvalue())
        app.urllib.request.urlopen.assert_not_called()

    def test_dry_run_does_not_create_history(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(["--dry-run"]), 0)
        self.assertFalse((APP_ROOT / "logs").exists())
        app.urllib.request.urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
