"""Build and run a resumable, line-addressed source audit.

The reviewer credential is read only from ``MINICODE_AUDIT_API_KEY``. Source
text is sent to the configured OpenAI-compatible endpoint but is never copied
into the ledger; the ledger stores hashes, reviewed ranges, findings and usage.
Re-running the script skips ranges already reviewed for the current file hash.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Iterable

import httpx


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SUFFIXES = frozenset(
    {
        ".bat",
        ".cjs",
        ".cmd",
        ".css",
        ".html",
        ".js",
        ".json",
        ".mjs",
        ".nsh",
        ".ps1",
        ".py",
        ".sh",
        ".svg",
        ".toml",
        ".ts",
        ".tsx",
        ".yaml",
        ".yml",
    }
)
SOURCE_NAMES = frozenset({"Dockerfile"})
EXCLUDED_NAMES = frozenset(
    {
        ".mcp.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "uv.lock",
    }
)
EXCLUDED_SUFFIXES = (".min.js", ".min.css", ".tsbuildinfo")


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: str
    sha256: str
    bytes: int
    lines: int
    max_line_chars: int


@dataclass(frozen=True, slots=True)
class ReviewRange:
    path: str
    sha256: str
    start_line: int
    end_line: int

    @property
    def key(self) -> str:
        return f"{self.path}\0{self.sha256}\0{self.start_line}\0{self.end_line}"


def _tracked_source_paths() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT
    )
    paths = [Path(raw.decode("utf-8")) for raw in output.split(b"\0") if raw]
    return [
        path
        for path in paths
        if path.name not in EXCLUDED_NAMES
        and not path.name.endswith(EXCLUDED_SUFFIXES)
        and (path.suffix.lower() in SOURCE_SUFFIXES or path.name in SOURCE_NAMES)
    ]


def _read_source(path: Path) -> tuple[bytes, str, list[str]]:
    raw = (ROOT / path).read_bytes()
    if b"\0" in raw:
        raise ValueError(f"tracked source contains a null byte: {path}")
    text = raw.decode("utf-8")
    lines = [line.removesuffix("\r") for line in text.split("\n")] if text else []
    if text.endswith("\n"):
        lines.pop()
    return raw, text, lines


def build_inventory() -> tuple[list[SourceFile], dict[str, list[str]]]:
    inventory: list[SourceFile] = []
    contents: dict[str, list[str]] = {}
    for path in _tracked_source_paths():
        raw, _text, lines = _read_source(path)
        relative = path.as_posix()
        inventory.append(
            SourceFile(
                path=relative,
                sha256=hashlib.sha256(raw).hexdigest(),
                bytes=len(raw),
                lines=len(lines),
                max_line_chars=max((len(line) for line in lines), default=0),
            )
        )
        contents[relative] = lines
    return inventory, contents


def _range_chunks(
    source: SourceFile,
    lines: list[str],
    *,
    chunk_chars: int,
) -> Iterable[ReviewRange]:
    if not lines:
        yield ReviewRange(source.path, source.sha256, 0, 0)
        return
    start = 1
    size = 0
    for index, line in enumerate(lines, start=1):
        rendered_size = len(str(index)) + len(line) + 3
        if index > start and size + rendered_size > chunk_chars:
            yield ReviewRange(source.path, source.sha256, start, index - 1)
            start = index
            size = 0
        size += rendered_size
    yield ReviewRange(source.path, source.sha256, start, len(lines))


def _load_completed_ranges(ledger: Path) -> set[str]:
    if not ledger.exists():
        return set()
    completed: set[str] = set()
    for line_number, line in enumerate(ledger.read_text(encoding="utf-8").splitlines(), start=1):
        record = json.loads(line)
        if record.get("status") != "completed":
            continue
        for item in record.get("ranges", []):
            review_range = ReviewRange(
                path=str(item["path"]),
                sha256=str(item["sha256"]),
                start_line=int(item["start_line"]),
                end_line=int(item["end_line"]),
            )
            completed.add(review_range.key)
    return completed


def _batch_ranges(
    ranges: Iterable[ReviewRange],
    contents: dict[str, list[str]],
    *,
    batch_chars: int,
) -> list[list[ReviewRange]]:
    batches: list[list[ReviewRange]] = []
    current: list[ReviewRange] = []
    current_size = 0
    for review_range in ranges:
        lines = contents[review_range.path]
        selected = lines[
            max(0, review_range.start_line - 1) : review_range.end_line
        ]
        size = sum(len(line) + len(str(number)) + 3 for number, line in enumerate(
            selected, start=max(1, review_range.start_line)
        )) + len(review_range.path) + 160
        if current and current_size + size > batch_chars:
            batches.append(current)
            current = []
            current_size = 0
        current.append(review_range)
        current_size += size
    if current:
        batches.append(current)
    return batches


SYSTEM_PROMPT = """You are auditing every supplied source line of MiniCode, an AI coding harness.
Its control plane owns model calls, the agent loop, tool routing, approvals, durable state and observation.
Its execution plane owns restricted file, command and network execution. Use public Codex behavior as the quality bar.

