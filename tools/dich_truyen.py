#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dịch truyện Trung Quốc sang tiếng Việt, mỗi chương một file.

Đặc điểm:
- Tự bỏ qua chương đã có bản dịch (không ghi đè).
- Một chương lỗi: ghi log rồi chạy tiếp chương sau.
- Chạy lại nhiều lần: chỉ dịch những chương còn thiếu.
- Tên riêng/thuật ngữ dùng chung tools/glossary.json để nhất quán.

Cấu hình API (theo thứ tự ưu tiên):
  1. Biến môi trường: ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, DICH_MODEL
  2. Khối "env" trong ~/.claude/settings.json

Chạy thử:
  python -I tools/dich_truyen.py --dry-run
  python -I tools/dich_truyen.py --limit 3
  python -I tools/dich_truyen.py --start 1 --end 50
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BASE_DIR
DEFAULT_OUTPUT = BASE_DIR / "output"
GLOSSARY_FILE = TOOLS_DIR / "glossary.json"

ERROR_LOG_NAME = "loi_dich.txt"
STATE_NAME = "tien_do_dich.json"

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
CJK_PUNCT_RE = re.compile(r"[，。；：！？、「」『』【】]")
CN_TITLE_RE = re.compile(r"^第\s*(\d+)\s*章\s*(.*)$")
VN_TITLE_RE = re.compile(r"^Chương\s*(\d+)\s*[:：\-–—]\s*(.+)$", re.IGNORECASE)
CN_NUM_RE = re.compile(r"^chuong_(\d+)\.txt$", re.IGNORECASE)

SYSTEM_PROMPT = """Bạn là dịch giả chuyên nghiệp, dịch tiểu thuyết tiên hiệp/kiếm hiệp Trung Quốc sang tiếng Việt.

Yêu cầu bắt buộc:
- Dịch trọn vẹn, trung thành, không tóm tắt, không bỏ sót câu hay đoạn nào.
- Văn phong tự nhiên, mượt mà, đúng không khí cổ trang tu tiên; câu văn Việt gọn và có nhịp.
- Giữ nguyên cách chia đoạn và toàn bộ lời thoại. Dùng ngoặc kép “ ” cho lời thoại.
- Tên riêng, địa danh, môn phái, cảnh giới, chiêu thức, đan dược, pháp bảo... dùng âm Hán-Việt và nhất quán tuyệt đối theo bảng thuật ngữ được cung cấp.
- Xưng hô theo đúng quan hệ và không khí cổ trang: ta/ngươi, huynh/đệ/muội/tỷ, tiền bối/vãn bối, sư phụ/đồ nhi, lão phu/lão hủ... Không dùng tôi/bạn một cách máy móc.
- Không thêm chú thích, lời bình, lời dẫn hay nội dung ngoài truyện.
- Không để sót chữ Hán hay dấu câu tiếng Trung trong bản dịch.
- Dòng đầu tiên phải là tiêu đề chương đã dịch, đúng dạng: Chương <số>: <tên chương>"""


class ValidationError(Exception):
    pass


# --------------------------------------------------------------------------
# Cấu hình API
# --------------------------------------------------------------------------

