#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dịch chương bằng opencode CLI (dùng model miễn phí, không cần API key)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
DEFAULT_MODEL = "opencode/deepseek-v4.1-flash-free"
TIMEOUT = 420
ATTEMPTS = 3


def load_translator():
    spec = importlib.util.spec_from_file_location("dich_truyen", TOOLS_DIR / "dich_truyen.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def find_opencode() -> str:
    for name in ("opencode.cmd", "opencode.exe", "opencode"):
        found = shutil.which(name)
        if found:
            return found
    fallback = Path(os.environ.get("APPDATA", "")) / "npm" / "opencode.cmd"
    if fallback.exists():
        return str(fallback)
    raise SystemExit("Không tìm thấy opencode CLI trong PATH.")


def call_opencode(opencode: str, model: str, prompt: str, timeout: int) -> str:
    args = [opencode, "run", "-m", model, "--pure", "--log-level", "ERROR"]
    with tempfile.TemporaryDirectory(prefix="dich_opencode_") as tmp:
        proc = subprocess.run(
            args,
            input=prompt.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            cwd=tmp,
        )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"opencode trả mã {proc.returncode}: {stderr[:300]}")
    return proc.stdout.decode("utf-8", "replace").strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dịch truyện bằng opencode CLI")
    parser.add_argument("--start", type=int, default=None)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=int, default=TIMEOUT)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()
    dt = load_translator()
    chapters = dt.collect_chapters(BASE_DIR)

    pending = []
    for number, path in chapters:
        if args.start is not None and number < args.start:
            continue
        if args.end is not None and number > args.end:
            continue
        target = OUTPUT_DIR / f"chuong_{number}.txt"
        if target.exists() and target.stat().st_size >= 200 and not args.force:
            continue
        pending.append((number, path))
    if args.limit > 0:
        pending = pending[: args.limit]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Model: {args.model}")
    print(f"Cần dịch: {len(pending)} chương | workers={args.workers}")
    if args.dry_run:
        for number, _ in pending:
            print(f"  - chuong_{number}.txt")
        return 0
    if not pending:
        print("Không có chương nào cần dịch.")
        return 0

    opencode = find_opencode()
    glossary_data = dt.load_glossary()
    glossary = dt.flatten_glossary(glossary_data)
    state = dt.load_state(OUTPUT_DIR)
    lock = threading.Lock()
    counters = {"ok": 0, "fail": 0, "skip": 0, "index": 0}

    def process(number: int, path: Path) -> None:
        target = OUTPUT_DIR / f"chuong_{number}.txt"
        if target.exists() and target.stat().st_size >= 200 and not args.force:
            with lock:
                counters["skip"] += 1
            return
        with lock:
            counters["index"] += 1
            index = counters["index"]

        try:
            _, cn_title, source_body, source_url = dt.read_chapter(path)
            if not dt.CJK_RE.search(source_body):
                raise ValueError("Không thấy nội dung tiếng Trung")

            prev_tail = dt.previous_context(OUTPUT_DIR, number)
            feedback = ""
            last_error: Exception | None = None
            result = None
            for attempt in range(1, ATTEMPTS + 1):
                prompt = dt.build_prompt(number, cn_title, source_body, glossary, prev_tail, feedback)
                prompt = "Không dùng bất kỳ công cụ nào. Chỉ trả về bản dịch theo đúng định dạng.\n\n" + prompt
                try:
                    raw = call_opencode(opencode, args.model, prompt, args.timeout)
                    title, body, new_terms = dt.parse_translation(raw, number)
                    dt.validate_translation(source_body, body)
                    result = (title, body, new_terms)
                    break
                except Exception as exc:
                    last_error = exc
                    feedback = f"Lần trước chưa đạt: {exc}. Hãy dịch lại đầy đủ và đúng định dạng."
                    print(f"    [Ch.{number}] lần {attempt}/{ATTEMPTS}: {exc}", flush=True)
                    if attempt < ATTEMPTS:
                        time.sleep(2 * attempt)
            if result is None:
                raise RuntimeError(f"Thất bại sau {ATTEMPTS} lần: {last_error}")

            title, body, new_terms = result
            parts = [f"Chương {number}: {title}", "", body]
            if source_url:
                parts += ["", f"Nguồn: {source_url}"]
            dt.write_atomic(target, "\n".join(parts).rstrip() + "\n")

            with lock:
                added = 0
                for key, value in new_terms.items():
                    if key not in glossary:
                        glossary_data["auto"][key] = value
                        glossary[key] = value
                        added += 1
                if added:
                    dt.save_glossary(glossary_data)
                counters["ok"] += 1
                state["last_success"] = max(state["last_success"], number)
                if number in state["errors"]:
                    state["errors"].remove(number)
                dt.save_state(OUTPUT_DIR, state)
                print(f"[OK] {index}/{len(pending)} — Chương {number}: {title} "
                      f"({len(body)} ký tự, +{added} thuật ngữ)", flush=True)
        except Exception as exc:
            with lock:
                counters["fail"] += 1
                dt.append_error(OUTPUT_DIR, number, str(exc))
                if number not in state["errors"]:
                    state["errors"].append(number)
                dt.save_state(OUTPUT_DIR, state)
                print(f"[LỖI] {index}/{len(pending)} — Chương {number}: {exc}", flush=True)

    if args.workers <= 1:
        for number, path in pending:
            process(number, path)
            time.sleep(0.5)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = []
            for number, path in pending:
                futures.append(pool.submit(process, number, path))
                time.sleep(0.6)
            for future in as_completed(futures):
                future.result()

    print("\n" + "=" * 55)
    print(f"Hoàn tất: {counters['ok']} chương mới, {counters['fail']} lỗi, {counters['skip']} bỏ qua.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nĐã dừng. Chạy lại để tiếp tục.")
        raise SystemExit(130)
