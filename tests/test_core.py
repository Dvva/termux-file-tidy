#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tidy_core 的单元测试与端到端小样本"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tidy_core import (  # noqa: E402
    Engine,
    expand_sources,
    match_block,
    parse,
    render,
    scan,
    wildcard_match,
)


class TestParse(unittest.TestCase):
    def test_basic(self):
        blocks, issues, _ = parse(
            "/DCIM/Screenshots\n"
            "    /archive/todo\n"
            "    com.microsoft.todos\n"
        )
        self.assertEqual([i for i in issues if i.level == "error"], [])
        self.assertEqual(len(blocks), 1)
        b = blocks[0]
        self.assertEqual(b.sources, ["/DCIM/Screenshots"])
        self.assertEqual(b.target, "/archive/todo")
        self.assertEqual(b.patterns, ["com.microsoft.todos"])

    def test_block_boundary_by_blank_line(self):
        blocks, _, _ = parse(
            "/A\n    /x\n\n/B\n    /y\n"
        )
        self.assertEqual(len(blocks), 2)

    def test_block_boundary_by_top_level(self):
        # 前一块已有目标，顶格行自动开新块
        blocks, _, _ = parse(
            "/A\n    /x\n/B\n    /y\n"
        )
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].target, "/x")
        self.assertEqual(blocks[1].sources, ["/B"])

    def test_multi_source_not_split(self):
        # 前一块还没有目标，顶格行算同块的另一个源
        blocks, _, _ = parse(
            "/A\n/B\n    /x\n"
        )
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].sources, ["/A", "/B"])

    def test_actions(self):
        blocks, _, _ = parse(
            "/A\n"
            "    /x\n"
            "    match: BV([0-9a-z]{4,})_\n"
            "    action: rename\n"
            "        replace: BV([0-9a-z]{4,})_\n"
            "        with:\n"
            "    action: command\n"
            "        cmd: echo {}\n"
        )
        b = blocks[0]
        self.assertEqual(len(b.actions), 2)
        self.assertEqual(b.actions[0].name, "rename")
        self.assertEqual(b.actions[0].get("replace"), "BV([0-9a-z]{4,})_")
        self.assertEqual(b.actions[1].name, "command")

    def test_skip(self):
        blocks, _, _ = parse("/A\n    /x\n    skip: true\n")
        self.assertTrue(blocks[0].skip)

    def test_clean(self):
        blocks, _, _ = parse("/DCIM\n    action: clean\n")
        self.assertTrue(blocks[0].clean)

    def test_errors(self):
        _, issues, _ = parse(
            "/A\n"
            "   /x\n"              # 3 空格缩进
            "    match: [0-9\n"     # 非法正则
            "    action: nope\n"    # 未知动作
            "    action: rename\n"
            "        bogus: 1\n"    # 不支持的参数
        )
        msgs = " ".join(i.msg for i in issues if i.level == "error")
        self.assertIn("缩进", msgs)
        self.assertIn("正则非法", msgs)
        self.assertIn("未知动作", msgs)
        self.assertIn("不支持参数", msgs)

    def test_two_targets(self):
        _, issues, _ = parse("/A\n    /x\n    /y\n")
        self.assertTrue(any("两个目标" in i.msg for i in issues))


class TestMatch(unittest.TestCase):
    def test_wildcard(self):
        self.assertTrue(wildcard_match("a.jpg", "*.jpg"))
        self.assertFalse(wildcard_match("a.png", "*.jpg"))
        self.assertTrue(wildcard_match("Screenshot_1.jpg", "Screenshot_*"))
        self.assertTrue(wildcard_match("x_com.tencent.mm_y", "com.tencent.mm"))
        self.assertFalse(wildcard_match("other", "com.tencent.mm"))

    def test_block_conditions(self):
        blocks, _, _ = parse(
            "/A\n    /x\n    *.jpg\n    match: ^IMG_[0-9]+\n    - *skip*\n"
        )
        b = blocks[0]
        self.assertTrue(match_block(b, "IMG_001.jpg"))
        self.assertFalse(match_block(b, "IMG_001.png"))
        self.assertFalse(match_block(b, "IMG_skip_1.jpg"))

    def test_or_semantics(self):
        blocks, _, _ = parse("/A\n    /x\n    aaa\n    bbb\n")
        b = blocks[0]
        self.assertTrue(match_block(b, "xxaaaxx"))
        self.assertTrue(match_block(b, "bbb"))
        self.assertFalse(match_block(b, "ccc"))


