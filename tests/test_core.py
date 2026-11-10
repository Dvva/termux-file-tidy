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
    def test_music_skips_non_audio(self):
        """.txt / .jpg 这类混在音乐目录里的文件不能被塞给 ffmpeg"""
        from tidy_core import compress_file
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "歌词.txt"
            p.write_text("not audio", encoding="utf-8")
            blocks, _, _ = parse(
                "/x\n    action: compress-music\n        format: mp3\n")
            status, msg, saved = compress_file(p, blocks[0].actions[0])
            self.assertEqual(status, "skip")
            self.assertIn("不是音频", msg)
            self.assertEqual(saved, 0)
            self.assertTrue(p.exists())

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


class TestPngFlatten(unittest.TestCase):
    """带透明的 png 要垫白底转 jpg，不然透明区域会变黑"""

    def _ff(self):
        import shutil
        return shutil.which("ffmpeg") or shutil.which("ffprobe")

    def setUp(self):
        if self._ff() is None:
            self.skipTest("没有 ffmpeg")

    def _make_alpha_png(self, p: Path):
        """左半边全透明、右半边带彩色噪点（噪点让 png 足够大，转 jpg 才会变小）"""
        import subprocess
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "testsrc2=size=400x400:rate=1",
             "-vf", "format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)'"
             ":a='if(lt(X,200),0,255)'",
             "-frames:v", "1", str(p)],
            check=True, timeout=60)

    def _pixel(self, p: Path, x: int, y: int):
        import subprocess
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(p), "-vf",
             f"crop=1:1:{x}:{y},format=rgb24", "-f", "rawvideo", "-"],
            capture_output=True, timeout=60).stdout
        return tuple(out[:3])

    def _action(self, extra: str = ""):
        blocks, issues, _ = parse(
            "/x\n    action: compress-image\n        quality: 3\n" + extra)
        self.assertEqual([i for i in issues if i.level == "error"], [])
        return blocks[0].actions[0]

    def test_alpha_detected(self):
        from tidy_core import _has_alpha, _probe_image
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.png"
            self._make_alpha_png(p)
            pix, w, h = _probe_image(p)
            self.assertIn("a", pix)
            self.assertEqual((w, h), (400, 400))
            self.assertTrue(_has_alpha(pix))
            self.assertFalse(_has_alpha("rgb24"))

    def test_transparent_png_becomes_jpg_with_white_bg(self):
        from tidy_core import compress_file
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.png"
            self._make_alpha_png(p)
            status, msg, _ = compress_file(p, self._action())
            self.assertEqual(status, "ok", msg)

            jpg = p.with_suffix(".jpg")
            self.assertTrue(jpg.exists(), "透明 png 应该转成 jpg")
            self.assertFalse(p.exists(), "原 png 应该删掉")

            r, g, b = self._pixel(jpg, 5, 5)
            self.assertGreater(r, 240)
            self.assertGreater(g, 240)
            self.assertGreater(b, 240)

            # 右半边是不透明的彩色噪点，不应该被刷白
            r, g, b = self._pixel(jpg, 300, 200)
            self.assertFalse(r > 240 and g > 240 and b > 240)

    def test_flatten_false_keeps_png(self):
        from tidy_core import compress_file
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.png"
            self._make_alpha_png(p)
            status, msg, _ = compress_file(p, self._action("        flatten: false\n"))
            if status == "skip":
                self.skipTest(f"没压小：{msg}")
            self.assertEqual(status, "ok", msg)
            self.assertTrue(p.exists(), "关掉 flatten 就不该改格式")
            self.assertFalse(p.with_suffix(".jpg").exists())


class TestVideoScale(unittest.TestCase):
    """视频按视频网站口径分档：短边定档，只降不升"""

    def _vf(self, conf: str):
        from tidy_core import video_scale_filter
        blocks, issues, _ = parse("/v\n    action: compress-video\n" + conf)
        self.assertEqual([i for i in issues if i.level == "error"], [])
        return video_scale_filter(blocks[0].actions[0])

    def test_parse_resolution(self):
        from tidy_core import parse_resolution
        self.assertEqual(parse_resolution("1080p"), 1080)
        self.assertEqual(parse_resolution("720P"), 720)
        self.assertEqual(parse_resolution("4k"), 2160)
        self.assertEqual(parse_resolution("1080"), 1080)
        self.assertEqual(parse_resolution(""), 0)
        self.assertEqual(parse_resolution("abc"), 0)

    def test_no_filter_when_nothing_written(self):
        """不写分辨率档位就完全不缩放，只靠 crf 降质量"""
        self.assertIsNone(self._vf("        crf: 23\n"))

    def test_resolution_filter_structure(self):
        vf = self._vf("        resolution: 720p\n")
        self.assertIn("min(ih,720)", vf)
        self.assertIn("min(iw,720)", vf)
        self.assertIn("-2", vf)

    def test_max_edge_still_available(self):
        vf = self._vf("        max_edge: 1080\n")
        self.assertIn("force_original_aspect_ratio=decrease", vf)

    def _probe(self, p):
        import subprocess
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=60).stdout.strip()
        w, h = out.split(",")[:2]
        return int(w), int(h)

    def _make(self, p, size):
        import subprocess
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", f"testsrc2=size={size}:rate=10", "-t", "1",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", str(p)],
            check=True, timeout=120)

    def test_small_video_is_not_upscaled(self):
        """640x360 写 720p，压完还是 640x360（视频网站的规则：不放大）"""
        import shutil
        from tidy_core import compress_file
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            self.skipTest("没有 ffmpeg/ffprobe")

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "small.mp4"
            self._make(p, "640x360")
            blocks, _, _ = parse(
                "/v\n    action: compress-video\n        resolution: 720p\n"
                "        crf: 40\n")
            status, msg, _ = compress_file(p, blocks[0].actions[0])
            if status != "ok":
                self.skipTest(f"没压小：{msg}")
            self.assertEqual(self._probe(p), (640, 360),
                             "小于目标档位的视频不能被放大")

    def test_large_video_is_downscaled(self):
        """1920x1080 写 720p，压完是 1280x720"""
        import shutil
        from tidy_core import compress_file
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            self.skipTest("没有 ffmpeg/ffprobe")

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "big.mp4"
            self._make(p, "1920x1080")
            blocks, _, _ = parse(
                "/v\n    action: compress-video\n        resolution: 720p\n"
                "        crf: 40\n")
            status, msg, _ = compress_file(p, blocks[0].actions[0])
            if status != "ok":
                self.skipTest(f"没压小：{msg}")
            self.assertEqual(self._probe(p), (1280, 720))


if __name__ == "__main__":
    unittest.main(verbosity=2)
