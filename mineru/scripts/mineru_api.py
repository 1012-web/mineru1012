"""Submit local PDFs to MinerU's precise parsing API.

The client keeps credentials outside the skill repository, splits oversized PDFs
without changing the originals, downloads and safely extracts result archives,
and records a local estimate of the daily account usage.

Usage:
    python mineru_api.py configure
    python mineru_api.py submit a.pdf [b.pdf ...] [-o OUTPUT_DIR]
    python mineru_api.py resume BATCH_ID
    python mineru_api.py usage
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import datetime as dt
import getpass
import hashlib
import http.client
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


API_BASE = "https://mineru.net/api/v4"
TOKEN_ENV = "MINERU_API_TOKEN"
CONFIG_DIR_ENV = "MINERU_SKILL_CONFIG_DIR"
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_PAGES = 200
MAX_LOCAL_BATCH = 50
DEFAULT_DAILY_FILE_QUOTA = 5000
DEFAULT_PRIORITY_PAGE_QUOTA = 1000
TERMINAL_STATES = {"done", "failed"}
SERVICE_TIMEZONE = dt.timezone(dt.timedelta(hours=8))


class ClientError(RuntimeError):
    pass


@dataclass
class UploadPart:
    source: str
    path: str
    upload_name: str
    data_id: str
    pages: int
    page_start: int
    page_end: int
    is_ocr: bool


def config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home())))
        return base / "mineru-skill"
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "mineru-skill"


def config_path() -> Path:
    return config_dir() / "config.json"


def usage_path() -> Path:
    return config_dir() / "usage.json"


def jobs_dir() -> Path:
    return config_dir() / "jobs"


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClientError(f"Cannot read JSON file: {path}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        os.chmod(temp, 0o600)
    os.replace(temp, path)


def _dpapi_protect(token: str) -> str:
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    raw = token.encode("utf-8")
    raw_buffer = ctypes.create_string_buffer(raw)
    source = DataBlob(len(raw), ctypes.cast(raw_buffer, ctypes.POINTER(ctypes.c_byte)))
    target = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(source), "MinerU API token", None, None, None, 1, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)
    return base64.b64encode(encrypted).decode("ascii")


def _dpapi_unprotect(value: str) -> str:
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    raw = base64.b64decode(value)
    raw_buffer = ctypes.create_string_buffer(raw)
    source = DataBlob(len(raw), ctypes.cast(raw_buffer, ctypes.POINTER(ctypes.c_byte)))
    target = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        decrypted = ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)
    return decrypted.decode("utf-8")


def store_token(token: str, daily_file_quota: int, priority_page_quota: int) -> None:
    if not token.strip():
        raise ClientError("Token cannot be empty")
    if daily_file_quota < 1 or priority_page_quota < 1:
        raise ClientError("Quota values must be positive integers")
    if os.name == "nt":
        storage = "windows-dpapi"
        protected = _dpapi_protect(token.strip())
    else:
        storage = "restricted-file"
        protected = token.strip()
    current = read_json(config_path(), {})
    current.update(
        {
            "token_storage": storage,
            "token": protected,
            "daily_file_quota": daily_file_quota,
            "priority_page_quota": priority_page_quota,
            "configured_at": dt.datetime.now().astimezone().isoformat(),
        }
    )
    write_json(config_path(), current)


def load_config() -> dict[str, Any]:
    config = read_json(config_path(), {})
    config.setdefault("daily_file_quota", DEFAULT_DAILY_FILE_QUOTA)
    config.setdefault("priority_page_quota", DEFAULT_PRIORITY_PAGE_QUOTA)
    return config


def load_token() -> str:
    from_env = os.environ.get(TOKEN_ENV, "").strip()
    if from_env:
        return from_env
    config = load_config()
    value = config.get("token")
    if not value:
        raise ClientError(
            "MinerU token is not configured. Run 'python mineru_api.py configure' locally."
        )
    if config.get("token_storage") == "windows-dpapi":
        return _dpapi_unprotect(value)
    return value


def api_url(path: str) -> str:
    base = os.environ.get("MINERU_API_BASE_URL", API_BASE).rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def json_request(method: str, path: str, token: str, payload: Any | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        api_url(path),
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ClientError(f"MinerU HTTP {exc.code}: {detail[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise ClientError(f"MinerU request failed: {exc.reason}") from exc
    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ClientError(f"MinerU returned non-JSON data: {body[:500]}") from exc
    if result.get("code") != 0:
        raise ClientError(f"MinerU API error {result.get('code')}: {result.get('msg')}")
    return result


def put_file(url: str, file_path: Path) -> None:
    parsed = urllib.parse.urlsplit(url)
    connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_cls(parsed.hostname, parsed.port, timeout=180)
    target = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))
    try:
        connection.putrequest("PUT", target)
        connection.putheader("Content-Length", str(file_path.stat().st_size))
        connection.endheaders()
        total = file_path.stat().st_size
        sent = 0
        next_progress = 32 * 1024 * 1024
        with file_path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                connection.send(chunk)
                sent += len(chunk)
                if sent >= next_progress and sent < total:
                    print(
                        f"UPLOAD_PROGRESS file={file_path.name} "
                        f"sent_mib={sent / 1024 / 1024:.1f} total_mib={total / 1024 / 1024:.1f}"
                    )
                    next_progress += 32 * 1024 * 1024
        response = connection.getresponse()
        detail = response.read()
        if not 200 <= response.status < 300:
            raise ClientError(
                f"Upload failed for {file_path.name}: HTTP {response.status}: "
                f"{detail[:500].decode('utf-8', errors='replace')}"
            )
    finally:
        connection.close()


def download_file(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "mineru-skill-api-client"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
    except (OSError, urllib.error.URLError) as exc:
        raise ClientError(f"Download failed: {url}: {exc}") from exc


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for number in range(2, 10000):
        candidate = path.with_name(f"{path.stem}-{number}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ClientError(f"Cannot allocate a unique output name near {path}")


def safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if target != root and root not in target.parents:
                raise ClientError(f"Unsafe ZIP member: {member.filename}")
        bundle.extractall(destination)


def restore_origin_pdf(part: dict[str, Any], destination: Path) -> str | None:
    upload_path = Path(part["path"])
    source_path = Path(part["source"])
    try:
        if upload_path.is_file():
            shutil.copy2(upload_path, destination)
            return str(destination)
        if not source_path.is_file():
            return None
        PdfReader, PdfWriter = require_pypdf()
        reader = PdfReader(str(source_path))
        start = int(part["page_start"]) - 1
        end = int(part["page_end"])
        if start == 0 and end == len(reader.pages):
            shutil.copy2(source_path, destination)
        else:
            write_pdf_range(reader, PdfWriter, start, end, destination)
        return str(destination)
    except Exception:
        return None


def require_pypdf():
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise ClientError("PDF preflight requires pypdf. Install it with: python -m pip install pypdf") from exc
    return PdfReader, PdfWriter


def detect_ocr(reader: Any) -> bool:
    total = len(reader.pages)
    sample_indexes = sorted({0, total // 2, total - 1})
    extracted = 0
    for index in sample_indexes:
        try:
            extracted += len("".join((reader.pages[index].extract_text() or "").split()))
        except Exception:
            pass
    return extracted < 120


def write_pdf_range(reader: Any, writer_cls: Any, start: int, end: int, destination: Path) -> None:
    writer = writer_cls()
    for index in range(start, end):
        writer.add_page(reader.pages[index])
    with destination.open("wb") as output:
        writer.write(output)


def prepare_pdf(source: Path, cache_root: Path, ocr_mode: str) -> list[UploadPart]:
    PdfReader, PdfWriter = require_pypdf()
    try:
        reader = PdfReader(str(source))
    except Exception as exc:
        raise ClientError(f"Cannot open PDF {source}: {exc}") from exc
    if reader.is_encrypted and reader.decrypt("") == 0:
        raise ClientError(f"Encrypted PDF requires a password and cannot be submitted: {source}")
    total_pages = len(reader.pages)
    if total_pages < 1:
        raise ClientError(f"PDF has no pages: {source}")
    is_ocr = detect_ocr(reader) if ocr_mode == "auto" else ocr_mode == "on"
    digest_seed = f"{source.resolve()}|{source.stat().st_size}|{source.stat().st_mtime_ns}"
    source_digest = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()[:12]
    source_cache = cache_root / source_digest
    source_cache.mkdir(parents=True, exist_ok=True)
    initial = [(start, min(start + MAX_PAGES, total_pages)) for start in range(0, total_pages, MAX_PAGES)]
    pending = list(initial)
    prepared: list[tuple[int, int, Path]] = []
    while pending:
        start, end = pending.pop(0)
        if start == 0 and end == total_pages and source.stat().st_size <= MAX_FILE_BYTES:
            candidate = source
        else:
            candidate = source_cache / f"{source.stem}.pages-{start + 1:04d}-{end:04d}.pdf"
            if not candidate.exists():
                partial = candidate.with_suffix(candidate.suffix + ".partial")
                write_pdf_range(reader, PdfWriter, start, end, partial)
                os.replace(partial, candidate)
        if candidate.stat().st_size > MAX_FILE_BYTES:
            if end - start == 1:
                raise ClientError(f"One page exceeds MinerU's 200 MB limit: {source}, page {start + 1}")
            midpoint = start + (end - start) // 2
            pending[0:0] = [(start, midpoint), (midpoint, end)]
            continue
        prepared.append((start, end, candidate))

    parts: list[UploadPart] = []
    for start, end, path in sorted(prepared):
        suffix = "" if total_pages <= MAX_PAGES and len(prepared) == 1 else f".pages-{start + 1:04d}-{end:04d}"
        upload_name = f"{source.stem}{suffix}.pdf"
        data_id = f"{source_digest}-{start + 1}-{end}"
        parts.append(
            UploadPart(
                source=str(source),
                path=str(path),
                upload_name=upload_name,
                data_id=data_id,
                pages=end - start,
                page_start=start + 1,
                page_end=end,
                is_ocr=is_ocr,
            )
        )
    return parts


def current_usage() -> dict[str, Any]:
    today = dt.datetime.now(SERVICE_TIMEZONE).date().isoformat()
    ledger = read_json(usage_path(), {"days": {}})
    records = ledger.get("days", {}).get(today, {}).get("records", {})
    files = len(records)
    pages = sum(int(record.get("pages", 0)) for record in records.values())
    config = load_config()
    file_quota = int(config.get("daily_file_quota", DEFAULT_DAILY_FILE_QUOTA))
    page_quota = int(config.get("priority_page_quota", DEFAULT_PRIORITY_PAGE_QUOTA))
    return {
        "date": today,
        "locally_tracked_submitted_files": files,
        "estimated_remaining_files": max(0, file_quota - files),
        "configured_daily_file_quota": file_quota,
        "locally_tracked_submitted_pages": pages,
        "estimated_remaining_priority_pages": max(0, page_quota - pages),
        "configured_priority_page_quota": page_quota,
        "reset": "00:00 UTC+08:00",
        "is_official_balance": False,
        "note": (
            "MinerU exposes no documented usage endpoint. Values are local estimates and exclude calls "
            "made elsewhere. Pages beyond the priority quota enter the normal queue rather than failing."
        ),
    }


def record_submitted(batch_id: str, part: UploadPart) -> None:
    today = dt.datetime.now(SERVICE_TIMEZONE).date().isoformat()
    ledger = read_json(usage_path(), {"days": {}})
    days = ledger.setdefault("days", {})
    records = days.setdefault(today, {}).setdefault("records", {})
    key = f"{batch_id}:{part.upload_name}"
    records.setdefault(
        key,
        {
            "pages": part.pages,
            "source": part.source,
            "submitted_at": dt.datetime.now().astimezone().isoformat(),
        },
    )
    write_json(usage_path(), ledger)


def print_usage(usage: dict[str, Any]) -> None:
    print(
        "USAGE "
        f"date={usage['date']} "
        f"files={usage['locally_tracked_submitted_files']}/{usage['configured_daily_file_quota']} "
        f"estimated_files_remaining={usage['estimated_remaining_files']} "
        f"priority_pages={usage['locally_tracked_submitted_pages']}/{usage['configured_priority_page_quota']} "
        f"estimated_priority_pages_remaining={usage['estimated_remaining_priority_pages']} "
        "official_balance=false"
    )


def save_job(batch_id: str, value: dict[str, Any]) -> Path:
    path = jobs_dir() / f"{batch_id}.json"
    write_json(path, value)
    return path


def public_part(part: UploadPart | dict[str, Any]) -> dict[str, Any]:
    value = asdict(part) if isinstance(part, UploadPart) else part
    fields = ("source", "upload_name", "data_id", "pages", "page_start", "page_end", "is_ocr")
    return {field: value.get(field) for field in fields}


def load_job(batch_id: str) -> tuple[Path, dict[str, Any]]:
    path = jobs_dir() / f"{batch_id}.json"
    job = read_json(path, None)
    if job is None:
        raise ClientError(f"No local job record for batch {batch_id}")
    return path, job


def poll_batch(token: str, batch_id: str, interval: int, timeout: int) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    last_summary = ""
    while True:
        response = json_request("GET", f"extract-results/batch/{batch_id}", token)
        results = response.get("data", {}).get("extract_result", [])
        summary = ",".join(f"{item.get('file_name')}={item.get('state')}" for item in results)
        if summary != last_summary:
            print(f"STATUS batch={batch_id} {summary}")
            last_summary = summary
        if results and all(item.get("state") in TERMINAL_STATES for item in results):
            return results
        if time.monotonic() >= deadline:
            raise ClientError(f"Timed out waiting for MinerU batch {batch_id}; resume it later")
        time.sleep(interval)


def download_results(job: dict[str, Any], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output_dir = Path(job["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    completed: list[dict[str, Any]] = []
    existing = {item.get("file_name"): item for item in job.get("downloads", [])}
    parts = {item.get("upload_name"): item for item in job.get("parts", [])}
    for result in results:
        file_name = result.get("file_name", "unknown")
        if result.get("state") != "done":
            completed.append(
                {"file_name": file_name, "state": result.get("state"), "error": result.get("err_msg", "")}
            )
            continue
        previous = existing.get(file_name)
        if previous and Path(previous.get("directory", "")).exists():
            completed.append(previous)
            continue
        url = result.get("full_zip_url")
        if not url:
            completed.append({"file_name": file_name, "state": "failed", "error": "missing full_zip_url"})
            continue
        stem = Path(file_name).stem + ".mineru"
        archive = unique_path(output_dir / f"{stem}.zip")
        directory = unique_path(output_dir / stem)
        download_file(url, archive)
        safe_extract(archive, directory)
        origin_name = f"{Path(file_name).stem}_origin.pdf"
        origin_pdf = restore_origin_pdf(parts.get(file_name, {}), directory / origin_name) if file_name in parts else None
        completed.append(
            {
                "file_name": file_name,
                "state": "done",
                "archive": str(archive),
                "directory": str(directory),
                "origin_pdf": origin_pdf,
                "origin_pdf_missing": origin_pdf is None,
            }
        )
    job["downloads"] = completed
    return completed


def create_and_run_batch(
    token: str,
    parts: list[UploadPart],
    output_dir: Path,
    model: str,
    language: str,
    enable_formula: bool,
    enable_table: bool,
    interval: int,
    timeout: int,
) -> dict[str, Any]:
    payload = {
        "files": [
            {
                "name": part.upload_name,
                "data_id": part.data_id,
                "is_ocr": part.is_ocr,
            }
            for part in parts
        ],
        "model_version": model,
        "language": language,
        "enable_formula": enable_formula,
        "enable_table": enable_table,
    }
    response = json_request("POST", "file-urls/batch", token, payload)
    batch_id = response["data"]["batch_id"]
    upload_urls = response["data"].get("file_urls", [])
    if len(upload_urls) != len(parts):
        raise ClientError(
            f"MinerU returned {len(upload_urls)} upload URLs for {len(parts)} files in batch {batch_id}"
        )
    job = {
        "batch_id": batch_id,
        "created_at": dt.datetime.now().astimezone().isoformat(),
        "output_dir": str(output_dir),
        "options": {
            "model": model,
            "language": language,
            "enable_formula": enable_formula,
            "enable_table": enable_table,
        },
        "parts": [asdict(part) for part in parts],
        "downloads": [],
    }
    job_path = save_job(batch_id, job)
    print(f"BATCH id={batch_id} files={len(parts)} job={job_path}")
    for part, upload_url in zip(parts, upload_urls):
        print(
            f"UPLOADING batch={batch_id} file={part.upload_name} "
            f"mib={Path(part.path).stat().st_size / 1024 / 1024:.1f} pages={part.pages}"
        )
        put_file(upload_url, Path(part.path))
        record_submitted(batch_id, part)
        print(f"UPLOADED batch={batch_id} file={part.upload_name} pages={part.pages} ocr={str(part.is_ocr).lower()}")
    results = poll_batch(token, batch_id, interval, timeout)
    downloads = download_results(job, results)
    save_job(batch_id, job)
    return {"batch_id": batch_id, "parts": [public_part(part) for part in parts], "results": downloads}


def write_report(output_dir: Path, report: dict[str, Any]) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = unique_path(output_dir / f"_mineru_api_report_{stamp}.json")
    write_json(path, report)
    return path


def command_configure(args: argparse.Namespace) -> int:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        token = getpass.getpass("MinerU API Token: ").strip()
    store_token(token, args.daily_file_quota, args.priority_page_quota)
    print(
        f"CONFIGURED path={config_path()} token_storage="
        f"{'windows-dpapi' if os.name == 'nt' else 'restricted-file'}"
    )
    print_usage(current_usage())
    return 0


def command_submit(args: argparse.Namespace) -> int:
    token = load_token()
    if args.poll_interval < 1 or args.timeout < 1:
        raise ClientError("Poll interval and timeout must be positive integers")
    sources = [Path(value).expanduser().resolve() for value in args.files]
    for source in sources:
        if not source.is_file() or source.suffix.lower() != ".pdf":
            raise ClientError(f"Not a PDF file: {source}")
    output_dir = Path(args.output).expanduser().resolve() if args.output else sources[0].parent / "MinerU"
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_root = output_dir / "_upload_cache"
    all_parts: list[UploadPart] = []
    for source in sources:
        all_parts.extend(prepare_pdf(source, cache_root, args.ocr))
    used_names: set[str] = set()
    for part in all_parts:
        original_name = part.upload_name
        candidate = original_name
        number = 2
        while candidate.casefold() in used_names:
            candidate = f"{Path(original_name).stem}.{part.data_id}.{number}.pdf"
            number += 1
        part.upload_name = candidate
        used_names.add(candidate.casefold())
    print(
        f"PREFLIGHT source_files={len(sources)} upload_parts={len(all_parts)} "
        f"pages={sum(part.pages for part in all_parts)} cache={cache_root}"
    )
    batches = []
    for offset in range(0, len(all_parts), MAX_LOCAL_BATCH):
        group = all_parts[offset : offset + MAX_LOCAL_BATCH]
        batches.append(
            create_and_run_batch(
                token,
                group,
                output_dir,
                args.model,
                args.language,
                args.enable_formula,
                args.enable_table,
                args.poll_interval,
                args.timeout,
            )
        )
    usage = current_usage()
    report = {
        "created_at": dt.datetime.now().astimezone().isoformat(),
        "source_files": [str(path) for path in sources],
        "output_dir": str(output_dir),
        "batches": batches,
        "usage": usage,
    }
    report_path = write_report(output_dir, report)
    print(f"DONE report={report_path}")
    print_usage(usage)
    failures = [result for batch in batches for result in batch["results"] if result.get("state") != "done"]
    return 2 if failures else 0


def command_resume(args: argparse.Namespace) -> int:
    token = load_token()
    if args.poll_interval < 1 or args.timeout < 1:
        raise ClientError("Poll interval and timeout must be positive integers")
    job_path, job = load_job(args.batch_id)
    results = poll_batch(token, args.batch_id, args.poll_interval, args.timeout)
    downloads = download_results(job, results)
    write_json(job_path, job)
    usage = current_usage()
    report_path = write_report(
        Path(job["output_dir"]),
        {
            "created_at": dt.datetime.now().astimezone().isoformat(),
            "resumed_batch": args.batch_id,
            "parts": [public_part(part) for part in job.get("parts", [])],
            "results": downloads,
            "usage": usage,
        },
    )
    print(f"DONE report={report_path}")
    print_usage(usage)
    return 2 if any(item.get("state") != "done" for item in downloads) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure = subparsers.add_parser("configure", help="store the MinerU token and quota definitions")
    configure.add_argument("--daily-file-quota", type=int, default=DEFAULT_DAILY_FILE_QUOTA)
    configure.add_argument("--priority-page-quota", type=int, default=DEFAULT_PRIORITY_PAGE_QUOTA)
    configure.set_defaults(func=command_configure)

    submit = subparsers.add_parser("submit", help="submit local PDFs and download completed results")
    submit.add_argument("files", nargs="+")
    submit.add_argument("-o", "--output")
    submit.add_argument("--model", choices=("pipeline", "vlm"), default="vlm")
    submit.add_argument("--language", default="ch")
    submit.add_argument("--ocr", choices=("auto", "on", "off"), default="auto")
    submit.add_argument("--formula", dest="enable_formula", action=argparse.BooleanOptionalAction, default=True)
    submit.add_argument("--table", dest="enable_table", action=argparse.BooleanOptionalAction, default=True)
    submit.add_argument("--poll-interval", type=int, default=5)
    submit.add_argument("--timeout", type=int, default=21600)
    submit.set_defaults(func=command_submit)

    resume = subparsers.add_parser("resume", help="resume polling and downloading a saved batch")
    resume.add_argument("batch_id")
    resume.add_argument("--poll-interval", type=int, default=5)
    resume.add_argument("--timeout", type=int, default=21600)
    resume.set_defaults(func=command_resume)

    usage = subparsers.add_parser("usage", help="show locally tracked daily usage")
    usage.set_defaults(func=lambda _args: (print_usage(current_usage()) or 0))
    return parser


def main() -> int:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(line_buffering=True)
        args = build_parser().parse_args()
        return args.func(args)
    except ClientError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
