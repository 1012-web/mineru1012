#!/usr/bin/env python3
"""Invoke the small public catalog Ability surface through Studio or REST."""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ALLOWED_ABILITIES = {
    "mulu1012-catalog/deduplicate-candidates",
    "mulu1012-catalog/import-local-batch",
    "mulu1012-catalog/get-batch",
    "mulu1012-catalog/get-candidate",
}
READONLY_ABILITIES = {
    "mulu1012-catalog/deduplicate-candidates",
    "mulu1012-catalog/get-batch",
    "mulu1012-catalog/get-candidate",
}


class AbilityCallError(RuntimeError):
    """An Ability transport or WordPress execution error."""

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.details = details


@dataclass(frozen=True)
class TransportConfig:
    transport: str
    site_path: str = ""
    site_url: str = ""
    wp_user: str = ""
    timeout: int = 120


def add_transport_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--transport",
        choices=("auto", "studio", "rest"),
        default="auto",
        help="Ability transport. auto chooses from --site-path or --site-url.",
    )
    parser.add_argument(
        "--site-path",
        default=os.environ.get("MULU1012_WP_PATH", ""),
        help="WordPress Studio site root for studio transport.",
    )
    parser.add_argument(
        "--site-url",
        default=os.environ.get("MULU1012_WP_URL", ""),
        help="WordPress base URL for REST transport.",
    )
    parser.add_argument(
        "--wp-user",
        default=os.environ.get("MULU1012_WP_USER", ""),
        help="WP login or ID. Required for both transports.",
    )
    parser.add_argument("--timeout", type=int, default=120, help="Call timeout in seconds.")


def transport_from_args(args: argparse.Namespace) -> TransportConfig:
    transport = args.transport
    if transport == "auto":
        if args.site_path:
            transport = "studio"
        elif args.site_url:
            transport = "rest"
        else:
            raise AbilityCallError("Provide --site-path for Studio or --site-url for REST.")

    if not args.wp_user:
        raise AbilityCallError("Provide --wp-user or set MULU1012_WP_USER.")
    if args.timeout < 1:
        raise AbilityCallError("--timeout must be at least 1 second.")
    if transport == "studio" and not args.site_path:
        raise AbilityCallError("Studio transport requires --site-path.")
    if transport == "rest" and not args.site_url:
        raise AbilityCallError("REST transport requires --site-url.")
    if transport == "rest":
        parsed_url = urllib.parse.urlparse(args.site_url)
        if not parsed_url.scheme or not parsed_url.netloc:
            raise AbilityCallError("--site-url must be an absolute WordPress URL.")
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if parsed_url.scheme != "https" and parsed_url.hostname not in local_hosts:
            raise AbilityCallError("Remote REST transport requires HTTPS.")

    return TransportConfig(
        transport=transport,
        site_path=args.site_path,
        site_url=args.site_url,
        wp_user=args.wp_user,
        timeout=args.timeout,
    )


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise AbilityCallError(f"Unable to read JSON input: {path}", details=str(error)) from error


def write_json_atomic(path: Path, value: Any) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def invoke_ability(name: str, input_value: Any, config: TransportConfig) -> Any:
    if name not in ALLOWED_ABILITIES:
        raise AbilityCallError(f"Ability is not allowed by this Skill: {name}")
    if config.transport == "studio":
        return _invoke_studio(name, input_value, config)
    if config.transport == "rest":
        return _invoke_rest(name, input_value, config)
    raise AbilityCallError(f"Unsupported transport: {config.transport}")


def rest_endpoint(name: str, input_value: Any, site_url: str) -> str:
    ability_path = urllib.parse.quote(name, safe="/")
    endpoint = (
        site_url.rstrip("/")
        + "/wp-json/wp-abilities/v1/abilities/"
        + ability_path
        + "/run"
    )
    if name not in READONLY_ABILITIES:
        return endpoint
    pairs = list(_flatten_query("input", input_value))
    if not pairs:
        raise AbilityCallError(f"Read-only REST Ability requires non-empty input: {name}")
    return endpoint + "?" + urllib.parse.urlencode(pairs)