Read every numbered line. Report only concrete, user-impacting defects supported by the supplied code: correctness,
state ownership, cancellation/timeout/recovery, concurrency isolation, permissions/sandbox escape, inaccurate tool
results, protocol/UI projection, accessibility, or dead/duplicated tool exposure. Do not request defensive checks,
speculative abstractions, style cleanup, or tests that merely mirror implementation. A finding must name the exact
file and line, show the failing trigger, and explain the observable impact. Return JSON only with this shape:
{"summary":"...","findings":[{"severity":"P0|P1|P2|P3","file":"path","line":1,"end_line":1,
"title":"...","evidence":"...","impact":"...","fix":"..."}]}. Return an empty findings array when no defect is present."""


def _render_batch(batch: list[ReviewRange], contents: dict[str, list[str]]) -> str:
    sections = [
        "Audit the following immutable source ranges. Line numbers before `|` are authoritative."
    ]
    for item in batch:
        sections.append(
            f"\n### {item.path} sha256={item.sha256} lines={item.start_line}-{item.end_line}"
        )
        if item.start_line == 0:
            sections.append("<empty file>")
            continue
        lines = contents[item.path][item.start_line - 1 : item.end_line]
        sections.extend(
            f"{number}|{line}"
            for number, line in enumerate(lines, start=item.start_line)
        )
    return "\n".join(sections)


def _parse_json_content(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1 :] if first_newline >= 0 else text
        if text.endswith("```"):
            text = text[:-3].rstrip()
    payload = json.loads(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("findings"), list):
        raise ValueError("review response must be an object with a findings array")
    return payload


def _review_batch(
    client: httpx.Client,
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    thinking: str,
    max_request_seconds: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if thinking != "omit":
        payload["thinking"] = {"type": thinking}
    url = base_url.rstrip("/") + "/chat/completions"
    started = time.monotonic()
    for attempt in range(9):
        if time.monotonic() - started >= max_request_seconds:
            raise TimeoutError("Source audit model batch exceeded its wall-clock limit")
        with client.stream(
            "POST", url,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        ) as response:
            if response.status_code < 400:
                content_parts: list[str] = []
                usage: dict[str, Any] = {}
                finish_reason = ""
                for line in response.iter_lines():
                    if time.monotonic() - started >= max_request_seconds:
                        raise TimeoutError("Source audit model batch exceeded its wall-clock limit")
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if isinstance(chunk.get("usage"), dict):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if isinstance(delta.get("content"), str):
                            content_parts.append(delta["content"])
                        finish_reason = choice.get("finish_reason") or finish_reason
                if finish_reason in {"length", "max_tokens", "max_completion_tokens"}:
                    raise ValueError("Source audit model output was truncated")
                return _parse_json_content("".join(content_parts)), usage
            response.read()
        if response.status_code != 429 and response.status_code < 500:
            response.raise_for_status()
        if attempt == 8:
            response.raise_for_status()
        retry_after = response.headers.get("retry-after", "").strip()
        delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else min(60.0, 2.0 ** attempt)
        time.sleep(min(delay, max(0.0, max_request_seconds - (time.monotonic() - started))))
    raise AssertionError("unreachable")


def _append_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_inventory(path: Path, inventory: list[SourceFile]) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "root": str(ROOT),
        "file_count": len(inventory),
        "line_count": sum(item.lines for item in inventory),
        "byte_count": sum(item.bytes for item in inventory),
        "files": [asdict(item) for item in inventory],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--base-url", default=os.environ.get("MINICODE_AUDIT_BASE_URL", ""))
    parser.add_argument("--model", default=os.environ.get("MINICODE_AUDIT_MODEL", "glm-5.3"))
    parser.add_argument("--chunk-chars", type=int, default=50_000)
    parser.add_argument("--batch-chars", type=int, default=100_000)
    parser.add_argument("--max-tokens", type=int, default=4_096)
    parser.add_argument("--max-request-seconds", type=float, default=180.0)
    parser.add_argument("--thinking", choices=("disabled", "enabled", "omit"), default="disabled")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-batches", type=int, default=0)
    args = parser.parse_args()

    output = args.out.expanduser().resolve()
    inventory_path = output / "source-inventory.json"
    ledger_path = output / "review-ledger.jsonl"
    inventory, contents = build_inventory()
    _write_inventory(inventory_path, inventory)
    completed = _load_completed_ranges(ledger_path)
    pending = [
        item
        for source in inventory
        for item in _range_chunks(source, contents[source.path], chunk_chars=args.chunk_chars)
        if item.key not in completed
    ]
    batches = _batch_ranges(pending, contents, batch_chars=args.batch_chars)
    print(json.dumps({
        "files": len(inventory),
        "lines": sum(item.lines for item in inventory),
        "pending_ranges": len(pending),
        "pending_batches": len(batches),
        "inventory": str(inventory_path),
        "ledger": str(ledger_path),
    }, ensure_ascii=False), flush=True)
    if args.dry_run or not batches:
        return 0
    if not args.base_url.strip():
        parser.error("--base-url or MINICODE_AUDIT_BASE_URL is required")
    api_key = os.environ.get("MINICODE_AUDIT_API_KEY", "").strip()
    if not api_key:
        parser.error("MINICODE_AUDIT_API_KEY is required")

    if args.max_batches > 0:
        batches = batches[: args.max_batches]

    with httpx.Client(timeout=httpx.Timeout(args.max_request_seconds, connect=30.0)) as client:
        for index, batch in enumerate(batches, start=1):
            started = time.monotonic()
            response, usage = _review_batch(
                client,
                base_url=args.base_url,
                api_key=api_key,
                model=args.model,
                prompt=_render_batch(batch, contents),
                max_tokens=args.max_tokens,
                thinking=args.thinking,
                max_request_seconds=args.max_request_seconds,
            )
            record = {
                "schema_version": 1,
                "status": "completed",
                "reviewed_at": datetime.now(UTC).isoformat(),
                "model": args.model,
                "batch": index,
                "ranges": [asdict(item) for item in batch],
                "review": response,
                "manual_status": "pending",
                "usage": usage,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            _append_record(ledger_path, record)
            print(json.dumps({
                "batch": index,
                "batches": len(batches),
                "ranges": len(batch),
                "findings": len(response["findings"]),
                "elapsed_seconds": record["elapsed_seconds"],
            }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
