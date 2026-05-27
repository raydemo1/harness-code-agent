---
name: mteb-leaderboard
description: Guidance for answering current ML leaderboard and benchmark questions. Use when finding or comparing model rankings on MTEB, HuggingFace leaderboards, embedding benchmarks, dated leaderboard snapshots, benchmark APIs, or raw JSON/CSV result data.
---

# MTEB Leaderboard Queries

Leaderboard answers are time-sensitive. Use live or dated authoritative data and
state the source date. Do not rely on memory for current rankings.

## Workflow

1. Identify the benchmark and date.
   Clarify whether the user wants current results or standings as of a specific
   date. If the date matters, search for snapshots, releases, commits, or archived
   data from that period.

2. Prefer primary sources.
   Use official leaderboard pages, HuggingFace Spaces, benchmark repos, raw
   JSON/CSV files, APIs, or maintainer releases before papers or blog posts.

3. Find raw data when the page is interactive.
   Inspect app repositories, network endpoints, Gradio/Space APIs, data files,
   or leaderboard exports. Do not give up because a JavaScript table does not
   render in a text fetch.

4. Validate eligibility.
   Check whether the leaderboard includes API models, closed models, multilingual
   models, rerankers, fine-tunes, or task-specific submissions. Do not exclude a
   model type without source evidence.

5. Cross-check.
   Compare at least two sources when practical. If sources disagree, prefer the
   most recent authoritative source and mention the discrepancy.

## Output Requirements

- Model name exactly as listed.
- Ranking or score requested by the user.
- Benchmark subset or task family.
- Source link and source date or last update.
- Any eligibility or temporal caveat.

## Pitfalls

- Using an academic paper for a current leaderboard question.
- Ignoring "as of" dates.
- Confusing task-specific rankings with aggregate rankings.
- Assuming an interactive page has no accessible data.
- Normalizing model names so much that they no longer match the source.

## Done Criteria

- Ranking was verified against an authoritative source.
- The source date matches the user's temporal requirement.
- Model eligibility was checked.
- The answer cites the data source and caveats.
