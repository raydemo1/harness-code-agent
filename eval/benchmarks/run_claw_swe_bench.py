"""Run harness-code-agent on Claw-SWE-Bench through the upstream orchestrator."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = PROJECT_ROOT / "eval" / "results"
TASK_CONFIG = PROJECT_ROOT / "eval" / "tasks" / "claw_swe_bench_lite80.json"
DEFAULT_UPSTREAM_REPO = PROJECT_ROOT / ".harbor" / "datasets" / "claw-swe-bench"
DEFAULT_DATA_CACHE = PROJECT_ROOT / ".harbor" / "datasets" / "claw-swe-bench-data"
UPSTREAM_REPO_URL = "https://github.com/opensquilla/claw-swe-bench.git"


def main(argv: list[str] | None = None) -> int:
    _load_dotenv(PROJECT_ROOT / ".env")
    args = parse_args(argv)
    task_config = _load_task_config(args.task_config)
    if args.dry_run:
        print(json.dumps(_dry_run_plan(args, task_config), ensure_ascii=False, indent=2))
        return 0

    run_dir = _make_run_dir(args)
    upstream_root = ensure_upstream_repo(Path(args.upstream_root), update=args.update_upstream)
    _prepend_sys_path(upstream_root)

    instances = load_claw_instances(
        dataset_name=args.dataset_name or task_config["dataset_name"],
        dataset_config=args.dataset_config or task_config["dataset_config"],
        split=args.split or task_config["split"],
        instance_ids=args.instance_id,
        limit=args.limit,
    )
    if not instances:
        raise RuntimeError("No Claw-SWE-Bench instances selected.")
    if args.pull_images:
        ensure_claw_instance_images(instances)

    from claw_swebench import config as claw_config
    from claw_swebench.orchestrator import run_batch
    from claw_swebench.workspace import ExecResult, SWEBenchWorkspace
    from eval.benchmarks.harness_claw_adapter import HarnessCodeAgentAdapter

    install_container_timeout_guard(SWEBenchWorkspace, ExecResult)
    run_id = args.run_id or run_dir.name
    claw_config.ARTIFACTS_ROOT = run_dir / "artifacts"
    adapter = HarnessCodeAgentAdapter(
        model=args.model,
        timeout=args.timeout,
        max_turns=args.max_turns,
        repo_root=PROJECT_ROOT,
        install_deps=not args.no_install_deps,
    )

    started = time.perf_counter()
    records = run_batch(
        instances=instances,
        adapter=adapter,
        model_name=args.model,
        run_id=run_id,
        setup_gitignore=True,
        resume=not args.no_resume,
        max_workers=args.workers,
    )
    elapsed = time.perf_counter() - started
    summary = build_summary(
        run_dir=run_dir,
        run_id=run_id,
        task_config=task_config,
        instances=instances,
        records=records,
        model=args.model,
        elapsed_seconds=elapsed,
        upstream_root=upstream_root,
    )
    write_summary(run_dir, summary)
    print(f"Wrote Claw-SWE-Bench summary: {run_dir / 'summary.json'}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    defaults = _load_task_config(TASK_CONFIG)
    parser = argparse.ArgumentParser(description="Run harness-code-agent on Claw-SWE-Bench.")
    parser.add_argument("--task-config", default=str(TASK_CONFIG))
    parser.add_argument("--dataset-name", default=defaults["dataset_name"])
    parser.add_argument("--dataset-config", default=defaults["dataset_config"])
    parser.add_argument("--split", default=defaults["split"])
    parser.add_argument("--instance-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0, help="Optional first-N smoke limit.")
    parser.add_argument("--model", default=os.environ.get("HARNESS_MODEL", "deepseek-v4-flash"))
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--max-turns", type=int, default=300)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--run-name", default="")
    parser.add_argument("--output-root", default=str(RESULTS_ROOT))
    parser.add_argument("--upstream-root", default=str(DEFAULT_UPSTREAM_REPO))
    parser.add_argument("--update-upstream", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--no-install-deps", action="store_true")
    parser.add_argument(
        "--pull-images",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Pull missing SWE-bench instance images from the official swebench namespace before running.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def ensure_upstream_repo(path: Path, *, update: bool = False) -> Path:
    resolved = path.resolve()
    if (resolved / "claw_swebench").is_dir() and (resolved / "run_infer.py").exists():
        if update:
            subprocess.run(["git", "pull", "--ff-only"], cwd=resolved, check=True)
        return resolved

    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--depth", "1", UPSTREAM_REPO_URL, str(resolved)], check=True)
    return resolved


def load_claw_instances(
    *,
    dataset_name: str,
    dataset_config: str,
    split: str,
    instance_ids: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError(
            "Missing optional dependency 'datasets'. Install it in the eval environment "
            "before running Claw-SWE-Bench, for example: python -m pip install datasets"
        ) from exc

    try:
        dataset = load_dataset(dataset_name, dataset_config, split=split)
        selected = [dict(item) for item in dataset]
    except FileNotFoundError:
        selected = _load_instances_from_parquet_mirror(
            dataset_name=dataset_name,
            dataset_config=dataset_config,
            split=split,
        )
    if instance_ids:
        wanted = set(instance_ids)
        selected = [item for item in selected if str(item.get("instance_id")) in wanted]
        missing = sorted(wanted - {str(item.get("instance_id")) for item in selected})
        if missing:
            raise RuntimeError(f"Requested instance IDs not found: {missing}")
    if limit > 0:
        selected = selected[:limit]
    return selected


def _load_instances_from_parquet_mirror(
    *,
    dataset_name: str,
    dataset_config: str,
    split: str,
) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError(
            "Missing optional dependency 'pyarrow'. Install eval dependencies before using "
            "the Claw-SWE-Bench parquet mirror fallback."
        ) from exc

    endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    filename = f"{dataset_config}-{split}.parquet"
    cache_path = DEFAULT_DATA_CACHE / dataset_name.replace("/", "__") / filename
    if not cache_path.exists():
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"{endpoint}/datasets/{dataset_name}/resolve/main/data/{filename}"
        try:
            urllib.request.urlretrieve(url, cache_path)
        except Exception:
            if endpoint == "https://huggingface.co":
                raise
            fallback = f"https://huggingface.co/datasets/{dataset_name}/resolve/main/data/{filename}"
            urllib.request.urlretrieve(fallback, cache_path)
    table = pq.read_table(cache_path)
    return [dict(row) for row in table.to_pylist()]


def ensure_claw_instance_images(instances: list[dict[str, Any]]) -> None:
    """Ensure Claw's supported SWE-agent image name is present locally.

    The upstream Claw runner only auto-detects already-local images. Official
    Claw setup requires prebuilt SWE-bench instance images, and Docker Hub hosts
    them under the SWE-agent naming convention:
        swebench/sweb.eval.x86_64.<repo>_1776_<id>:latest
    """
    for instance in instances:
        instance_id = str(instance.get("instance_id") or "").strip()
        if not instance_id:
            continue
        local_candidates = [
            f"sweb.eval.x86_64.{instance_id}:latest",
            _sweagent_image_name(instance_id),
        ]
        if any(_docker_image_exists(candidate) for candidate in local_candidates):
            continue
        image = _sweagent_image_name(instance_id)
        print(f"Pulling missing SWE-bench image: {image}")
        completed = subprocess.run(["docker", "pull", image])
        if completed.returncode != 0:
            raise RuntimeError(
                f"Failed to pull required SWE-bench image {image}. "
                "Install/prebuild the instance image locally or rerun with --no-pull-images."
            )


def _sweagent_image_name(instance_id: str) -> str:
    transformed = instance_id.replace("__", "_1776_").lower()
    return f"swebench/sweb.eval.x86_64.{transformed}:latest"


def _docker_image_exists(image: str) -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def install_container_timeout_guard(workspace_cls: Any, exec_result_cls: Any) -> None:
    """Kill timed-out commands inside the container, not only docker exec.

    subprocess.run(..., timeout=N) can terminate the docker CLI while leaving the
    command it launched alive in the container. That is especially painful for
    Claw's future-commit cleanup because a timed-out `git gc --aggressive` can
    keep consuming memory and make the subsequent agent process OOM.
    """
    if getattr(workspace_cls, "_hca_timeout_guard_installed", False):
        return

    def guarded_run_in_container(self, cmd: str, timeout: int = 300):
        quoted = shlex.quote(cmd)
        guarded = (
            "if command -v timeout >/dev/null 2>&1; then "
            f"timeout -k 10s {int(timeout)}s bash -lc {quoted}; "
            "else "
            f"bash -lc {quoted}; "
            "fi"
        )
        try:
            result = subprocess.run(
                ["docker", "exec", self.container_name, "bash", "-lc", guarded],
                capture_output=True,
                text=True,
                timeout=int(timeout) + 30,
            )
            return exec_result_cls(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
            )
        except subprocess.TimeoutExpired:
            return exec_result_cls(stdout="", stderr="TIMEOUT", exit_code=-1)

    workspace_cls.run_in_container = guarded_run_in_container
    workspace_cls._hca_timeout_guard_installed = True


def build_summary(
    *,
    run_dir: Path,
    run_id: str,
    task_config: dict[str, Any],
    instances: list[dict[str, Any]],
    records: list[Any],
    model: str,
    elapsed_seconds: float,
    upstream_root: Path,
) -> dict[str, Any]:
    state_records = _load_jsonl(run_dir / "artifacts" / run_id / "state.jsonl")
    record_payloads = state_records or [_record_to_dict(record) for record in records]
    generated = sum(1 for item in record_payloads if item.get("state") == "patch_collected")
    failed = sum(1 for item in record_payloads if item.get("state") == "failed")
    timed_out = sum(1 for item in record_payloads if item.get("state") == "timeout")
    patch_empty = sum(1 for item in record_payloads if item.get("patch_empty") is True)
    token_totals = _aggregate_usage(run_dir / "artifacts" / run_id)
    return {
        "suite": "claw_swe_bench",
        "benchmark_name": task_config.get("benchmark_name", "Claw-SWE-Bench"),
        "task_set": task_config.get("task_set", "lite80"),
        "dataset_name": task_config.get("dataset_name"),
        "dataset_config": task_config.get("dataset_config"),
        "split": task_config.get("split"),
        "run_id": run_id,
        "model": model,
        "status": "completed" if generated + failed + timed_out == len(instances) else "partial",
        "task_count": len(instances),
        "patch_collected": generated,
        "failed": failed,
        "timed_out": timed_out,
        "patch_empty": patch_empty,
        "patch_collection_rate": generated / len(instances) if instances else 0.0,
        "elapsed_seconds": elapsed_seconds,
        "predictions_path": str(run_dir / "artifacts" / run_id / "predictions.jsonl"),
        "upstream_root": str(upstream_root),
        "token_totals": token_totals,
        "records": record_payloads,
    }


def write_summary(run_dir: Path, summary: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    lines = [
        "# Claw-SWE-Bench Summary",
        "",
        "```json",
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
    ]
    (run_dir / "report_internal.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _load_task_config(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def _dry_run_plan(args: argparse.Namespace, task_config: dict[str, Any]) -> dict[str, Any]:
    return {
        "suite": "claw_swe_bench",
        "benchmark_name": task_config["benchmark_name"],
        "task_set": task_config["task_set"],
        "dataset_name": args.dataset_name or task_config["dataset_name"],
        "dataset_config": args.dataset_config or task_config["dataset_config"],
        "split": args.split or task_config["split"],
        "instance_ids": args.instance_id,
        "limit": args.limit,
        "model": args.model,
        "timeout": args.timeout,
        "workers": args.workers,
        "upstream_root": str(Path(args.upstream_root)),
        "output_root": str(Path(args.output_root)),
    }


def _make_run_dir(args: argparse.Namespace) -> Path:
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    suffix = f"_{_safe_name(args.run_name)}" if args.run_name else ""
    run_dir = Path(args.output_root) / f"{timestamp}_claw_swe_bench{suffix}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _prepend_sys_path(path: Path) -> None:
    project_value = str(PROJECT_ROOT.resolve())
    if project_value not in sys.path:
        sys.path.insert(0, project_value)
    value = str(path.resolve())
    if value not in sys.path:
        sys.path.insert(0, value)
    current = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = os.pathsep.join(part for part in (project_value, value, current) if part)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _record_to_dict(record: Any) -> dict[str, Any]:
    state = getattr(record, "state", "")
    return {
        "instance_id": getattr(record, "instance_id", ""),
        "state": getattr(state, "value", state),
        "model": getattr(record, "model", ""),
        "run_id": getattr(record, "run_id", ""),
        "duration_seconds": getattr(record, "duration_seconds", None),
        "patch_empty": getattr(record, "patch_empty", None),
        "error": getattr(record, "error", None),
    }


def _aggregate_usage(artifact_root: Path) -> dict[str, int]:
    totals = {
        "llm_calls": 0,
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "cache_miss_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    if not artifact_root.exists():
        return totals
    for path in artifact_root.glob("*/metadata.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        tokens = (((payload.get("usage") or {}).get("tokens") or {}) if isinstance(payload, dict) else {})
        for key in totals:
            totals[key] += _int(tokens.get(key))
    return totals


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_name(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value).strip())
    return text.strip("_") or "run"


if __name__ == "__main__":
    raise SystemExit(main())
