#!/data/data/com.termux/files/usr/bin/python3
# -*- coding: utf-8 -*-
"""
termux-file-tidy 命令行入口

用法：
    python tasker_file_tidy.py                 # 按默认配置执行
    python tasker_file_tidy.py --dry-run       # 只预览，不动文件
    python tasker_file_tidy.py --only 3        # 只跑第 3 个规则块
    python tasker_file_tidy.py --undo          # 撤销上一次运行
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from tidy_core import (
    DEFAULT_ROOT,
    ERROR,
    Engine,
    attach_sources,
    clean_empty_dirs,
    expand_ignores,
    match_block,
    owner_blocks,
    parse,
    scan,
    targets_to_skip,
    to_real,
    undo,
)

DEFAULT_CONFIG = Path(__file__).with_name("tasker_file_tify.conf")

# 状态目录：日志和 undo 记录。可用环境变量覆盖，方便测试或自定义位置
STATE_DIR = Path(os.environ.get(
    "FILE_TIDY_STATE",
    str(Path.home() / ".local" / "state" / "file_tidy"),
))

log = logging.getLogger("file_tidy")


# ============================================================
# 日志与通知
# ============================================================


def setup_logging(verbose: bool) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = STATE_DIR / f"tidy_{datetime.now():%Y%m%d}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.DEBUG if verbose else logging.WARNING)
    sh.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root.addHandler(sh)

    return log_path


def notify(title: str, body: str) -> None:
    """Termux 通知，没有 termux-api 时静默跳过"""
    if not shutil.which("termux-notification"):
        return
    try:
        subprocess.run(
            ["termux-notification", "--title", title, "--content", body],
            check=False, timeout=10,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        pass


# ============================================================
# 主流程
# ============================================================


def run(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser()
    if not config_path.exists():
        print(f"[错误] 找不到配置文件：{config_path}", file=sys.stderr)
        return 2

    undo_path = STATE_DIR / "undo.log"

    if args.undo:
        ok, fail = undo(undo_path, dry_run=args.dry_run)
        print(f"撤销完成：成功 {ok}，失败 {fail}")
        return 0 if fail == 0 else 1

    log_path = setup_logging(args.verbose)

    text = config_path.read_text(encoding="utf-8")
    blocks, issues, raw_ignores = parse(text, str(config_path))

    errors = [i for i in issues if i.level == ERROR]
    if errors:
        for i in errors:
            print(f"第 {i.line} 行 [错误] {i.msg}", file=sys.stderr)
        print(f"配置有 {len(errors)} 处错误，已停止，未做任何操作。", file=sys.stderr)
        return 1

    root = Path(args.root).expanduser()
    if not root.is_absolute():
        root = Path(DEFAULT_ROOT)

    if args.only:
        if not (1 <= args.only <= len(blocks)):
            print(f"[错误] --only 超出范围（共 {len(blocks)} 个块）", file=sys.stderr)
            return 2
        blocks = [blocks[args.only - 1]]

    active = [b for b in blocks if not b.skip]
    skipped = len(blocks) - len(active)
    for b in blocks:
        if b.skip:
            log.info("跳过第 %d 行规则块（skip: true）", b.line)

    # 展开每个块的源目录，后面用来判断文件归属
    attach_sources(active, root)
    ignore_dirs = expand_ignores(raw_ignores, root)
    if ignore_dirs:
        log.info("忽略 %d 个目录", len(ignore_dirs))

    # 收集源目录与目标目录
    all_sources: list[Path] = []
    targets: list[Path] = []
    clean_dirs: list[Path] = []
    for b in active:
        all_sources.extend(b.dirs)
        if b.target:
            targets.append(to_real(b.target, root))
        if b.clean:
            clean_dirs.extend(b.dirs)

    # 只有「落在某个源目录内部」的目标才需要排除，避免刚移进去又被扫出来。
    # 目标本身是另一个块的源（链式规则，如 先移动到 X 再对 X 重命名）时不能排除。
    scan_skip = targets_to_skip(targets, all_sources)

    files = scan(all_sources, scan_skip, ignore_dirs, args.include_hidden)
    log.info("扫描到 %d 个文件，%d 个规则块（跳过 %d）", len(files), len(active), skipped)

    engine = Engine(root=root, dry_run=args.dry_run, undo_path=undo_path)
    taken: set[Path] = set()
    processed: set[Path] = set()
    moved = failed = 0

    for f in files:
        if f.path in processed:
            continue
        for b in owner_blocks(active, f.path):
            if b.clean:
                continue
            if not match_block(b, f.path.name):
                continue
            plan = engine.plan(b, f)
            if plan is None:
                break
            result = engine.run_plan(plan, taken)
            processed.add(f.path)
            if result.ok:
                moved += 1
                if args.dry_run:
                    print(f"{plan.src}  ->  {result.dst}")
            else:
                failed += 1
                log.error("失败 %s：%s", plan.src, result.error)
            break

    removed: list[Path] = []
    if clean_dirs:
        removed = clean_empty_dirs(clean_dirs, dry_run=args.dry_run,
                                   ignores=ignore_dirs,
                                   include_hidden=args.include_hidden)

    engine.save_undo()

    summary = (f"处理 {moved} 个，失败 {failed} 个，"
               f"清理空目录 {len(removed)} 个")
    print(summary)
    if args.dry_run:
        print("（dry-run，未改动任何文件）")
    log.info(summary)

    if not args.no_notify:
        notify("文件分类完成", summary)

    if args.verbose:
        print(f"日志：{log_path}")

    return 0 if failed == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tasker_file_tidy",
        description="按配置分类整理手机文件",
    )
    p.add_argument("--config", "-c", default=str(DEFAULT_CONFIG),
                   help="配置文件路径")
    p.add_argument("--dry-run", "-n", action="store_true",
                   help="只打印计划，不移动任何文件")
    p.add_argument("--undo", action="store_true",
                   help="撤销上一次运行（读取 undo.log）")
    p.add_argument("--only", type=int, metavar="N",
                   help="只执行第 N 个规则块，用于调试")
    p.add_argument("--root", default=DEFAULT_ROOT,
                   help=f"存储根目录（默认 {DEFAULT_ROOT}）")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="输出详细日志")
    p.add_argument("--no-notify", action="store_true",
                   help="不发送 Termux 通知")
    p.add_argument("--include-hidden", action="store_true",
                   help="也处理 . 开头的隐藏目录（默认跳过）")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except Exception as exc:  # 兜底，避免定时任务静默失败
        logging.exception("未预期异常")
        print(f"[错误] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