class TestRender(unittest.TestCase):
    def test_vars(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "Screenshot_1.jpg"
            p.write_text("x")
            st = p.stat()
            from tidy_core import FileRef
            ref = FileRef(path=p, stat=st, index=3, total=10)
            self.assertEqual(render("{name}", ref), "Screenshot_1")
            self.assertEqual(render("{ext}", ref), "jpg")
            self.assertEqual(render("{full}", ref), "Screenshot_1.jpg")
            self.assertEqual(render("{dir}", ref), Path(td).name)
            self.assertEqual(render("{index:03d}", ref), "003")
            self.assertEqual(render("{count}", ref), "10")
            self.assertRegex(render("{mtime:%Y}", ref), r"^\d{4}$")


class TestEngine(unittest.TestCase):
    def test_move_and_rename(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "DCIM" / "Screenshots"
            src.mkdir(parents=True)
            (src / "shot_com.deepseek.chat.jpg").write_text("a")

            text = (
                "/DCIM/Screenshots\n"
                "    /Pictures/Deepseek\n"
                "    com.deepseek.chat\n"
                "    action: rename\n"
                "        prefix: __\n"
            )
            blocks, issues, _ = parse(text)
            self.assertEqual([i for i in issues if i.level == "error"], [])

            files = scan([src], [])
            self.assertEqual(len(files), 1)

            engine = Engine(root=root)
            plan = engine.plan(blocks[0], files[0])
            self.assertIsNotNone(plan)
            self.assertEqual(plan.dst, root / "Pictures" / "Deepseek" / "__shot_com.deepseek.chat.jpg")

            res = engine.run_plan(plan, set())
            self.assertTrue(res.ok)
            self.assertTrue(plan.dst.exists())
            self.assertFalse((src / "shot_com.deepseek.chat.jpg").exists())

    def test_conflict_gets_suffix(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src"
            dst = root / "dst"
            src.mkdir()
            dst.mkdir()
            (src / "a.txt").write_text("new")
            (dst / "a.txt").write_text("old")

            text = "/src\n    /dst\n"
            blocks, _, _ = parse(text)
            files = scan([src], [])
            engine = Engine(root=root)
            plan = engine.plan(blocks[0], files[0])
            res = engine.run_plan(plan, set())
            self.assertTrue(res.ok)
            self.assertEqual(res.dst.name, "a (1).txt")
            self.assertEqual((dst / "a.txt").read_text(), "old")

    def test_dry_run_does_not_touch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src"
            src.mkdir()
            (src / "a.txt").write_text("x")

            blocks, _, _ = parse("/src\n    /dst\n")
            files = scan([src], [])
            engine = Engine(root=root, dry_run=True)
            plan = engine.plan(blocks[0], files[0])
            res = engine.run_plan(plan, set())
            self.assertTrue(res.ok)
            self.assertTrue((src / "a.txt").exists())
            self.assertFalse((root / "dst" / "a.txt").exists())

    def test_expand_sources(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Pictures" / "同人图A").mkdir(parents=True)
            (root / "Pictures" / "同人图B").mkdir(parents=True)
            got = expand_sources(["/Pictures/同人图*"], root)
            self.assertEqual(len(got), 2)

    def test_owner_priority_deeper_source_wins(self):
        """父目录的块不能抢走子目录块的文件"""
        from tidy_core import attach_sources, owner_blocks

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Pictures" / "Screenshots").mkdir(parents=True)
            f = root / "Pictures" / "Screenshots" / "a.png"
            f.write_text("x")

            blocks, _, _ = parse(
                "/Pictures\n    /out/parent\n\n"
                "/Pictures/Screenshots\n    /out/child\n"
            )
            attach_sources(blocks, root)
            owners = owner_blocks(blocks, f)
            self.assertEqual(len(owners), 2)
            # 更深的源排在前面
            self.assertEqual(owners[0].target, "/out/child")


class TestIgnore(unittest.TestCase):
    def test_parse_ignore(self):
        blocks, issues, ignores = parse(
            "ignore: /Pictures/.gs\n"
            "ignore: /Download/.csj\n"
            "\n"
            "/A\n"
            "    /x\n"
        )
        self.assertEqual(ignores, [("/Pictures/.gs", 1), ("/Download/.csj", 2)])
        self.assertEqual(len(blocks), 1)
        self.assertEqual([i for i in issues if i.level == "error"], [])

    def test_scan_skips_hidden_and_ignore(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Pictures" / ".gs").mkdir(parents=True)
            (root / "Pictures" / ".gs" / "a.jpg").write_text("x")
            (root / "Pictures" / "keep").mkdir()
            (root / "Pictures" / "keep" / "b.jpg").write_text("y")
            (root / "Pictures" / "ignoreme").mkdir()
            (root / "Pictures" / "ignoreme" / "c.jpg").write_text("z")

            files = scan([root / "Pictures"], [], [root / "Pictures" / "ignoreme"])
            self.assertEqual([f.path.name for f in files], ["b.jpg"])

            all_files = scan([root / "Pictures"], [], [], include_hidden=True)
            self.assertEqual(sorted(f.path.name for f in all_files),
                             ["a.jpg", "b.jpg", "c.jpg"])

    def test_scan_skips_ignored_source_root(self):
        """源目录本身被 ignore 时，整个源都跳过"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "同人图"
            src.mkdir()
            (src / "a.jpg").write_text("x")

            files = scan([src], [], [src])
            self.assertEqual(files, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
