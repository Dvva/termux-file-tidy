#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tidy_core 的单元测试与端到端小样本"""

import os
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
            "/A\n    /x\n    *.jpg\n    - *skip*\n"
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

    def test_wildcard_and_regex_are_or(self):
        """裸行和 match: 混用时，任一条命中即可，不能 AND"""
        blocks, _, _ = parse(
            "/A\n"
            "    /x\n"
            "    com.xingin.xhs\n"
            "    match: Camera_1040g[0-9a-z]{20,}\n"
            "    match: Camera_XHS_[0-9]{14}\\.\n"
        )
        b = blocks[0]
        # 只命中裸行
        self.assertTrue(match_block(b, "Screenshot_com.xingin.xhs.jpg"))
        # 只命中 match: 正则
        self.assertTrue(match_block(b, "Camera_1040g0k031v5pbchj40005pb9g60864ons.jpg"))
        self.assertTrue(match_block(b, "Camera_XHS_17767757733131.jpg"))
        # 都不命中
        self.assertFalse(match_block(b, "IMG_20260101_123456.jpg"))


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

    def test_timestamp_and_rand(self):
        """{timestamp} 是秒级 10 位；{rand:3} 是 3 位随机数字"""
        from tidy_core import FileRef
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.jpg"
            p.write_text("x")
            ref = FileRef(path=p, stat=p.stat())

            ts = render("{timestamp}", ref)
            self.assertTrue(ts.isdigit())
            self.assertEqual(len(ts), 10)

            r = render("{rand:3}", ref)
            self.assertEqual(len(r), 3)
            self.assertTrue(r.isdigit())

            r5 = render("{rand:5}", ref)
            self.assertEqual(len(r5), 5)

    def test_btime(self):
        """创建时间：系统不支持时回退 mtime，且 %s 返回 Unix 秒"""
        from tidy_core import FileRef
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.jpg"
            p.write_text("x")
            ref = FileRef(path=p, stat=p.stat())

            self.assertRegex(render("{btime:%Y}", ref), r"^\d{4}$")
            epoch = render("{btime:%s}", ref)
            self.assertTrue(epoch.isdigit())
            # 没有 birthtime 时应与 mtime 一致
            self.assertEqual(epoch, render("{mtime:%s}", ref))

    def test_quote_for_command(self):
        """command 里的路径要加引号，否则文件名空格会被 shell 拆开"""
        from tidy_core import FileRef
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "有 空格 的 文件.mp4"
            p.write_text("x")
            ref = FileRef(path=p, stat=p.stat())

            plain = render("cmd {}", ref)
            quoted = render("cmd {}", ref, quote=True)

            self.assertIn("有 空格 的 文件.mp4", plain)
            self.assertNotIn("'", plain)
            self.assertIn("'", quoted)
            self.assertIn("有 空格 的 文件.mp4", quoted)


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

    def test_command_only_block_runs_in_place(self):
        """只有 command、没有移动/改名的块也要执行"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "videos"
            src.mkdir()
            (src / "a.txt").write_text("x")

            blocks, _, _ = parse(
                "/videos\n"
                "    action: command\n"
                "        cmd: echo {}\n"
            )
            files = scan([src], [])
            engine = Engine(root=root, dry_run=True)
            plan = engine.plan(blocks[0], files[0])
            self.assertIsNotNone(plan)
            self.assertEqual(plan.dst, files[0].path)
            self.assertIsNotNone(plan.command)

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

    def test_targets_to_skip(self):
        """目标在源内部才排除；目标本身是别的源时保留（链式规则）"""
        from tidy_core import targets_to_skip

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inside = root / "Pictures" / "Deepseek"
            outside = root / "收藏视频"
            inside.mkdir(parents=True)
            outside.mkdir()

            # outside 同时是源，不能被排除
            got = targets_to_skip([inside, outside],
                                  [root / "Pictures", outside])
            self.assertEqual(got, [inside])


    def test_depth_limits_recursion(self):
        """depth: 1 只看源目录这一层，不进子目录"""
        from tidy_core import attach_sources, owner_blocks

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "DL"
            (src / "sub").mkdir(parents=True)
            (src / "a.jpg").write_text("x")
            (src / "sub" / "b.jpg").write_text("y")

            blocks, _, _ = parse("/DL\n    /out\n    depth: 1\n")
            attach_sources(blocks, root)

            self.assertEqual(len(owner_blocks(blocks, src / "a.jpg")), 1)
            self.assertEqual(len(owner_blocks(blocks, src / "sub" / "b.jpg")), 0)


class TestProgress(unittest.TestCase):
    def test_bar_uses_spaces(self):
        """空白格用空格，不用 ░，任何终端都能正常显示"""
        from tidy_core import fmt_bar
        self.assertEqual(fmt_bar(0.0), " " * 20)
        self.assertEqual(fmt_bar(1.0), "█" * 20)
        s = fmt_bar(0.35)
        self.assertEqual(len(s), 20)
        self.assertEqual(s.count("█"), 7)
        self.assertLessEqual(set(s), {"█", " "})

    def test_line_finish_format(self):
        import io
        from tidy_core import Progress
        buf = io.StringIO()
        p = Progress("移动", 10, "个", stream=buf, force=False, groups=2)
        p.set_groups(2, 1)
        p.update(done=5, current="/DCIM/Screenshots")
        p.finish()
        out = buf.getvalue()
        self.assertIn("移动", out)
        self.assertIn("50%", out)
        self.assertIn("1/2 组", out)
        self.assertIn("5/10 个", out)
        self.assertIn("/DCIM/Screenshots", out)

    def test_block_layout_three_lines(self):
        import io
        from tidy_core import Progress
        buf = io.StringIO()
        p = Progress("压缩进度", 12, layout="block", group="视频",
                     stream=buf, force=False)
        p.update(done=9, failed=1, current="/x/a.mp4  (64.3 MB)",
                 extra="省 62.1 MB")
        p.finish(extra="省 62.1 MB")
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("压缩进度", lines[0])
        self.assertIn("75%", lines[0])
        self.assertIn("/x/a.mp4", lines[1])
        self.assertIn("省 62.1 MB", lines[1])
        self.assertIn("视频", lines[2])
        self.assertIn("9/12", lines[2])
        self.assertIn("失败 1", lines[2])

    def test_non_tty_prints_single_line(self):
        import io
        from tidy_core import Progress
        buf = io.StringIO()
        p = Progress("扫描", 2200, "个文件", stream=buf, force=False)
        p.update(done=2200)
        p.finish()
        self.assertEqual(len(buf.getvalue().splitlines()), 1)


class TestResume(unittest.TestCase):
    def test_roundtrip(self):
        from tidy_core import Resume
        with tempfile.TemporaryDirectory() as td:
            conf = Path(td) / "a.conf"
            conf.write_text("x")
            state = Path(td) / "state"

            r = Resume(state, conf)
            self.assertEqual(r.load(), 0)
            r.mark(Path("/a/b.mp4"), "compress-video", "ok", 100)
            r.close(success=False)

            r2 = Resume(state, conf)
            self.assertEqual(r2.load(), 1)
            self.assertTrue(r2.is_done(Path("/a/b.mp4")))
            self.assertFalse(r2.is_done(Path("/a/c.mp4")))
            r2.close(success=True)

            r3 = Resume(state, conf)
            self.assertEqual(r3.load(), 0, "正常结束后状态文件要删掉")

    def test_truncated_line_is_tolerated(self):
        """被 kill 时可能留下半行 JSON，下次不能因此崩掉"""
        from tidy_core import Resume
        with tempfile.TemporaryDirectory() as td:
            conf = Path(td) / "a.conf"
            conf.write_text("x")
            state = Path(td) / "state"
            r = Resume(state, conf)
            r.mark(Path("/a/b.mp4"), "compress-video", "ok")
            r.close(success=False)

            raw = r.path.read_text(encoding="utf-8")
            r.path.write_text(raw + '{"p": "/a/half', encoding="utf-8")

            r2 = Resume(state, conf)
            self.assertEqual(r2.load(), 1)

    def test_cleanup_tmp(self):
        from tidy_core import TMP_MARK, cleanup_tmp
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "sub").mkdir()
            (root / f".a{TMP_MARK}.mp4").write_text("x")
            (root / "sub" / f".b{TMP_MARK}.mp3").write_text("y")
            (root / "keep.mp4").write_text("z")

            self.assertEqual(cleanup_tmp([root], dry_run=True), 2)
            self.assertEqual(len(list(root.rglob(f"*{TMP_MARK}*"))), 2,
                             "dry-run 不能真删")

            self.assertEqual(cleanup_tmp([root]), 2)
            self.assertEqual(len(list(root.rglob(f"*{TMP_MARK}*"))), 0)
            self.assertTrue((root / "keep.mp4").exists())


class TestCompress(unittest.TestCase):
    def test_preserve_time(self):
        """preserve_time: true 时压缩后修改时间不变"""
        import shutil
        import subprocess

        from tidy_core import compress_file
        ff = shutil.which("ffmpeg")
        if ff is None:
            self.skipTest("没有 ffmpeg")

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.jpg"
            subprocess.run(
                [ff, "-y", "-loglevel", "error", "-f", "lavfi",
                 "-i", "testsrc2=size=1600x1200:rate=1", "-frames:v", "1",
                 "-q:v", "1", str(p)],
                check=True, timeout=60)
            old = 1600000000
            os.utime(p, (old, old))

            blocks, _, _ = parse(
                "/x\n"
                "    action: compress-image\n"
                "        quality: 31\n"
                "        preserve_time: true\n"
            )
            status, msg, _saved = compress_file(p, blocks[0].actions[0])
            if status != "ok":
                self.skipTest(f"这张图压不小：{msg}")
            self.assertAlmostEqual(p.stat().st_mtime, old, delta=1)

    def test_no_preserve_time(self):
        import shutil
        import subprocess

        from tidy_core import compress_file
        ff = shutil.which("ffmpeg")
        if ff is None:
            self.skipTest("没有 ffmpeg")

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.jpg"
            subprocess.run(
                [ff, "-y", "-loglevel", "error", "-f", "lavfi",
                 "-i", "testsrc2=size=1600x1200:rate=1", "-frames:v", "1",
                 "-q:v", "1", str(p)],
                check=True, timeout=60)
            old = 1600000000
            os.utime(p, (old, old))

            blocks, _, _ = parse(
                "/x\n"
                "    action: compress-image\n"
                "        quality: 31\n"
                "        preserve_time: false\n"
            )
            status, msg, _saved = compress_file(p, blocks[0].actions[0])
            if status != "ok":
                self.skipTest(f"这张图压不小：{msg}")
            self.assertGreater(p.stat().st_mtime, old + 86400 * 365,
                               "关闭保留时间后应该是当前时间")


class TestTabIndent(unittest.TestCase):
    def test_tab_is_error(self):
        _, issues, _ = parse("/A\n\t/x\n")
        msgs = " ".join(i.msg for i in issues if i.level == "error")
        self.assertIn("Tab", msgs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
