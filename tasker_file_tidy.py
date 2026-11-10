#!/data/data/com.termux/files/usr/bin/python3
# -*- coding: utf-8 -*-
"""
termux-file-tidy 命令行入口

用法：
    tify                                   # 按默认配置执行（有上次进度会接着做）
    tify blocks                            # 只打印配置，不执行
    tify --dry-run                         # 只预览，不动文件
    tify --only 3                          # 只跑第 3 个规则块
    tify --scheduled                       # 定时任务模式：压缩块只跑一部分
    tify --fresh                           # 忽略上次进度，从头来
    tify --undo                            # 撤销上一次运行
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from tidy_core import (
    COMPRESS_ACTIONS,
    DEFAULT_ROOT,
    ERROR,
    SCHEDULED_LIMIT,
    Engine,
    Progress,
    Resume,
    _human_size,
    attach_sources,
    clean_empty_dirs,
    cleanup_tmp,
    compress_file,
    dedupe_block,
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

# 状态目录：日志、undo 记录、续传进度。可用环境变量覆盖，方便测试
STATE_DIR = Path(os.environ.get(
    "FILE_TIDY_STATE",
    str(Path.home() / ".local" / "state" / "file_tidy"),
))

# 压缩动作对应的中文短名，进度条和汇总都用它
KIND = {
    "compress-image": "图片",
    "compress-music": "音乐",
    "compress-video": "视频",
}

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
# 小工具
# ============================================================


def progress_force(args: argparse.Namespace) -> bool | None:
    """进度条是否强制开/关；None 交给 Progress 自己看 stderr 是不是终端"""
    if args.progress is True:
        return True
    if args.progress is False:
        return False
    if args.dry_run:
        # dry-run 要逐条打印计划，动态进度条会把输出冲乱，只留终态行
        return False
    return None


def _short_src(block) -> str:
    """块的代表路径，进度条上显示用"""
    if not block.sources:
        return ""
    if len(block.sources) == 1:
        return block.sources[0]
    return f"{block.sources[0]} 等 {len(block.sources)} 个源"


def _action_summary(block) -> str:
    """动作摘要，给 blocks 命令看"""
    names = []
    for a in block.actions:
        if a.name in COMPRESS_ACTIONS:
            kv = " ".join(f"{p.key}={p.value}" for p in a.params)
            names.append(f"{a.name}({kv})" if kv else a.name)
        else:
            names.append(a.name)
    if block.clean and "clean" not in names:
        names.append("clean")
    if block.target:
        names.append(f"-> {block.target}")
    return "  ".join(names)


def _block_label(block) -> str:
    """进度条左边的标签：移动 / 重命名 / 命令"""
    names = {a.name for a in block.actions}
    if "rename" in names and not block.target and not (names & {"move", "copy"}):
        return "重命名"
    if names and names <= {"command", "append"}:
        return "命令"
    return "移动"


def _is_dedupe(block) -> bool:
    return any(a.name == "dedupe" for a in block.actions)


def _has_compress(block) -> bool:
    return any(a.name in COMPRESS_ACTIONS for a in block.actions)


def _compress_action(block):
    return next((a for a in block.actions if a.name in COMPRESS_ACTIONS), None)


def _int_param(value: str) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def ask_compress_block(block, count: int, size_text: str) -> str:
    """终端里问一句要不要压这个块，返回 run / skip / all"""
    print(f"\n块 {block.line}  {_short_src(block)}   {count} 个文件 · {size_text}  "
          f"[{_compress_action(block).name}]")
    print("  执行？[Y/s/a]  回车=执行  s=跳过这个块  a=后面全部执行不再问")
    try:
        ans = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "run"
    if ans in ("s", "skip"):
        return "skip"
    if ans in ("a", "all"):
        return "all"
    return "run"


# ============================================================
# blocks：只打印配置
# ============================================================


def show_blocks(args: argparse.Namespace, config_path: Path) -> int:
    """打印解析后的规则块，一个文件都不动"""
    text = config_path.read_text(encoding="utf-8")
    blocks, issues, raw_ignores = parse(text, str(config_path))

    errors = [i for i in issues if i.level == ERROR]
    if errors:
        for i in errors:
            print(f"第 {i.line} 行 [错误] {i.msg}", file=sys.stderr)
        print(f"配置有 {len(errors)} 处错误。", file=sys.stderr)
        return 1
    for i in issues:
        print(f"第 {i.line} 行 [警告] {i.msg}", file=sys.stderr)

    root = Path(args.root).expanduser()
    if not root.is_absolute():
        root = Path(DEFAULT_ROOT)

    attach_sources(blocks, root)
    ignore_dirs = expand_ignores(raw_ignores, root)

    # 统计每个块能吃多少文件（只读扫描，不改任何东西）
    all_sources = [d for b in blocks for d in b.dirs]
    targets = [to_real(b.target, root) for b in blocks if b.target]
    files = scan(all_sources, targets_to_skip(targets, all_sources),
                 ignore_dirs, args.include_hidden)
    counts: dict[int, int] = {}
    strict = [b for b in blocks if not b.skip]
    for f in files:
        for b in owner_blocks(strict, f.path):
            if b.clean:
                continue
            if _is_dedupe(b):
                # 去重块显示源目录里的文件总数（真正重复几个要算哈希才知道）
                counts[id(b)] = counts.get(id(b), 0) + 1
                break
            if not match_block(b, f.path.name):
                continue
            counts[id(b)] = counts.get(id(b), 0) + 1
            break

    print(f"配置  {config_path}")
    print(f"      {len(blocks)} 个规则块 · {len(all_sources)} 个扫描源 · "
          f"{len(files)} 个文件 · {len(ignore_dirs)} 个忽略目录")
    print()

    for i, b in enumerate(blocks, 1):
        flag = "（跳过）" if b.skip else ""
        n = counts.get(id(b), 0)
        print(f"  {i:>2}  {b.line:>3}行  {_short_src(b)}{flag}")
        print(f"         {_action_summary(b) or '（无动作）'}   {n} 个文件")
    return 0


# ============================================================
# 执行
# ============================================================


def group_files(active, files) -> dict[int, list]:
    """把文件按规则块分组：一个文件只归最深的那个匹配块"""
    buckets: dict[int, list] = {id(b): [] for b in active}
    for f in files:
        for b in owner_blocks(active, f.path):
            # clean 块负责收尾，dedupe 块整块处理，都不进逐文件循环
            if b.clean or _is_dedupe(b):
                continue
            if not match_block(b, f.path.name):
                continue
            buckets[id(b)].append(f)
            break
    return buckets


def run_dedupe(block, args, root, engine, resume, stats, force, ignore_dirs) -> None:
    """去重块：先按字节显示哈希进度，再按组显示搬运进度"""
    label = _short_src(block)
    hash_prog = Progress("去重", 1, "字节", human=True, force=force,
                         current=label, interval=0.5)
    seen = {"hash": False, "group": False}

    def on_hash(done: int, total: int) -> None:
        seen["hash"] = True
        hash_prog.total = max(total, 1)
        hash_prog.update(done=min(done, total), current=label)

    group_prog = Progress("去重", 1, "组", force=force, current=label,
                          interval=0.5)

    def on_group(done: int, total: int, moved: int) -> None:
        seen["group"] = True
        group_prog.total = max(total, 1)
        group_prog.update(done=done, current=label,
                          extra=f"重复 {moved} 个")

    groups_n, moved_n, records = dedupe_block(
        block, root, args.dry_run, ignore_dirs, args.include_hidden,
        on_hash=on_hash, on_group=on_group)

    # 没有任何同名同大小的候选时，两个回调都不会触发，就别画一条空进度条
    if seen["hash"]:
        hash_prog.finish()
    if seen["group"]:
        group_prog.finish(extra=f"重复 {moved_n} 个" if moved_n else "")

    if groups_n:
        log.info("去重：%d 组重复，移动 %d 个", groups_n, moved_n)
    stats["dedupe"] += moved_n
    if args.dry_run:
        for rec in records:
            _, src, dst = rec.split("\t")
            print(f"{src}  ->  {dst}（重复）")
    engine.undo_lines.extend(records)


def run_compress(block, comp, todo, args, resume, stats, saved_kind, force) -> None:
    """压缩块：独立进度条，三行布局（块 / 当前文件 / 计数）"""
    stage = comp.name
    kind = KIND.get(stage, "压缩")
    label = _short_src(block)

    pending = [f for f in todo if not resume.is_done(f.path)]
    already = len(todo) - len(pending)
    if already:
        log.info("%s 上次已处理 %d 个，跳过", label, already)

    # 先压旧的，定时任务被截断时每次都能往前推进
    pending.sort(key=lambda f: (f.stat.st_mtime, str(f.path)))

    # limit 只在定时任务模式生效（终端手动跑可以交互跳过，不硬截断）
    limit = _int_param(comp.get("limit"))
    if args.scheduled:
        cap = limit or SCHEDULED_LIMIT.get(stage, 0)
        if cap and len(pending) > cap:
            log.info("%s 定时任务本次只处理 %d 个（共 %d 个等待）",
                     label, cap, len(pending))
            pending = pending[:cap]

    if not pending:
        return

    # 终端里先问一句，定时任务直接跑
    if not args.scheduled and not args.dry_run and sys.stdin.isatty():
        size = sum(f.stat.st_size for f in pending)
        if not args.yes:
            ans = ask_compress_block(block, len(pending), _human_size(size))
            if ans == "skip":
                log.info("跳过压缩块 %s", label)
                stats["skipped_blocks"] += 1
                return
            if ans == "all":
                args.yes = True

    prog = Progress("压缩进度", len(pending),
                    layout="line" if args.dry_run else "block",
                    group=kind, force=force, interval=1.0)
    saved = 0
    failed = skipped_n = done = 0

    for f in pending:
        try:
            size = f.path.stat().st_size
        except OSError:
            continue
        prog.update(done=done, failed=failed, skipped=skipped_n,
                    current=f"{f.path}  ({_human_size(size)})",
                    extra=f"省 {_human_size(saved)}   {prog.eta_text()}".strip())
        status, msg, delta = compress_file(f.path, comp, args.dry_run)
        done += 1
        if status == "ok":
            stats["compressed"] += 1
            saved += delta
            saved_kind[kind] += delta
            if args.dry_run:
                print(f"{f.path}  ->  {msg}")
            else:
                log.info("压缩 %s：%s", f.path, msg)
        elif status == "skip":
            skipped_n += 1
            stats["compress_skipped"] += 1
            log.info("跳过压缩 %s：%s", f.path, msg)
        else:
            failed += 1
            stats["failed"] += 1
            log.error("压缩失败 %s：%s", f.path, msg)
        resume.mark(f.path, stage, status, delta)

    prog.update(done=done, failed=failed, skipped=skipped_n, force=True)
    prog.finish(extra=f"省 {_human_size(saved)}")


def run_plain(block, todo, args, engine, resume, stats, force, taken) -> None:
    """普通块：移动 / 复制 / 重命名 / 命令，一行进度条"""
    label = _block_label(block)
    src = _short_src(block)

    # 组 = 块内文件按所在目录分组，进度条上的 x/y 组
    left: dict[Path, int] = {}
    for f in todo:
        left[f.path.parent] = left.get(f.path.parent, 0) + 1

    prog = Progress(label, len(todo), "个", force=force,
                    current=src, groups=len(left))

    for i, f in enumerate(todo, 1):
        key = f.path.parent
        if resume.is_done(f.path):
            left[key] -= 1
            if left[key] == 0:
                prog.advance_group()
            prog.update(done=i, current=src, force=False)
            continue

        plan = engine.plan(block, f)
        if plan is not None:
            result = engine.run_plan(plan, taken)
            if result.ok:
                stats["moved"] += 1
                resume.mark(f.path, label, "ok")
                if args.dry_run:
                    print(f"{plan.src}  ->  {result.dst}")
            else:
                stats["failed"] += 1
                log.error("失败 %s：%s", plan.src, result.error)

        left[key] -= 1
        if left[key] == 0:
            prog.advance_group()
        prog.update(done=i, current=src, failed=stats["failed"],
                    force=False)

    prog.finish(extra="")


# ============================================================
# 主流程
# ============================================================


def run(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser()
    if not config_path.exists():
        print(f"[错误] 找不到配置文件：{config_path}", file=sys.stderr)
        return 2

    if args.command == "blocks":
        return show_blocks(args, config_path)

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
    for i in issues:
        log.warning("配置第 %d 行：%s", i.line, i.msg)

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

    force = progress_force(args)

    # ---------- 续传 ----------
    resume = Resume(STATE_DIR, config_path, enabled=not args.fresh,
                    writable=not args.dry_run)
    if args.fresh:
        resume.enabled = False
    previous = resume.load() if resume.enabled else 0
    if previous:
        print(resume.summary())
        # 上次被中断时很可能留下半截 .tidy-tmp，先清掉再开工
        tmp_dirs = [d for b in active if _has_compress(b) for d in b.dirs]
        n = cleanup_tmp(tmp_dirs, dry_run=args.dry_run)
        if n:
            print(f"清理上次残留的临时文件 {n} 个")
    stats = {"moved": 0, "failed": 0, "compressed": 0,
             "compress_skipped": 0, "dedupe": 0, "removed": 0,
             "skipped_blocks": 0}
    saved_kind = {"视频": 0, "图片": 0, "音乐": 0}

    engine = Engine(root=root, dry_run=args.dry_run, undo_path=undo_path)
    taken: set[Path] = set()

    try:
        # ---------- 扫描 ----------
        t0 = time.monotonic()
        files = scan(all_sources, scan_skip, ignore_dirs, args.include_hidden)
        scan_prog = Progress("扫描", len(files), "个文件", force=force)
        scan_prog.update(done=len(files), force=True)
        scan_prog.finish()
        log.info("扫描到 %d 个文件，%d 个规则块（跳过 %d），用时 %.1fs",
                 len(files), len(active), skipped, time.monotonic() - t0)

        buckets = group_files(active, files)

        # ---------- 去重（整块先跑，重复的先归位） ----------
        for b in active:
            if not _is_dedupe(b):
                continue
            run_dedupe(b, args, root, engine, resume, stats, force, ignore_dirs)

        # ---------- 逐块处理 ----------
        for b in active:
            if b.clean or _is_dedupe(b):
                continue
            todo = buckets.get(id(b)) or []
            if not todo:
                continue
            comp = _compress_action(b)
            if comp is not None:
                run_compress(b, comp, todo, args, resume, stats, saved_kind, force)
            else:
                run_plain(b, todo, args, engine, resume, stats, force, taken)

        # ---------- 清理空目录 ----------
        removed: list[Path] = []
        if clean_dirs:
            prog = Progress("清理", len(clean_dirs), "个目录", force=force)
            removed = clean_empty_dirs(
                clean_dirs, dry_run=args.dry_run, ignores=ignore_dirs,
                include_hidden=args.include_hidden,
                on_dir=lambda d, n: prog.update(
                    done=min(prog.done + 1, prog.total),
                    extra=f"删除 {n} 个空目录"),
            )
            prog.update(done=len(clean_dirs), extra=f"删除 {len(removed)} 个空目录",
                        force=True)
            prog.finish(extra=f"删除 {len(removed)} 个空目录")
        stats["removed"] = len(removed)

        engine.save_undo()
        resume.close(success=True)
    except BaseException:
        # 中断也要关掉状态文件，记录已经落盘，下次能接着做
        resume.close(success=False)
        raise

    # ---------- 汇总 ----------
    summary = (f"处理 {stats['moved']} 个，失败 {stats['failed']} 个，"
               f"压缩 {stats['compressed']} 个（跳过 {stats['compress_skipped']}），"
               f"去重 {stats['dedupe']} 个，清理空目录 {stats['removed']} 个")
    print(summary)

    total_saved = sum(saved_kind.values())
    if total_saved:
        parts = " / ".join(f"{k} {_human_size(v)}"
                           for k, v in saved_kind.items() if v)
        print(f"省空间 {_human_size(total_saved)} · {parts}")

    if args.dry_run:
        print("（dry-run，未改动任何文件）")
    print(f"（日志：{log_path}）")

    log.info(summary)

    if not args.no_notify:
        notify("文件分类完成", summary)

    return 0 if stats["failed"] == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tasker_file_tidy",
        description="按配置分类整理手机文件",
    )
    p.add_argument("command", nargs="?", default="run",
                   choices=["run", "blocks"],
                   help="run=执行（默认），blocks=只打印配置不执行")
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
    p.add_argument("--scheduled", action="store_true",
                   help="定时任务模式：压缩块按 limit 只跑一部分，不交互询问")
    p.add_argument("--fresh", action="store_true",
                   help="忽略上次未完成的进度，从头开始")
    p.add_argument("--yes", "-y", action="store_true",
                   help="压缩块不再询问，全部执行")
    p.add_argument("--progress", dest="progress", action="store_true", default=None,
                   help="强制显示进度条（默认只在终端里显示）")
    p.add_argument("--no-progress", dest="progress", action="store_false",
                   help="不显示进度条")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("已中断（进度已保存，下次接着做）", file=sys.stderr)
        return 130
    except Exception as exc:  # 兜底，避免定时任务静默失败
        logging.exception("未预期异常")
        print(f"[错误] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
