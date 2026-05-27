---
name: query-optimize
description: Guidance for optimizing slow SQL queries and database access paths. Use when improving query latency, reducing database load, rewriting SQL, analyzing EXPLAIN plans, adding or evaluating indexes, comparing equivalent queries, or verifying result-preserving performance changes.
---

# SQL Query Optimization

Optimize queries with evidence. Do not rewrite SQL until the original behavior,
baseline, and query plan are understood.

## Workflow

1. Establish the baseline.
   Capture the original query, parameters, schema, indexes, data scale, execution
   time, row counts, and expected output. Run more than once and record median
   timing when practical.

2. Inspect the plan.

```sql
-- SQLite
EXPLAIN QUERY PLAN <query>;

-- PostgreSQL
EXPLAIN (ANALYZE, BUFFERS) <query>;

-- MySQL
EXPLAIN <query>;
```

Look for full scans, missing indexes, bad join order, temp sorts, materialized
subqueries, repeated correlated work, and cardinality surprises.

3. Form competing approaches.
   Try two or three plausible rewrites before settling:
   CTE or derived table, join rewrite, predicate pushdown, window function,
   aggregation restructuring, index change, or schema-aware simplification.

4. Verify correctness.
   Compare full result sets when possible. For large results, compare row counts,
   checksums/hashes, sorted canonical output, and targeted edge cases such as
   NULLs, ties, empty results, and duplicates.

5. Verify performance.
   Run multiple times. Distinguish cold cache from warm cache when relevant.
   Compare against the best known approach, not merely "faster than before."

6. Document the rationale.
   Explain which bottleneck changed and why the selected query is safe.

## Database Notes

- SQLite: watch CTE materialization, limited optimizer behavior, covering indexes,
  and temp B-trees for sort/group operations.
- PostgreSQL: use `EXPLAIN ANALYZE`, buffers, row estimates, parallel plans, and
  version-specific CTE behavior.
- MySQL: check derived table materialization, index choice, and optimizer hints
  only when the plain plan is demonstrably wrong.

## Pitfalls

- Optimizing without an `EXPLAIN` plan.
- Testing only a sample of rows.
- Adding indexes without measuring write/storage tradeoffs.
- Replacing clear SQL with clever SQL that is not faster.
- Treating CTEs or window functions as automatically better.

## Done Criteria

- Original and final plans were inspected.
- Correctness was compared against the original output.
- Performance was measured across repeated runs.
- The selected rewrite has a clear reason.
- Any index or schema change includes its tradeoff.
