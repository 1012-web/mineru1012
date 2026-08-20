from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pypdf import PdfWriter


SKILL_ROOT = Path(__file__).resolve().parents[1]
CLIENT = SKILL_ROOT / "scripts" / "mineru_api.py"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
import _common
import mineru_api


def result_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("full.md", "# mock\n")
        archive.writestr("layout.json", "[]")
        archive.writestr("block_list.json", "[]")
    return buffer.getvalue()


class MockMinerUHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    uploaded = []
    submitted_names = []
    archive = result_zip()

    def log_message(self, _format, *_args):
        pass

    def send_json(self, value):
        body = json.dumps(value).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).submitted_names = [item["name"] for item in payload["files"]]
        urls = [f"http://127.0.0.1:{self.server.server_port}/upload/{index}" for index in range(len(payload["files"]))]
        self.send_json({"code": 0, "msg": "ok", "data": {"batch_id": "batch-test", "file_urls": urls}})

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        type(self).uploaded.append(len(self.rfile.read(length)))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path.endswith("/extract-results/batch/batch-test"):
            results = [
                {
                    "file_name": name,
                    "state": "done",
                    "err_msg": "",
                    "full_zip_url": f"http://127.0.0.1:{self.server.server_port}/download/{index}.zip",
                }
                for index, name in enumerate(type(self).submitted_names)
            ]
            self.send_json({"code": 0, "msg": "ok", "data": {"batch_id": "batch-test", "extract_result": results}})
            return
        if self.path.startswith("/download/"):
            body = type(self).archive
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


class MinerUApiTest(unittest.TestCase):
    def test_v4_middle_json_is_discovered(self):
        with tempfile.TemporaryDirectory() as temp_name:
            nested = Path(temp_name) / "result"
            nested.mkdir()
            middle = nested / "sample_middle.json"
            middle.write_text('{"pdf_info": []}', encoding="utf-8")
            self.assertEqual(Path(_common.find_files(temp_name)["layout"]), middle)

    def test_origin_pdf_can_be_rebuilt_after_temp_split_is_gone(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "source.pdf"
            writer = PdfWriter()
            for _ in range(3):
                writer.add_blank_page(width=595, height=842)
            with source.open("wb") as stream:
                writer.write(stream)
            destination = root / "part_origin.pdf"
            restored = mineru_api.restore_origin_pdf(
                {
                    "path": str(root / "deleted-temp-part.pdf"),
                    "source": str(source),
                    "page_start": 2,
                    "page_end": 3,
                },
                destination,
            )
            self.assertEqual(restored, str(destination))
            from pypdf import PdfReader

            self.assertEqual(len(PdfReader(str(destination)).pages), 2)

    def test_lossless_split_cache_is_reused(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "large-page-count.pdf"
            writer = PdfWriter()
            for _ in range(201):
                writer.add_blank_page(width=595, height=842)
            with source.open("wb") as stream:
                writer.write(stream)
            cache = root / "cache"
            first = mineru_api.prepare_pdf(source, cache, "on")
            mtimes = [Path(part.path).stat().st_mtime_ns for part in first]
            second = mineru_api.prepare_pdf(source, cache, "on")
            self.assertEqual([(part.page_start, part.page_end) for part in first], [(1, 200), (201, 201)])
            self.assertEqual([part.path for part in first], [part.path for part in second])
            self.assertEqual(mtimes, [Path(part.path).stat().st_mtime_ns for part in second])

    def test_end_to_end_local_batch(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "sample.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=595, height=842)
            writer.add_blank_page(width=595, height=842)
            with source.open("wb") as stream:
                writer.write(stream)

            server = ThreadingHTTPServer(("127.0.0.1", 0), MockMinerUHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                env = os.environ.copy()
                env["MINERU_API_TOKEN"] = "sk-test"
                env["MINERU_SKILL_CONFIG_DIR"] = str(root / "config")
                env["MINERU_API_BASE_URL"] = f"http://127.0.0.1:{server.server_port}/api/v4"
                output = root / "output"
                process = subprocess.run(
                    [
                        sys.executable,
                        str(CLIENT),
                        "submit",
                        str(source),
                        "-o",
                        str(output),
                        "--poll-interval",
                        "1",
                        "--timeout",
                        "10",
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
            self.assertEqual(len(MockMinerUHandler.uploaded), 1)
            self.assertGreater(MockMinerUHandler.uploaded[0], 0)
            self.assertIn("DONE report=", process.stdout)
            self.assertIn("files=1/5000", process.stdout)
            self.assertIn("priority_pages=2/1000", process.stdout)
            extracted = list(output.glob("sample.mineru*/full.md"))
            self.assertEqual(len(extracted), 1)
            origins = list(output.glob("sample.mineru*/sample_origin.pdf"))
            self.assertEqual(len(origins), 1)
            reports = list(output.glob("_mineru_api_report_*.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertFalse(report["usage"]["is_official_balance"])
            self.assertEqual(report["usage"]["estimated_remaining_priority_pages"], 998)
            self.assertEqual(report["batches"][0]["parts"][0]["pages"], 2)


if __name__ == "__main__":
    unittest.main()
