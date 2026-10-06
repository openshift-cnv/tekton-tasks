#!/usr/bin/env python

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import yaml  # type: ignore
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Literal

import requests

PROW_URL = "https://prow.ci.openshift.org"
PROW_GCS_WEB_URL = "https://gcsweb-ci.apps.ci.l2s4.p1.openshiftapps.com/gcs"
GANGWAY_API_URL = "https://gangway-ci.apps.ci.l2s4.p1.openshiftapps.com/v1"
GANGWAY_SECRET_NAME = "OpenShift CI Gangway"
_gangway_token_cache: str | None = None


class AppError(RuntimeError):
    def __init__(
        self, message: str, *, exit_code: int = 1, print_to_stderr: bool = True
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.print_to_stderr = print_to_stderr


def _format_http_error(
    *,
    action: str,
    url: str,
    status_code: int | None = None,
    reason: str | None = None,
) -> str:
    bits: list[str] = [f"{action} failed."]
    if status_code is not None:
        bits.append(f"HTTP {status_code}" + (f" ({reason})" if reason else "") + ".")
    bits.append(f"URL: <{url}>")
    return " ".join(bits)


def _job_url(job: Mapping[str, Any]) -> str | None:
    url = job.get("status", {}).get("url")
    return str(url) if url else None


def _job_gs_prefix(job: Mapping[str, Any]) -> str | None:
    """
    Returns the 'bucket/path' portion used by gs://... derived from the job URL, if available.
    Example:
        https://prow.ci.openshift.org/view/gs/<bucket>/<path...> -> <bucket>/<path...>
    """
    url = _job_url(job)
    if not url:
        return None
    marker = "/view/gs/"
    if marker not in url:
        return None
    return url.split(marker, 1)[1].strip("/")


def _prowjob_yaml_url(job_id: str) -> str:
    cleaned = (job_id or "").strip()
    return f"{PROW_URL}/prowjob?prowjob={cleaned}"


def _print_job_details(*, job_id: str, job: Mapping[str, Any] | None) -> None:
    print(f"[INFO] ProwJob ID: <{job_id}>")
    print(f"[INFO] ProwJob YAML URL: <{_prowjob_yaml_url(job_id)}>")
    if not job:
        print("[WARN] ProwJob details are not available yet (could not fetch payload).")
        print("[INFO] ProwJob status.url: <unset>")
        return
    status_url = _job_url(job)
    state = str(job.get("status", {}).get("state", ""))
    print(f"[INFO] ProwJob status.state: <{state or 'unknown'}>")
    print(f"[INFO] ProwJob status.url: <{status_url or 'unset'}>")


def _try_get_job_from_id(
    job_id: str,
    *,
    session: requests.Session,
    attempts: int = 5,
    sleep_seconds: float = 2.0,
) -> dict[str, Any] | None:
    last_err: AppError | None = None
    for i in range(max(1, attempts)):
        try:
            return get_job_from_id(job_id, session=session)
        except AppError as err:
            last_err = err
            if i < attempts - 1:
                time.sleep(sleep_seconds)
            continue
    if last_err:
        print(
            f"[WARN] Unable to fetch ProwJob <{job_id}> yet: {last_err}",
            file=sys.stderr,
        )
    return None


def _safe_join_outdir(*, outdir: Path, rel_path: str) -> Path:
    p = Path((rel_path or "").strip())
    if str(p).strip() == "":
        raise AppError("Artifact path is empty.", exit_code=2)
    if p.is_absolute() or ".." in p.parts:
        raise AppError(
            f"Artifact path <{rel_path}> is invalid (must be a relative path without '..').",
            exit_code=2,
        )
    return outdir / p


def _classify_gcs_artifact_path(
    *,
    gs_prefix: str,
    rel_path: str,
) -> Literal["file", "dir", "missing", "unknown"]:
    cleaned = (rel_path or "").strip().lstrip("/")
    if cleaned == "":
        return "unknown"
    if cleaned.endswith("/"):
        return "dir"

    src = f"gs://{gs_prefix}/{cleaned}"
    proc = subprocess.run(
        ["gsutil", "ls", src],
        check=False,
        capture_output=True,
        text=True,
    )
    combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        if "No URLs matched" in combined:
            return "missing"
        return "unknown"

    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return "unknown"

    # If any listed entry is under <src>/ then <src> behaves like a directory prefix.
    if any(ln.startswith(src + "/") for ln in lines):
        return "dir"

    # Otherwise, if the object itself is listed, treat it as a file-like object.
    if any(ln.rstrip("/") == src for ln in lines):
        return "file"

    return "unknown"


def _looks_like_html_response(resp: requests.Response) -> bool:
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "text/html" in content_type:
        return True
    head = (resp.content or b"")[:1024].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def _looks_like_glob(pattern: str) -> bool:
    cleaned = (pattern or "").strip()
    if cleaned == "":
        return False
    # gsutil supports wildcard characters (*, ?, []) and users often use ** for recursion.
    return any(ch in cleaned for ch in ("*", "?", "[", "]")) or "**" in cleaned


def _list_gcs_matches(
    *,
    gs_prefix: str,
    pattern: str,
    is_recursive: bool,
) -> list[str]:
    cleaned = (pattern or "").strip().lstrip("/")
    if cleaned == "":
        raise AppError("Artifact pattern is empty.", exit_code=2)

    _ensure_gsutil_available()
    src = f"gs://{gs_prefix}/{cleaned}"
    if cleaned.endswith("/"):
        src = src + "**"
        is_recursive = True

    args = ["gsutil", "ls"]
    if is_recursive:
        args.append("-r")
    args.append(src)

    proc = subprocess.run(args, check=False, capture_output=True, text=True)
    combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        if "No URLs matched" in combined:
            return []
        raise AppError(
            f"Listing artifacts failed (gsutil exit {proc.returncode}). Source: <{src}>\n{combined}",
            exit_code=2,
        )

    matches: list[str] = []
    for ln in (proc.stdout or "").splitlines():
        s = ln.strip()
        if not s or s.startswith("TOTAL:"):
            continue
        if not s.startswith("gs://"):
            continue
        if s.endswith("/"):
            # Directory marker/prefix.
            continue
        matches.append(s)
    return matches


def download_job_artifacts_patterns(
    job: Mapping[str, Any],
    patterns: list[str],
    *,
    outdir: str | Path | None = None,
    session: requests.Session | None = None,
    recursive: bool = False,
) -> list[Path]:
    """
    Download artifacts from a job by relative path patterns.

    Supports:
    - Exact files (download via HTTPS when possible)
    - Directory prefixes (trailing '/'; downloaded recursively)
    - Glob patterns (handled via gsutil listing + downloads)
    """
    if not patterns:
        return []

    job_url = _job_url(job)
    if not job_url:
        raise AppError(
            "Cannot download artifacts because the job payload has no status.url.",
            exit_code=2,
        )

    outdir_path = Path(outdir) if outdir is not None else (Path.cwd() / "artifacts")
    outdir_path.mkdir(parents=True, exist_ok=True)

    sess = session or requests.Session()
    downloaded: list[Path] = []

    gs_prefix = _job_gs_prefix(job)
    gs_base = f"gs://{gs_prefix}/" if gs_prefix else None

    for raw in patterns:
        cleaned = (raw or "").strip()
        if cleaned == "":
            continue
        cleaned = cleaned.lstrip("/")

        is_dir_prefix = cleaned.endswith("/")
        has_glob = _looks_like_glob(cleaned)
        is_recursive = recursive or is_dir_prefix or ("**" in cleaned)

        # Exact file (fast path): use HTTPS, which avoids requiring gsutil.
        if not has_glob and not is_dir_prefix:
            output_path = fetch_job_artifact(
                job_url, cleaned, outdir_path, session=sess
            )
            print(
                f"[INFO] Downloaded artifact <{cleaned}> to <{output_path.as_posix()}>"
            )
            downloaded.append(output_path)
            continue

        if not gs_prefix:
            raise AppError(
                "Cannot resolve artifact glob/directory patterns because the job payload has no status.url with /view/gs/.",
                exit_code=2,
            )

        matches = _list_gcs_matches(
            gs_prefix=gs_prefix, pattern=cleaned, is_recursive=is_recursive
        )
        if not matches:
            hint = (
                "try adding a trailing '/' for a directory download"
                if not has_glob
                else "check the pattern"
            )
            raise AppError(
                f"No artifacts matched pattern <{cleaned}>. {hint}. Job: <{job_url}>",
                exit_code=2,
            )

        for obj in matches:
            if not gs_base or not obj.startswith(gs_base):
                # Best-effort fallback: place into outdir by basename.
                rel = Path(obj).name
            else:
                rel = obj[len(gs_base) :]

            local_dest = _safe_join_outdir(outdir=outdir_path, rel_path=rel)
            local_dest.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                ["gsutil", "cp", obj, str(local_dest)],
                check=False,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
                raise AppError(
                    f"Downloading artifact failed (gsutil exit {proc.returncode}). Source: <{obj}>\n{combined}",
                    exit_code=2,
                )
            downloaded.append(local_dest)
            print(f"[INFO] Downloaded artifact <{rel}> to <{local_dest.as_posix()}>")

    return downloaded


def download_job_artifacts(
    job: Mapping[str, Any],
    artifact_paths: list[str],
    *,
    outdir: str | Path | None = None,
    session: requests.Session | None = None,
) -> list[Path]:
    return download_job_artifacts_patterns(
        job, artifact_paths, outdir=outdir, session=session, recursive=False
    )


def download_job_artifact_dirs(
    job: Mapping[str, Any],
    dir_paths: list[str],
    *,
    outdir: str | Path | None = None,
    session: requests.Session | None = None,
) -> None:
    download_job_artifacts_patterns(
        job, dir_paths, outdir=outdir, session=session, recursive=True
    )
    return None


def get_truncated_hashed(token: str) -> str:
    hash_obj = hashlib.sha512(token.encode())
    hashed_token = hash_obj.hexdigest()
    return hashed_token[: len(hashed_token) // 2]


def get_gangway_token() -> str:
    global _gangway_token_cache
    if _gangway_token_cache is not None:
        return _gangway_token_cache

    token = os.getenv("GANGWAY_TOKEN")
    if not token:
        raise AppError(
            "GANGWAY_TOKEN is not set. Export it before using --trigger.",
            exit_code=2,
        )

    _gangway_token_cache = token
    return token


def fetch_job_artifact(
    job_url: str,
    artifact_path: str,
    outdir: str | Path | None = None,
    *,
    session: requests.Session | None = None,
    timeout_seconds: int = 60,
) -> Path:
    """
    Download an artifact (file) from a Prow job URL (prow view/gs or gcsweb).
    """
    if outdir is None:
        outdir = Path.cwd() / "artifacts"
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    job_artifacts_url = job_url.replace(f"{PROW_URL}/view/gs", PROW_GCS_WEB_URL)
    artifact_url = f"{job_artifacts_url.rstrip('/')}/{artifact_path.lstrip('/')}"
    if not artifact_path or artifact_path.strip() == "":
        raise AppError("Artifact path is empty.", exit_code=2, print_to_stderr=False)
    if artifact_path.strip().endswith("/"):
        raise AppError(
            f"Artifact path <{artifact_path}> looks like a directory. Use --download-artifact-dir (or pass a file path).",
            exit_code=2,
        )
    output_path = _safe_join_outdir(outdir=outdir, rel_path=artifact_path.lstrip("/"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and output_path.is_dir():
        raise AppError(
            f"Cannot write artifact to <{output_path.as_posix()}> because it is a directory. "
            f"Path <{artifact_path}> may be a directory; try adding a trailing '/' or use --download-artifact-dir.",
            exit_code=2,
        )

    sess = session or requests.Session()
    try:
        resp = sess.get(artifact_url, timeout=timeout_seconds)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as err:
        resp = err.response
        status = getattr(resp, "status_code", None)
        reason = getattr(resp, "reason", None)
        if status == 404:
            raise AppError(
                _format_http_error(
                    action="Downloading Prow job artifact",
                    url=artifact_url,
                    status_code=status,
                    reason=reason,
                )
                + " The job/artifact may have expired or the path is wrong.",
                exit_code=2,
            ) from None
        raise AppError(
            _format_http_error(
                action="Downloading Prow job artifact",
                url=artifact_url,
                status_code=status,
                reason=reason,
            ),
            exit_code=2,
        ) from None
    except requests.exceptions.RequestException as err:
        raise AppError(
            f"Downloading Prow job artifact failed due to a network error: {err}. URL: <{artifact_url}>",
            exit_code=2,
        ) from None

    if _looks_like_html_response(resp):
        raise AppError(
            f"Refusing to save HTML response for artifact <{artifact_path}>. "
            f"This usually means the path is wrong or points at a directory listing. "
            f"URL: <{artifact_url}>",
            exit_code=2,
            print_to_stderr=False,
        )

    try:
        output_path.write_bytes(resp.content)
    except IsADirectoryError:
        raise AppError(
            f"Cannot write artifact to <{output_path.as_posix()}> because it is a directory. "
            f"Path <{artifact_path}> may be a directory; try adding a trailing '/' or use --download-artifact-dir.",
            exit_code=2,
        ) from None
    return output_path


def get_job_from_url(
    job_url: str,
    *,
    outdir: str | Path | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """
    Get the attributes of a Prow job from its URL (by downloading prowjob.json).
    """
    prowjob_path = fetch_job_artifact(job_url, "prowjob.json", outdir, session=session)
    job = json.loads(prowjob_path.read_text(encoding="utf-8"))
    print(
        f"[INFO] Matched Prow Job: <{job.get('metadata', {}).get('name', 'unknown')}>"
    )
    return job


def get_job_from_id(
    job_id: str, *, session: requests.Session | None = None, timeout_seconds: int = 60
) -> dict[str, Any]:
    """
    Get the attributes of a Prow job from its ID (via /prowjob?prowjob=<id>, YAML payload).
    """
    job_yaml_url = f"{PROW_URL}/prowjob"

    sess = session or requests.Session()
    try:
        resp = sess.get(
            job_yaml_url, params={"prowjob": job_id}, timeout=timeout_seconds
        )
        resp.raise_for_status()
        job_yaml = resp.text
    except requests.exceptions.HTTPError as err:
        resp = err.response
        status = getattr(resp, "status_code", None)
        reason = getattr(resp, "reason", None)
        url = f"{job_yaml_url}?prowjob={job_id}"
        if status == 404:
            raise AppError(
                _format_http_error(
                    action=f"Fetching ProwJob <{job_id}>",
                    url=url,
                    status_code=status,
                    reason=reason,
                )
                + " The ProwJob ID may be wrong, too old, or already garbage-collected.",
                exit_code=2,
            ) from None
        if status in (401, 403):
            raise AppError(
                _format_http_error(
                    action=f"Fetching ProwJob <{job_id}>",
                    url=url,
                    status_code=status,
                    reason=reason,
                )
                + " This looks like an authentication/authorization issue.",
                exit_code=2,
            ) from None
        raise AppError(
            _format_http_error(
                action=f"Fetching ProwJob <{job_id}>",
                url=url,
                status_code=status,
                reason=reason,
            ),
            exit_code=2,
        ) from None
    except requests.exceptions.RequestException as err:
        raise AppError(
            f"Fetching ProwJob <{job_id}> failed due to a network error: {err}. URL: <{job_yaml_url}?prowjob={job_id}>",
            exit_code=2,
        ) from None

    try:
        job = yaml.safe_load(job_yaml)
    except Exception as err:
        raise AppError(
            f"Fetched ProwJob <{job_id}>, but failed to parse YAML: {err}. URL: <{job_yaml_url}?prowjob={job_id}>",
            exit_code=2,
        ) from None

    if not isinstance(job, dict):
        raise AppError(
            f"Fetched ProwJob <{job_id}>, but got unexpected payload type: {type(job)}",
            exit_code=2,
        )

    return job


def wait_job_for_state(
    job: Mapping[str, Any],
    expected_states: list[str],
    *,
    poll_seconds: int = 90,
    max_seconds: int | None = None,
    artifacts_dir: str | Path | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """
    Wait until a Prow job reaches one of the expected states.
    """
    if artifacts_dir is None:
        artifacts_dir = Path.cwd() / "artifacts"
    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    job_id = str(job.get("metadata", {}).get("name", ""))
    job_state = str(job.get("status", {}).get("state", ""))

    start = time.monotonic()
    sess = session or requests.Session()
    last_refresh_warn: float | None = None

    while True:
        if max_seconds is not None and (time.monotonic() - start) > max_seconds:
            raise AppError(
                f"Timed out waiting for Prow Job <{job_id}> to reach one of {expected_states}; last state <{job_state}>.",
                exit_code=2,
            )

        try:
            refreshed = get_job_from_id(job_id, session=sess)
        except AppError as err:
            now = time.monotonic()
            if last_refresh_warn is None or (now - last_refresh_warn) > max(
                30, poll_seconds
            ):
                print(
                    f"[WARN] Failed to refresh Prow Job <{job_id}>: {err}",
                    file=sys.stderr,
                )
                last_refresh_warn = now
            refreshed = None
        except Exception as err:
            now = time.monotonic()
            if last_refresh_warn is None or (now - last_refresh_warn) > max(
                30, poll_seconds
            ):
                print(
                    f"[WARN] Failed to refresh Prow Job <{job_id}> due to an unexpected error: {err}",
                    file=sys.stderr,
                )
                last_refresh_warn = now
            refreshed = None

        if refreshed:
            job = refreshed
            job_id = str(job.get("metadata", {}).get("name", job_id))
            job_state = str(job.get("status", {}).get("state", job_state))

        if job_state in expected_states:
            print(f"[INFO] Prow Job <{job_id}> reached state <{job_state}>")
            break

        print(
            f"[INFO] Prow Job <{job_id}> state <{job_state}> is not in {expected_states}\n"
            "[INFO] Waiting before checking again..."
        )
        time.sleep(poll_seconds)

    # Archive the Prow job data locally
    (artifacts_dir / "prowjob.json").write_text(
        json.dumps(job, indent=2), encoding="utf-8"
    )
    return dict(job)


def wait_job_completion(
    job: Mapping[str, Any],
    *,
    poll_seconds: int = 90,
    max_seconds: int | None = None,
    artifacts_dir: str | Path | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    completed_states = ["success", "failure", "aborted", "error"]
    return wait_job_for_state(
        job,
        completed_states,
        poll_seconds=poll_seconds,
        max_seconds=max_seconds,
        artifacts_dir=artifacts_dir,
        session=session,
    )


def trigger_job(
    job_name: str,
    custom_envs: Mapping[str, str] | None = None,
    custom_annotations: Mapping[str, str] | None = None,
    *,
    triggered_by: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """
    Trigger a Prow job on OpenShift CI via Gangway.
    """
    if triggered_by is None:
        triggered_by = f"{os.getenv('JOB_BASE_NAME', '')}{os.getenv('BUILD_DISPLAY_NAME', '')}".strip()

    if not triggered_by:
        triggered_by = os.getenv(
            "TRIGGERED_BY", os.getenv("HOSTNAME", "gangway.py script")
        )

    job_request: dict[str, Any] = {
        "job_execution_type": "1",
        "pod_spec_options": {
            "annotations": {
                "triggered-by": triggered_by,
            },
        },
    }

    if custom_envs:
        job_request["pod_spec_options"]["envs"] = dict(custom_envs)

    if custom_annotations:
        job_request["pod_spec_options"]["annotations"].update(dict(custom_annotations))

    print(json.dumps(job_request, indent=4))

    job_trigger_url = f"{GANGWAY_API_URL}/executions/{job_name}"
    token = get_gangway_token()
    sess = session or requests.Session()

    env_keys = sorted((custom_envs or {}).keys())
    annotation_keys = sorted((custom_annotations or {}).keys())
    print(
        f"[INFO] Triggering Prow Job <{job_name}>"
        f" (triggered_by={triggered_by!r}, env_keys={env_keys}, annotation_keys={annotation_keys})"
    )

    try:
        resp = sess.post(
            job_trigger_url,
            headers={"Authorization": f"Bearer {token}"},
            json=job_request,
            timeout=60,
        )
        resp.raise_for_status()
    except requests.exceptions.HTTPError as err:
        resp = err.response
        status = getattr(resp, "status_code", None)
        reason = getattr(resp, "reason", None)
        if status in (401, 403):
            raise AppError(
                _format_http_error(
                    action=f"Triggering Prow job <{job_name}> via Gangway",
                    url=job_trigger_url,
                    status_code=status,
                    reason=reason,
                )
                + " Check your Gangway token (GANGWAY_TOKEN) and permissions.",
                exit_code=2,
            ) from None
        raise AppError(
            _format_http_error(
                action=f"Triggering Prow job <{job_name}> via Gangway",
                url=job_trigger_url,
                status_code=status,
                reason=reason,
            ),
            exit_code=2,
        ) from None
    except requests.exceptions.RequestException as err:
        raise AppError(
            f"Triggering Prow job <{job_name}> via Gangway failed due to a network error: {err}. URL: <{job_trigger_url}>",
            exit_code=2,
        ) from None

    gangway_info = resp.json()
    job_id = gangway_info.get("id")
    if not job_id:
        raise AppError(
            f"Unexpected Gangway response (missing 'id'): {gangway_info}",
            exit_code=1,
        )

    job_id_s = str(job_id).strip()
    job = _try_get_job_from_id(job_id_s, session=sess) or {
        "metadata": {"name": job_id_s},
        "status": {},
    }
    return job


def _ensure_gsutil_available() -> None:
    if shutil.which("gsutil"):
        return

    raise RuntimeError("gsutil is still not available after installation attempt")


def process_job_results(
    job: Mapping[str, Any],
    *,
    artifacts_dir: str | Path | None = None,
) -> bool:
    """
    Process the results of a Prow job.

    Returns True for success, False otherwise.
    """
    state = str(job.get("status", {}).get("state", ""))
    return state == "success"


def _parse_env_kv_pairs(raw_pairs: list[str] | None) -> dict[str, str]:
    if not raw_pairs:
        return {}

    envs: dict[str, str] = {}
    for raw in raw_pairs:
        s = (raw or "").strip()
        if s == "":
            continue
        if "=" not in s:
            raise AppError(
                f"Invalid -e/--env value <{raw}>; expected KEY=VALUE.",
                exit_code=2,
            )
        key, value = s.split("=", 1)
        key = key.strip()
        if key == "":
            raise AppError(
                f"Invalid -e/--env value <{raw}>; KEY must not be empty.",
                exit_code=2,
            )
        if key in envs:
            print(
                f"[WARN] Duplicate -e/--env key <{key}>; overriding previous value.",
                file=sys.stderr,
            )
        envs[key] = value
    return envs


def _write_job_info_file(job_info: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        yaml.safe_dump(job_info, f, default_flow_style=False)
    print(f"[INFO] Wrote job info to <{out_path.as_posix()}>")


@dataclass(frozen=True)
class CliArgs:
    show_config: bool = False
    trigger: str | None = None
    wait: bool = False
    env: list[str] | None = None
    dry_run: bool = False
    wait_job_id: str | None = None
    traceback: bool = False
    download_artifact: list[str] | None = None
    download_artifact_dir: list[str] | None = None
    artifacts_dir: str | None = None
    write_job_info_file: str | None = None
    poll_interval: int = 90


def _parse_args(argv: list[str] | None = None) -> CliArgs:
    p = argparse.ArgumentParser(
        description="Helpers to interact with OpenShift CI Prow/Gangway."
    )
    p.add_argument(
        "--show-config",
        action="store_true",
        help="Print configuration (URLs, token fingerprint).",
    )
    p.add_argument(
        "--trigger", metavar="JOB_NAME", help="Trigger a Prow job via Gangway."
    )
    p.add_argument(
        "-e",
        "--env",
        action="append",
        metavar="KEY=VALUE",
        help="Custom env vars for --trigger (repeatable). Example: -e KEY=VALUE -e foo=bar",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate execution without triggering jobs or writing local artifacts.",
    )
    p.add_argument(
        "--wait",
        action="store_true",
        help="With --trigger, wait for the job to finish (and then download artifacts if requested).",
    )
    p.add_argument(
        "--wait-job-id",
        metavar="PROWJOB_ID",
        help="Wait for completion of a ProwJob by ID.",
    )
    p.add_argument(
        "--traceback",
        action="store_true",
        help="Print full traceback on errors (debugging).",
    )
    p.add_argument(
        "--download-artifact",
        action="append",
        metavar="REL_PATH",
        help=(
            "Download a job artifact by relative path (repeatable). "
            "Supports glob patterns; a trailing '/' implies recursive download. "
            "Example: --download-artifact build-log.txt --download-artifact artifacts/**/junit*.xml"
        ),
    )
    p.add_argument(
        "--download-artifact-dir",
        action="append",
        metavar="REL_DIR",
        help=(
            "Download all objects under an artifact directory/prefix (repeatable). "
            "Supports glob patterns; a trailing '/' implies recursive download. "
            "Example: --download-artifact-dir artifacts/ (requires job status.url)."
        ),
    )
    p.add_argument(
        "--artifacts-dir",
        default="artifacts",
        metavar="DIR",
        help="Destination directory for downloaded artifacts and archived job data (default: ./artifacts).",
    )
    p.add_argument(
        "--write-job-info-file",
        metavar="FILE",
        help="Write job information (ID, URLs) to a YAML file.",
    )
    p.add_argument(
        "--poll-interval",
        type=int,
        default=90,
        metavar="SECONDS",
        help="Polling interval in seconds when waiting for job completion (default: 90).",
    )
    ns = p.parse_args(argv)
    return CliArgs(
        show_config=ns.show_config,
        trigger=ns.trigger,
        wait=ns.wait,
        env=ns.env,
        dry_run=ns.dry_run,
        wait_job_id=ns.wait_job_id,
        traceback=ns.traceback,
        download_artifact=ns.download_artifact,
        download_artifact_dir=ns.download_artifact_dir,
        artifacts_dir=ns.artifacts_dir,
        write_job_info_file=ns.write_job_info_file,
        poll_interval=ns.poll_interval,
    )


def main() -> None:
    args = _parse_args()

    try:
        artifacts_dir = Path(args.artifacts_dir or "artifacts")
        if not args.dry_run:
            artifacts_dir.mkdir(parents=True, exist_ok=True)

        if args.show_config or (not args.trigger and not args.wait_job_id):
            token = os.getenv("GANGWAY_TOKEN")
            if token:
                print("GANGWAY_TOKEN (truncated): ", get_truncated_hashed(token))
            else:
                print("GANGWAY_TOKEN (truncated): <unset>")
            print(f"PROW_URL: {PROW_URL}")
            print(f"PROW_GCS_WEB_URL: {PROW_GCS_WEB_URL}")
            print(f"GANGWAY_API_URL: {GANGWAY_API_URL}")
            print(f"GANGWAY_SECRET_NAME: {GANGWAY_SECRET_NAME}")
            print(f"ARTIFACTS_DIR: {artifacts_dir.as_posix()}")

        sess = requests.Session()

        if args.env and not args.trigger:
            raise AppError("-e/--env can only be used with --trigger.", exit_code=2)

        if args.dry_run and (args.download_artifact or args.download_artifact_dir):
            raise AppError(
                "Artifact download flags cannot be used with --dry-run.",
                exit_code=2,
            )

        if args.trigger:
            if (args.download_artifact or args.download_artifact_dir) and not args.wait:
                raise AppError(
                    "Artifact download flags require --wait when used with --trigger.",
                    exit_code=2,
                )

            custom_envs = _parse_env_kv_pairs(args.env)
            if args.dry_run:
                env_kv = ", ".join(f"{k}={v}" for k, v in sorted(custom_envs.items()))
                print(
                    f"[INFO] Dry-run: would trigger Prow Job <{args.trigger}> via Gangway URL: <{GANGWAY_API_URL}/executions/{args.trigger}>"
                )
                print(f"[INFO] Dry-run: envs=[{env_kv}]")
                if args.wait:
                    print("[INFO] Dry-run: would wait for job completion.")
                raise SystemExit(0)

            job = trigger_job(args.trigger, custom_envs=custom_envs, session=sess)
            job_id = str(job.get("metadata", {}).get("name", "")).strip()
            if not job_id:
                raise AppError(
                    "Triggered job, but did not get a ProwJob ID back.",
                    exit_code=1,
                )
            _print_job_details(job_id=job_id, job=job)

            if not args.wait:
                if args.write_job_info_file:
                    info = {
                        "prowJobId": job_id,
                        "prowJobUrl": _job_url(job) or "",
                        "prowJobYamlUrl": _prowjob_yaml_url(job_id),
                    }
                    _write_job_info_file(info, Path(args.write_job_info_file))
                raise SystemExit(0)

            final = wait_job_completion(
                job,
                session=sess,
                artifacts_dir=artifacts_dir,
                poll_seconds=args.poll_interval,
            )
            if args.download_artifact:
                download_job_artifacts(
                    final,
                    args.download_artifact,
                    outdir=artifacts_dir,
                    session=sess,
                )
            if args.download_artifact_dir:
                download_job_artifact_dirs(
                    final,
                    args.download_artifact_dir,
                    outdir=artifacts_dir,
                    session=sess,
                )
            ok = process_job_results(final, artifacts_dir=artifacts_dir)
            raise SystemExit(0 if ok else 1)

        if args.wait_job_id:
            if args.dry_run:
                job = get_job_from_id(args.wait_job_id, session=sess)
                job_id = str(
                    job.get("metadata", {}).get("name", args.wait_job_id)
                ).strip()
                _print_job_details(job_id=job_id or args.wait_job_id, job=job)
                raise SystemExit(0)

            job = get_job_from_id(args.wait_job_id, session=sess)
            final = wait_job_completion(
                job,
                session=sess,
                artifacts_dir=artifacts_dir,
                poll_seconds=args.poll_interval,
            )
            if args.download_artifact:
                download_job_artifacts(
                    final,
                    args.download_artifact,
                    outdir=artifacts_dir,
                    session=sess,
                )
            if args.download_artifact_dir:
                download_job_artifact_dirs(
                    final,
                    args.download_artifact_dir,
                    outdir=artifacts_dir,
                    session=sess,
                )
            ok = process_job_results(final, artifacts_dir=artifacts_dir)
            if args.write_job_info_file:
                info = {
                    "prowJobId": args.wait_job_id,
                    "prowJobUrl": _job_url(final) or "",
                    "prowJobYamlUrl": _prowjob_yaml_url(args.wait_job_id),
                    "prowJobState": final.get("status", {}).get("state", ""),
                    "prowJobStartTime": final.get("status", {}).get("startTime", ""),
                    "prowJobCompletionTime": final.get("status", {}).get(
                        "completionTime", ""
                    ),
                    "prowJobPendingTime": final.get("status", {}).get(
                        "pendingTime", ""
                    ),
                }
                _write_job_info_file(info, Path(args.write_job_info_file))
            raise SystemExit(0 if ok else 1)

    except AppError as err:
        out = sys.stderr if getattr(err, "print_to_stderr", True) else sys.stdout
        print(f"[ERROR] {err}", file=out)
        raise SystemExit(err.exit_code)

    except KeyboardInterrupt:
        print("[ERROR] Interrupted.", file=sys.stderr)
        raise SystemExit(130)

    except Exception as err:
        if args.traceback:
            raise
        print(f"[ERROR] Unexpected error: {err}", file=sys.stderr)
        print(
            "[ERROR] Re-run with --traceback for a full stack trace.", file=sys.stderr
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
