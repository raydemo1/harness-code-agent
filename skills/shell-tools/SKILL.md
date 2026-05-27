---
name: shell-tools
description: Practical guidance for reliable shell data processing with jq, xargs, GNU parallel, find, sort, and pipelines. Use when transforming JSON/CSV/text streams, batching commands, parallelizing local work, safely handling filenames, or building reproducible command-line data workflows.
---

# Shell Tools

Use this skill to build shell pipelines that are correct, safe, and repeatable.
Prefer structured tools over brittle text parsing.

## Core Rules

- Parse JSON with `jq`, not `grep` or regex.
- Use null-delimited paths for filenames: `find -print0`, `xargs -0`.
- Quote variables and arguments.
- Start with one small input, then scale.
- Use `tee`, temporary files, or intermediate commands to inspect pipelines.
- Prefer `rg` for search when available.

## jq Patterns

```bash
jq '.' file.json
jq -r '.items[] | [.id, .email] | @tsv' file.json
jq '.items[] | select(.active == true)' file.json
jq -n --arg value "$VALUE" '{value: $value}'
```

For large arrays, stream compact records:

```bash
jq -c '.items[]' large.json
```

## xargs And find

Use null delimiters when filenames may contain spaces or newlines:

```bash
find . -name '*.log' -print0 | xargs -0 -n 20 gzip
```

Limit batches and parallelism explicitly:

```bash
xargs -n 1 -P 4 process < ids.txt
```

## GNU parallel

Use `parallel` for more complex concurrency, placeholders, progress, and rate
limits:

```bash
parallel -j 5 --delay 0.2 'curl -s "https://api.example.com/item/{}"' :::: ids.txt
```

## Data Shaping

```bash
LC_ALL=C sort file.txt
sort file.txt | uniq -c
cut -d',' -f1,3 file.csv
tr -d '\r' < input.txt > output.txt
```

## Windows Notes

When running in PowerShell, prefer native cmdlets for filesystem mutation and use
explicit UTF-8 encoding for text files. Use Unix-style pipelines only when the
environment actually provides the tools.

## Pitfalls

- Parsing JSON with `grep`.
- Building destructive commands from untrusted text.
- Forgetting `-0` with filenames.
- Running unlimited parallel jobs against APIs.
- Hiding failures in long pipelines without checking exit codes.

## Done Criteria

- The pipeline handles spaces and special characters where relevant.
- The transformation is tested on representative input.
- Parallelism or batching is bounded.
- Output format is explicit and reproducible.
