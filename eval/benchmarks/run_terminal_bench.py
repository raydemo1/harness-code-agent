from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT_BOOTSTRAP = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT_BOOTSTRAP) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT_BOOTSTRAP))

from eval.benchmarks.usage_metrics import collect_harbor_job_usage


DATASET_ARCHIVE_URL = (
    "https://github.com/harbor-framework/terminal-bench-2-1/archive/refs/heads/main.zip"
)
HARBOR_DATASET_ID = "terminal-bench@2.1"
LOCAL_DATASET_DIR_NAME = "terminal-bench-2-1"
TERMINAL_BENCH_LABEL = "Terminal-Bench 2.1"
DOCKERHUB_IMAGE_NAMESPACE = "alexgshaw"
DOCKERHUB_IMAGE_TAG = "20251031"


def resolve_harbor_executable(env: dict[str, str]) -> str:
    harbor_path = shutil.which("harbor") or shutil.which("harbor.exe")
    if harbor_path:
        return harbor_path

    candidates = [
        Path(env.get("USERPROFILE", "")) / ".local" / "bin" / "harbor.exe",
        Path(env.get("USERPROFILE", "")) / ".local" / "bin" / "harbor",
        Path(env.get("APPDATA", "")) / "Python" / "Python312" / "Scripts" / "harbor.exe",
        Path(env.get("USERPROFILE", "")) / "AppData" / "Roaming" / "Python" / "Python312" / "Scripts" / "harbor.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return "harbor"


def build_launch_environment(
    repo_root: Path,
    base_env: dict[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> dict[str, str]:
    env = dict(base_env or os.environ.copy())
    temp_dir = (repo_root / ".harbor" / "tmp").resolve()
    temp_dir.mkdir(parents=True, exist_ok=True)
    env["TEMP"] = str(temp_dir)
    env["TMP"] = str(temp_dir)
    env.setdefault("MAX_AGENT_ITERATIONS", "100")
    env.setdefault("MAX_AGENT_TOOL_CALLS", "400")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("TERM", "dumb")
    env.setdefault("RICH_FORCE_TERMINAL", "0")

    dotenv_file = dotenv_path or (repo_root / ".env")
    if dotenv_file.exists():
        for raw_line in dotenv_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key and key not in env:
                env[key] = value

    return env


def default_local_dataset_path(repo_root: Path) -> Path:
    return (repo_root / ".harbor" / "datasets" / LOCAL_DATASET_DIR_NAME).resolve()


def resolve_harbor_dataset_path(dataset_path: Path) -> Path:
    tasks_root = dataset_path / "tasks"
    if is_valid_harbor_dataset(dataset_path):
        return tasks_root
    raise RuntimeError(
        f"Terminal-Bench 2.1 dataset at {dataset_path} must contain tasks/*/task.toml."
    )


def is_valid_harbor_dataset(dataset_path: Path) -> bool:
    return any(_task_files(dataset_path, None))


def _download_and_extract_dataset_archive(dataset_path: Path) -> None:
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(dataset_path.parent)) as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        archive_path = temp_dir / f"{LOCAL_DATASET_DIR_NAME}.zip"
        urllib.request.urlretrieve(DATASET_ARCHIVE_URL, archive_path)
        extract_dir = temp_dir / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(extract_dir)

        extracted_roots = [path for path in extract_dir.iterdir() if path.is_dir()]
        if len(extracted_roots) != 1:
            raise RuntimeError(
                f"Expected exactly one extracted root in {extract_dir}, found {len(extracted_roots)}"
            )

        source_root = extracted_roots[0]
        dataset_path.mkdir(parents=True, exist_ok=True)
        for child in source_root.iterdir():
            shutil.move(str(child), str(dataset_path / child.name))


def ensure_local_dataset(dataset_path: Path) -> Path:
    resolved = dataset_path.resolve()
    if is_valid_harbor_dataset(resolved):
        return resolved

    if resolved.exists():
        shutil.rmtree(resolved)

    _download_and_extract_dataset_archive(resolved)

    if not is_valid_harbor_dataset(resolved):
        raise RuntimeError(
            f"Local dataset checkout at {resolved} does not contain Harbor tasks."
        )

    return resolved


def repair_task_images(dataset_path: Path, tasks: list[str] | None = None) -> int:
    repaired = 0
    task_files = _task_files(dataset_path, tasks)
    for task_file in task_files:
        task_name = task_file.parent.name
        content = task_file.read_text(encoding="utf-8")
        image = _task_docker_image(content)
        if not image or _docker_image_exists(image):
            continue

        fallback = f"{DOCKERHUB_IMAGE_NAMESPACE}/{task_name}:{DOCKERHUB_IMAGE_TAG}"
        if not _docker_image_exists(fallback):
            continue

        updated = _replace_task_docker_image(content, fallback)
        if updated != content:
            task_file.write_text(updated, encoding="utf-8")
            repaired += 1
    return repaired


def rewrite_task_images_to_ghcr(dataset_path: Path) -> int:
    """Backward-compatible name for older tests/integrations.

    The old implementation rewrote every task to a guessed GHCR image. Some
    Some Terminal-Bench tasks do not have a public GHCR package, so the safe
    behavior is now to repair only broken image references.
    """
    return repair_task_images(dataset_path)


def _task_files(dataset_path: Path, tasks: list[str] | None) -> list[Path]:
    tasks_root = dataset_path / "tasks"
    if tasks:
        return [
            tasks_root / task / "task.toml"
            for task in tasks
            if (tasks_root / task / "task.toml").exists()
        ]
    return list(tasks_root.glob("*/task.toml"))


def _task_docker_image(content: str) -> str:
    match = re.search(r'^docker_image\s*=\s*"([^"]+)"\s*$', content, flags=re.MULTILINE)
    return match.group(1) if match else ""


def _replace_task_docker_image(content: str, image: str) -> str:
    return re.sub(
        r'^docker_image\s*=\s*".*"$',
        f'docker_image = "{image}"',
        content,
        flags=re.MULTILINE,
    )


def _docker_image_exists(image: str) -> bool:
    try:
        completed = subprocess.run(
            ["docker", "manifest", "inspect", image],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def build_harbor_run_command(
    harbor_executable: str,
    tasks: list[str],
    runner_env: str | None,
    dataset_path: Path | None,
    force_build: bool,
) -> list[str]:
    command = [harbor_executable, "run"]
    if dataset_path is None:
        command.extend(["-d", HARBOR_DATASET_ID])
    else:
        command.extend(["--path", str(dataset_path)])

    command.extend(["--agent-import-path", "eval.benchmarks.harbor_agent:HarnessAgent"])

    if runner_env:
        command.extend(["--env", runner_env])
    if force_build:
        command.append("--force-build")
    for task in tasks:
        command.extend(["--include-task-name", task])

    return command


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=f"Run {TERMINAL_BENCH_LABEL} from a local dataset with repaired prebuilt image references."
    )
    parser.add_argument(
        "--task",
        action="append",
        default=[],
        help="Task name to run. Repeat to include multiple tasks.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run the full local dataset. If omitted and no --task is given, the full dataset is still run.",
    )
    parser.add_argument(
        "--dataset-path",
        type=Path,
        default=None,
        help=f"Local dataset directory. Defaults to repo-local .harbor/datasets/{LOCAL_DATASET_DIR_NAME}",
    )
    parser.add_argument(
        "--env",
        dest="runner_env",
        default=None,
        help="Harbor environment backend, for example daytona.",
    )
    parser.add_argument(
        "--force-build",
        action="store_true",
        help="Force Harbor to build environments locally instead of using task docker_image.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[2]
    env = build_launch_environment(repo_root)
    harbor_executable = resolve_harbor_executable(env)

    tasks = [] if args.full else args.task
    dataset_path = ensure_local_dataset(args.dataset_path or default_local_dataset_path(repo_root))
    if args.runner_env != "daytona" and not docker_daemon_running():
        print("Docker daemon is not running. Please start Docker and try again.", file=sys.stderr)
        return 125
    repaired_count = repair_task_images(dataset_path, None if args.full else tasks)
    command = build_harbor_run_command(
        harbor_executable=harbor_executable,
        tasks=tasks,
        runner_env=args.runner_env,
        dataset_path=resolve_harbor_dataset_path(dataset_path),
        force_build=args.force_build,
    )

    print(f"Using TEMP/TMP: {env['TEMP']}")
    print(f"Prepared local dataset: {dataset_path}")
    print(f"Repaired {repaired_count} broken task docker_image entries")
    print(f"Running: {' '.join(command)}")

    jobs_root = repo_root / "jobs"
    before_jobs = _job_names(jobs_root)
    completed = subprocess.run(command, cwd=repo_root, env=env)
    for job_dir in _new_job_dirs(jobs_root, before_jobs):
        summary = collect_harbor_job_usage(job_dir)
        print("HCA_TERMINAL_BENCH_RESULT:" + json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return completed.returncode


def docker_daemon_running() -> bool:
    try:
        completed = subprocess.run(
            ["docker", "info", "--format", "{{json .ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _job_names(jobs_root: Path) -> set[str]:
    if not jobs_root.exists():
        return set()
    return {path.name for path in jobs_root.iterdir() if path.is_dir()}


def _new_job_dirs(jobs_root: Path, before: set[str]) -> list[Path]:
    if not jobs_root.exists():
        return []
    new_dirs = [path for path in jobs_root.iterdir() if path.is_dir() and path.name not in before]
    return sorted(new_dirs, key=lambda item: item.stat().st_mtime)


if __name__ == "__main__":
    raise SystemExit(main())
