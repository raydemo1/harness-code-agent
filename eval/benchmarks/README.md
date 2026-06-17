# Eval Benchmarks

Adapters for running the harness agent on standard evaluation benchmarks.

## Terminal-Bench 2.0 (via Harbor)

### Prerequisites

```bash
# Install harbor framework
pip install harbor

# Docker must be running (or use --env daytona for cloud)
docker info

# Export your API credentials
export $(grep -v '^#' .env | xargs)
```

### Run

```bash
# Prepare a local Terminal-Bench 2.0 checkout, repair broken docker_image
# entries when needed, then run a single task
python eval/benchmarks/run_terminal_bench.py --task fix-git

# Run multiple tasks from the local repaired dataset
python eval/benchmarks/run_terminal_bench.py --task fix-git --task query-optimize

# Run the full local 2.0 dataset
python eval/benchmarks/run_terminal_bench.py --full

# Use Daytona instead of local Docker
python eval/benchmarks/run_terminal_bench.py --task fix-git --env daytona

# Force Harbor to build environments instead of using task docker_image
python eval/benchmarks/run_terminal_bench.py --task fix-git --force-build
```

### How it works

1. The launcher downloads a repo-local `terminal-bench-2` dataset archive when needed
2. It preserves task `docker_image` values that are pullable, and repairs only broken image references to known Docker Hub fallbacks
3. Harbor runs against the local dataset via `--path`, avoiding task metadata drift
4. `HarnessAgent.install()` clones our repo inside the task container
5. Harbor runs the headless terminal runner from `/app` in the container, with `PYTHONPATH` pointing at the uploaded agent code
6. Harbor evaluates the result using the task's `tests/test.sh`