def _setting(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    settings = Path.home() / ".claude" / "settings.json"
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
        value = (data.get("env") or {}).get(name)
        return value or None
    except Exception:
        return None


def _api_config() -> tuple[str, str, str]:
    base = (_setting("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
    token = _setting("ANTHROPIC_AUTH_TOKEN") or _setting("ANTHROPIC_API_KEY") or ""
    model = (
        os.environ.get("DICH_MODEL")
        or _setting("ANTHROPIC_DEFAULT_SONNET_MODEL")
        or _setting("ANTHROPIC_MODEL")
        or "claude-sonnet-4-5"
    )
    if not token:
        raise SystemExit(
            "Không tìm thấy ANTHROPIC_AUTH_TOKEN. Hãy đặt biến môi trường "
            "hoặc cấu hình khối env trong ~/.claude/settings.json."
        )
    return base, token, model


class ApiClient:
    def __init__(self, base: str, token: str, model: str, timeout: int = 240):
        self.base = base
        self.token = token
        self.model = model
        self.timeout = timeout
        self.session = requests.Session()

    def _url(self) -> str:
        if self.base.endswith("/v1"):
            return self.base + "/messages"
        return self.base + "/v1/messages"

    def _headers(self, style: str) -> dict:
        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "accept": "application/json",
        }
        if style == "bearer":
            headers["authorization"] = f"Bearer {self.token}"
        else:
            headers["x-api-key"] = self.token
        return headers

    def complete(self, prompt: str, max_tokens: int) -> str:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0.35,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }
        last_error: Exception | None = None
        for style in ("bearer", "x-api-key"):
            try:
                response = self.session.post(
                    self._url(),
                    json=payload,
                    headers=self._headers(style),
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                break

            if response.status_code in (401, 403) and style == "bearer":
                last_error = RuntimeError(f"HTTP {response.status_code}")
                continue
            if response.status_code >= 400:
                snippet = response.text[:500].replace("\n", " ")
                raise RuntimeError(f"HTTP {response.status_code}: {snippet}")

            return self._extract_text(response)

        raise RuntimeError(f"Không gọi được API: {last_error}")

    @staticmethod
    def _extract_text(response: requests.Response) -> str:
        try:
            data = response.json()
        except ValueError:
            return response.text.strip()

        content = data.get("content")
        if isinstance(content, list):
            parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            text = "".join(parts).strip()
            if text:
                return text

        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") or {}
            text = (message.get("content") or "").strip()
            if text:
                return text

        raise RuntimeError(f"Phản hồi API không có nội dung: {str(data)[:300]}")


# --------------------------------------------------------------------------
# Đọc/ghi dữ liệu
# --------------------------------------------------------------------------

def load_glossary() -> dict:
    empty = {"_note": "Sửa trực tiếp tên/thuật ngữ tại đây. Script không ghi đè mục đã có.",
             "names": {}, "terms": {}, "auto": {}}
    if not GLOSSARY_FILE.exists():
        return empty
    try:
        data = json.loads(GLOSSARY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return empty
    for key in ("names", "terms", "auto"):
        data.setdefault(key, {})
    return data


def save_glossary(data: dict) -> None:
    tmp = GLOSSARY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, GLOSSARY_FILE)


def flatten_glossary(data: dict) -> dict[str, str]:
    merged: dict[str, str] = {}
    for key in ("names", "terms", "auto"):
        merged.update({k: v for k, v in (data.get(key) or {}).items() if k and v})
    return merged


def collect_chapters(input_dir: Path) -> list[tuple[int, Path]]:
    items: list[tuple[int, Path]] = []
    for path in input_dir.iterdir():
        match = CN_NUM_RE.fullmatch(path.name)
        if match and path.is_file():
            items.append((int(match.group(1)), path))
    items.sort(key=lambda item: item[0])
    return items


def read_chapter(path: Path) -> tuple[int | None, str, str, str | None]:
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()

    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines):
        raise ValueError("File rỗng")

    title = lines[index].strip()
    index += 1

    body_lines: list[str] = []
    source: str | None = None
    for line in lines[index:]:
        stripped = line.strip()
        if stripped.lower().startswith(("nguồn:", "nguon:")):
            source = stripped.split(":", 1)[1].strip() or None
            continue
        body_lines.append(line)

    body = "\n".join(body_lines).strip()
    if not body:
        raise ValueError("Không có nội dung truyện")

    match = CN_TITLE_RE.match(title)
    chapter_no = int(match.group(1)) if match else None
    cn_title = (match.group(2).strip() if match else title) or f"Chương {chapter_no or ''}".strip()
    return chapter_no, cn_title, body, source


def write_atomic(path: Path, content: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    os.replace(tmp, path)


def load_state(output_dir: Path) -> dict:
    path = output_dir / STATE_NAME
    default = {"last_success": 0, "done": 0, "errors": [], "updated_at": ""}
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        default.update({k: data.get(k, default[k]) for k in default})
    except Exception:
        pass
    return default


def save_state(output_dir: Path, state: dict) -> None:
    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    write_atomic(output_dir / STATE_NAME, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def append_error(output_dir: Path, chapter_no: int, message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} | Chương {chapter_no} | {message.replace(chr(10), ' ')[:500]}\n"
    with (output_dir / ERROR_LOG_NAME).open("a", encoding="utf-8") as handle:
        handle.write(line)


# --------------------------------------------------------------------------
# Dịch
# --------------------------------------------------------------------------

def previous_context(output_dir: Path, chapter_no: int, limit: int = 700) -> str:
    for number in range(chapter_no - 1, max(0, chapter_no - 4), -1):
        path = output_dir / f"chuong_{number}.txt"
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8").strip()
            except Exception:
                continue
            body = re.sub(r"\n+Nguồn:\s*\S+\s*$", "", text)
            return body[-limit:]
    return ""


def build_prompt(
    chapter_no: int,
    cn_title: str,
    body: str,
    glossary: dict[str, str],
    prev_tail: str,
    feedback: str = "",
) -> str:
    relevant = {k: v for k, v in glossary.items() if k in body}
    if len(relevant) < 20:
        for key, value in glossary.items():
            relevant.setdefault(key, value)
            if len(relevant) >= 60:
                break

    term_lines = "\n".join(f"- {key} → {value}" for key, value in sorted(relevant.items()))
    if not term_lines:
        term_lines = "(chưa có)"

    context_block = ""
    if prev_tail:
        context_block = (
            "\n[BẢN DỊCH CUỐI CHƯƠNG TRƯỚC — chỉ để giữ giọng văn và cách xưng hô, "
            "không dịch lại]\n" + prev_tail + "\n"
        )

    feedback_block = f"\n[LƯU Ý TỪ LẦN DỊCH TRƯỚC]\n{feedback}\n" if feedback else ""

    return f"""Dịch chương {chapter_no} của truyện tiên hiệp dưới đây sang tiếng Việt.

[BẢNG THUẬT NGỮ BẮT BUỘC — dùng đúng cách viết bên phải]
{term_lines}
{context_block}{feedback_block}
[TIÊU ĐỀ GỐC]
{cn_title}

[NỘI DUNG GỐC]
{body}

[ĐỊNH DẠNG TRẢ VỀ]
Dòng 1: Chương {chapter_no}: <tên chương đã dịch>
Dòng 2: để trống
Tiếp theo: toàn bộ nội dung đã dịch, giữ nguyên chia đoạn.

Cuối cùng, thêm khối sau để ghi nhận thuật ngữ mới (nếu không có thì ghi "không"):

<<<THUẬT_NGỮ_MỚI>>>
chữ Hán = cách dịch tiếng Việt
<<<HẾT>>>

Chỉ liệt kê tên riêng/thuật ngữ MỚI chưa có trong bảng trên, mỗi dòng một mục."""


def parse_translation(raw: str, chapter_no: int) -> tuple[str, str, dict[str, str]]:
    text = raw.strip()
    text = re.sub(r"^```(?:markdown|text|md)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)

    new_terms: dict[str, str] = {}
    marker = re.search(r"<<<\s*THUẬT[\s_]*NGỮ[\s_]*MỚI\s*>>>", text, re.IGNORECASE)
    if marker:
        head = text[: marker.start()].rstrip()
        tail = text[marker.end():]
        tail = re.split(r"<<<\s*HẾT\s*>>>", tail, maxsplit=1, flags=re.IGNORECASE)[0]
        for line in tail.splitlines():
            line = line.strip().lstrip("-•* ").strip()
            if not line or line.lower() in {"không", "khong", "none", "(không có)"}:
                continue
            parts = re.split(r"\s*(?:=|→|->|:)\s*", line, maxsplit=1)
            if len(parts) != 2:
                continue
            source, target = parts[0].strip(), parts[1].strip()
            if CJK_RE.search(source) and target and not CJK_RE.search(target) and len(target) <= 60:
                new_terms[source] = target
        text = head

    lines = text.splitlines()
    first_index = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first_index is None:
        raise ValidationError("Bản dịch rỗng")

    first_line = lines[first_index].strip()
    match = VN_TITLE_RE.match(first_line)
    if not match:
        raise ValidationError("Dòng đầu không đúng dạng 'Chương N: tên chương'")
    if int(match.group(1)) != chapter_no:
        raise ValidationError(f"Số chương ở tiêu đề ({match.group(1)}) không khớp ({chapter_no})")

    title = match.group(2).strip()
    body = "\n".join(lines[first_index + 1:]).strip()
    body = re.sub(r"\n+Nguồn:\s*https?://\S+\s*$", "", body).strip()
    if not body:
        raise ValidationError("Không có nội dung sau tiêu đề")
    return title, body, new_terms


def validate_translation(source_body: str, translated_body: str) -> None:
    if CJK_RE.search(translated_body):
        raise ValidationError("Bản dịch còn sót chữ Hán")
    if CJK_PUNCT_RE.search(translated_body):
        raise ValidationError("Bản dịch còn dấu câu tiếng Trung (，。！？...)")

    source_chars = len(CJK_RE.findall(source_body))
    if source_chars >= 50 and len(translated_body) < source_chars * 0.45:
        raise ValidationError(
            f"Bản dịch quá ngắn ({len(translated_body)} ký tự so với {source_chars} chữ Hán), "
            "có thể bị bỏ sót hoặc cắt cụt"
        )

    source_lines = sum(1 for line in source_body.splitlines() if line.strip())
    output_lines = sum(1 for line in translated_body.splitlines() if line.strip())
    if source_lines >= 5 and output_lines < source_lines * 0.5:
        raise ValidationError(
            f"Số đoạn bị thiếu ({output_lines} so với {source_lines}) — có thể đã tóm tắt"
        )


def translate_chapter(
    client: ApiClient,
    chapter_no: int,
    cn_title: str,
    source_body: str,
    glossary: dict[str, str],
    prev_tail: str,
    attempts: int = 3,
) -> tuple[str, str, dict[str, str]]:
    feedback = ""
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        prompt = build_prompt(chapter_no, cn_title, source_body, glossary, prev_tail, feedback)
        # Model proxy dành token cho phần suy luận trước khi xuất bản dịch,
        # nên cần ngân sách rộng; lượt sau tăng thêm nếu bị cắt cụt.
        max_tokens = min(32000, max(8000, int(len(source_body) * 2.2)) + (attempt - 1) * 4000)
        try:
            raw = client.complete(prompt, max_tokens)
        except Exception as exc:
            last_error = exc
            print(f"    (lần {attempt}/{attempts}) lỗi API: {exc}")
            if attempt < attempts:
                time.sleep(5 * attempt)
            continue

        try:
            title, body, new_terms = parse_translation(raw, chapter_no)
            validate_translation(source_body, body)
            return title, body, new_terms
        except ValidationError as exc:
            last_error = exc
            feedback = f"Lần trước chưa đạt: {exc}. Hãy dịch lại đầy đủ và đúng định dạng."
            print(f"    (lần {attempt}/{attempts}) {exc}")
            if attempt < attempts:
                time.sleep(2)

    raise RuntimeError(f"Thất bại sau {attempts} lần: {last_error}")


# --------------------------------------------------------------------------
# Chương trình chính
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dịch truyện Trung → Việt theo chương")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                        help="Thư mục chứa chuong_N.txt (mặc định: thư mục dự án)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="Thư mục lưu bản dịch (mặc định: output)")
    parser.add_argument("--start", type=int, default=None, help="Chương bắt đầu")
    parser.add_argument("--end", type=int, default=None, help="Chương kết thúc")
    parser.add_argument("--limit", type=int, default=0, help="Chỉ dịch tối đa N chương (0 = không giới hạn)")
    parser.add_argument("--force", action="store_true", help="Dịch lại và ghi đè file đã có")
    parser.add_argument("--dry-run", action="store_true", help="Chỉ liệt kê chương cần dịch")
    parser.add_argument("--no-source", action="store_true", help="Không ghi dòng Nguồn ở cuối file")
    parser.add_argument("--delay", type=float, default=0.4, help="Nghỉ giữa các chương (giây)")
    parser.add_argument("--workers", type=int, default=1, help="Số chương dịch song song (mặc định 1)")
    parser.add_argument("--model", default=None, help="Ghi đè model (mặc định lấy từ cấu hình)")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()
    input_dir: Path = args.input.resolve()
    output_dir: Path = args.output.resolve()

    if not input_dir.exists():
        print(f"Không tìm thấy thư mục input: {input_dir}")
        return 2

    chapters = collect_chapters(input_dir)
    if not chapters:
        print(f"Không có file chuong_N.txt trong: {input_dir}")
        return 2

    numbers = [number for number, _ in chapters]
    missing = [n for n in range(numbers[0], numbers[-1] + 1) if n not in set(numbers)]
    if missing:
        preview = ", ".join(map(str, missing[:30]))
        more = "..." if len(missing) > 30 else ""
        print(f"Cảnh báo: thiếu {len(missing)} chương: {preview}{more}")

    pending: list[tuple[int, Path]] = []
    for number, path in chapters:
        if args.start is not None and number < args.start:
            continue
        if args.end is not None and number > args.end:
            continue
        target = output_dir / f"chuong_{number}.txt"
        exists = target.exists() and target.stat().st_size >= 200
        if exists and not args.force:
            continue
        pending.append((number, path))

    if args.limit and args.limit > 0:
        pending = pending[: args.limit]

    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Input : {input_dir}")
    print(f"Output: {output_dir}")
    print(f"Tổng số chương có file: {len(chapters)} | cần dịch lần này: {len(pending)}")

    if args.dry_run:
        for number, _ in pending[:50]:
            print(f"  - chuong_{number}.txt")
        if len(pending) > 50:
            print(f"  ... và {len(pending) - 50} chương nữa")
        return 0

    if not pending:
        print("Không có chương nào cần dịch.")
        return 0

    base, token, model = _api_config()
    if args.model:
        model = args.model
    client = ApiClient(base, token, model)
    print(f"Model : {model}")

    glossary_data = load_glossary()
    glossary = flatten_glossary(glossary_data)
    state = load_state(output_dir)

    lock = threading.Lock()
    local = threading.local()
    counters = {"success": 0, "failed": 0, "skipped": 0, "index": 0}

    def worker_client() -> ApiClient:
        if args.workers <= 1:
            return client
        thread_client = getattr(local, "client", None)
        if thread_client is None:
            thread_client = ApiClient(base, token, model, timeout=600)
            local.client = thread_client
        return thread_client

    def process(number: int, path: Path) -> None:
        target = output_dir / f"chuong_{number}.txt"
        if target.exists() and target.stat().st_size >= 200 and not args.force:
            with lock:
                counters["skipped"] += 1
            return

        with lock:
            counters["index"] += 1
            index = counters["index"]

        try:
            _, cn_title, source_body, source_url = read_chapter(path)
            if not CJK_RE.search(source_body):
                raise ValueError("Không thấy nội dung tiếng Trung")

            prev_tail = previous_context(output_dir, number)
            title, body, new_terms = translate_chapter(
                worker_client(), number, cn_title, source_body, glossary, prev_tail
            )

            parts = [f"Chương {number}: {title}", "", body]
            if source_url and not args.no_source:
                parts += ["", f"Nguồn: {source_url}"]
            write_atomic(target, "\n".join(parts).rstrip() + "\n")

            with lock:
                added = 0
                for key, value in new_terms.items():
                    if key not in glossary:
                        glossary_data["auto"][key] = value
                        glossary[key] = value
                        added += 1
                if added:
                    save_glossary(glossary_data)

                counters["success"] += 1
                state["last_success"] = max(state["last_success"], number)
                state["done"] = state["done"] + 1 if parallel else counters["success"]
                if number in state["errors"]:
                    state["errors"].remove(number)
                save_state(output_dir, state)
                print(f"[OK] {index}/{len(pending)} — Chương {number}: {title} "
                      f"({len(body)} ký tự, +{added} thuật ngữ)")

        except Exception as exc:
            with lock:
                counters["failed"] += 1
                append_error(output_dir, number, str(exc))
                if number not in state["errors"]:
                    state["errors"].append(number)
                save_state(output_dir, state)
                print(f"[LỖI] {index}/{len(pending)} — Chương {number}: {exc} (đã ghi log, chạy tiếp)")

    parallel = args.workers > 1
    if parallel:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(process, number, path) for number, path in pending]
            for future in as_completed(futures):
                future.result()
    else:
        for number, path in pending:
            process(number, path)
            if args.delay > 0:
                time.sleep(args.delay)

    success = counters["success"]
    failed = counters["failed"]
    skipped = counters["skipped"]

    print("\n" + "=" * 55)
    print(f"Hoàn tất: {success} chương dịch mới, {failed} lỗi, {skipped} bỏ qua.")
    if failed:
        print(f"Xem chi tiết lỗi: {output_dir / ERROR_LOG_NAME}")
        print("Chạy lại cùng lệnh để thử lại các chương còn thiếu.")
    print(f"Thư mục bản dịch: {output_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nĐã dừng. Các chương đã dịch vẫn được giữ nguyên; chạy lại để tiếp tục.")
        raise SystemExit(130)