def _flatten_query(prefix: str, value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _flatten_query(f"{prefix}[{key}]", child)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from _flatten_query(f"{prefix}[{index}]", child)
        return
    if isinstance(value, bool):
        yield prefix, "true" if value else "false"
        return
    if value is None:
        yield prefix, ""
        return
    if isinstance(value, (str, int, float)):
        yield prefix, str(value)
        return
    raise AbilityCallError(f"REST input contains an unsupported value at {prefix}.")


def _invoke_studio(name: str, input_value: Any, config: TransportConfig) -> Any:
    studio = shutil.which("studio")
    if not studio:
        raise AbilityCallError("Studio CLI is unavailable.")

    site_path = Path(config.site_path).resolve()
    if not (site_path / "wp-config.php").is_file():
        raise AbilityCallError(f"--site-path is not a WordPress Studio site: {site_path}")

    runner_source = Path(__file__).with_name("wp-ability-runner.php").resolve()
    if not runner_source.is_file():
        raise AbilityCallError(f"Ability runner is missing: {runner_source}")

    with tempfile.TemporaryDirectory(prefix=".mulu1012-ability-", dir=site_path) as temp_dir:
        runner = Path(temp_dir) / "runner.php"
        input_path = Path(temp_dir) / "input.json"
        output_path = Path(temp_dir) / "output.json"
        shutil.copy2(runner_source, runner)
        input_path.write_text(
            json.dumps(input_value, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        command = [
            studio,
            "wp",
            "eval-file",
            str(runner),
            name,
            str(input_path),
            str(output_path),
            config.wp_user,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=site_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=config.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise AbilityCallError(
                f"Ability timed out after {config.timeout} seconds: {name}"
            ) from error

        response = read_json(output_path) if output_path.is_file() else None
        if completed.returncode != 0:
            details = response or {
                "stdout": completed.stdout[-4000:],
                "stderr": completed.stderr[-4000:],
            }
            message = _error_message(response) or f"Studio Ability call failed: {name}"
            raise AbilityCallError(message, details=details)
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise AbilityCallError(
                f"Studio Ability returned an invalid response: {name}",
                details={
                    "response": response,
                    "stdout": completed.stdout[-4000:],
                    "stderr": completed.stderr[-4000:],
                },
            )
        return response.get("result")


def _invoke_rest(name: str, input_value: Any, config: TransportConfig) -> Any:
    app_password = os.environ.get("MULU1012_WP_APP_PASSWORD", "")
    if not app_password:
        raise AbilityCallError(
            "REST transport requires MULU1012_WP_APP_PASSWORD. "
            "Do not put the Application Password in command arguments."
        )

    endpoint = rest_endpoint(name, input_value, config.site_url)
    credentials = base64.b64encode(
        f"{config.wp_user}:{app_password}".encode("utf-8")
    ).decode("ascii")
    headers = {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "User-Agent": "mulu1012-catalog-collection/1.0",
    }
    is_readonly = name in READONLY_ABILITIES
    body = None
    if not is_readonly:
        body = json.dumps({"input": input_value}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="GET" if is_readonly else "POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout) as response:
            raw = response.read().decode("utf-8-sig")
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8-sig", errors="replace")
        details = _decode_json_or_text(raw)
        raise AbilityCallError(
            _error_message(details) or f"WordPress REST returned HTTP {error.code}.",
            details=details,
        ) from error
    except urllib.error.URLError as error:
        raise AbilityCallError(f"Unable to reach WordPress REST: {error.reason}") from error

    result = _decode_json_or_text(raw)
    if isinstance(result, dict) and result.get("code") and result.get("message"):
        raise AbilityCallError(str(result["message"]), details=result)
    return result


def _decode_json_or_text(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw[-4000:]}


def _error_message(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("error"), dict):
        return str(value["error"].get("message") or "")
    return str(value.get("message") or "")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Invoke an allowed catalog Ability.")
    parser.add_argument("--ability", required=True, choices=sorted(ALLOWED_ABILITIES))
    parser.add_argument("--input", help="JSON input path. Defaults to an empty object.")
    parser.add_argument("--output", help="Optional JSON result path.")
    add_transport_arguments(parser)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = transport_from_args(args)
    input_value = read_json(Path(args.input).resolve()) if args.input else {}
    result = invoke_ability(args.ability, input_value, config)
    response = {"ok": True, "ability": args.ability, "result": result}
    if args.output:
        write_json_atomic(Path(args.output), response)
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AbilityCallError as error:
        print(
            json.dumps(
                {"ok": False, "error": str(error), "details": error.details},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)
