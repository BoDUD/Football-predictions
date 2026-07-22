import importlib.util
import ctypes
from pathlib import Path
import tempfile
import unittest

from PIL import Image


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "wechat_push.py"
SPEC = importlib.util.spec_from_file_location("wechat_push", SCRIPT)
wechat_push = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(wechat_push)


class WeChatPushTests(unittest.TestCase):
    class FakeEditor:
        def __init__(self, value=""):
            self.value = value

        def get_value(self):
            return self.value

        def set_edit_text(self, value):
            self.value = value

    def test_lineup_message_is_bound_to_match_id(self):
        text = wechat_push.validate_event_message(
            "lineup-check:2999990",
            "【临场分析｜2999990】\n比赛：A vs B\n检查时间：02:30 JST\n比赛状态：赛前\n主推维持：是\n主推：小2.5\n比分参考：1-0、1-1",
        )
        self.assertIn("\n主推：小2.5\n", text)
        with self.assertRaises(ValueError):
            wechat_push.validate_event_message(
                "lineup-check:2999990",
                "【临场分析｜2907406】\n比赛：A vs B\n检查时间：02:30 JST\n比赛状态：赛前\n主推维持：是\n主推：小2.5\n比分参考：1-0、1-1",
            )

    def test_initial_plain_text_format(self):
        text = wechat_push.validate_event_message(
            "initial:2999990",
            "【初盘分析｜2999990】\n比赛：A vs B\n开赛：03:00 JST\n主推：A 0\n比分参考：1-0、1-1",
        )
        self.assertTrue(text.startswith("【初盘分析｜2999990】\n"))
        with self.assertRaises(ValueError):
            wechat_push.validate_event_message(
                "initial:2999990",
                "# 初盘\n比赛：A vs B\n开赛：03:00 JST\n主推：A 0\n比分参考：1-0、1-1",
            )

    def test_test_event_requires_visible_test_label(self):
        self.assertIn(
            "测试",
            wechat_push.validate_event_message("test:manual-1", "微信推送测试"),
        )
        with self.assertRaises(ValueError):
            wechat_push.validate_event_message("test:manual-1", "hello"),

    def test_visual_signature_rms(self):
        base = Image.new("RGB", (30, 20), (10, 20, 30))
        same = Image.new("RGB", (30, 20), (10, 20, 30))
        changed = Image.new("RGB", (30, 20), (220, 220, 220))
        self.assertEqual(wechat_push.image_rms(base, same), 0.0)
        self.assertGreater(wechat_push.image_rms(base, changed), 100.0)
        self.assertEqual(wechat_push.image_rms(base, Image.new("RGB", (10, 10))), float("inf"))

    def test_green_button_detection(self):
        image = Image.new("RGB", (100, 100), (230, 230, 230))
        self.assertEqual(wechat_push.green_fraction(image, (50, 50)), 0.0)
        green = Image.new("RGB", (100, 100), (20, 190, 120))
        self.assertGreater(wechat_push.green_fraction(green, (50, 50)), 0.9)

    def test_windows_input_structure_has_native_union_size(self):
        if wechat_push.os.name == "nt":
            self.assertEqual(ctypes.sizeof(wechat_push.Input), 40)

    def test_rpa_editor_stages_and_clears_exact_plain_text(self):
        editor = self.FakeEditor()
        message = "【初盘分析｜2999990】\n比赛：A vs B"
        self.assertFalse(wechat_push.prepare_rpa_editor(editor, message))
        self.assertEqual(editor.value, message)
        wechat_push.clear_rpa_editor(editor, message)
        self.assertEqual(editor.value, "")

    def test_rpa_editor_refuses_unknown_draft(self):
        editor = self.FakeEditor("用户自己的草稿")
        with self.assertRaises(RuntimeError):
            wechat_push.prepare_rpa_editor(editor, "自动消息")
        self.assertEqual(editor.value, "用户自己的草稿")

    def test_rpa_editor_adopts_only_exact_hash_and_message(self):
        message = "【临场分析｜2999990】\n主推维持：是"
        digest = wechat_push.hashlib.sha256(message.encode("utf-8")).hexdigest()
        self.assertTrue(wechat_push.prepare_rpa_editor(self.FakeEditor(message), message, digest))
        with self.assertRaises(RuntimeError):
            wechat_push.prepare_rpa_editor(self.FakeEditor(message + "改"), message, digest)


if __name__ == "__main__":
    unittest.main()
