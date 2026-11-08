#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bili-fix 的纯函数测试（不联网、不调 ffmpeg）"""

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("bili_fix", str(ROOT / "bin" / "bili-fix"))
SPEC = importlib.util.spec_from_loader("bili_fix", LOADER)
bili = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(bili)


class TestExtract(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(
            bili.extract_bvid("BV1kb4y1m7e7_《原神》角色演示.mp4"),
            "BV1kb4y1m7e7",
        )

    def test_lowercase(self):
        # 前缀统一规范成大写
        self.assertEqual(bili.extract_bvid("bv1kb4y1m7e7_x.mp4"), "BV1kb4y1m7e7")

    def test_missing(self):
        self.assertIsNone(bili.extract_bvid("普通视频.mp4"))

    def test_too_short(self):
        self.assertIsNone(bili.extract_bvid("BV1kb4y1m7e.mp4"))


class TestCleanTitle(unittest.TestCase):
    def test_brackets(self):
        self.assertEqual(
            bili.clean_title("【合集】标题（第一集）"),
            "【合集】标题[第一集]",
        )

    def test_illegal_chars(self):
        got = bili.clean_title('a/b\\c:d*e?f"g<h>i|j')
        self.assertNotIn("/", got)
        self.assertNotIn("\\", got)
        self.assertNotIn(":", got)

    def test_trailing_dots(self):
        self.assertEqual(bili.clean_title("  标题...  "), "标题")

    def test_empty_fallback(self):
        self.assertEqual(bili.clean_title("   "), "untitled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
