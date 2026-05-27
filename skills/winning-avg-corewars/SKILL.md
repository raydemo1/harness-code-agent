---
name: winning-avg-corewars
description: Guidance for developing CoreWars Redcode warriors that meet target win rates. Use when writing, optimizing, or debugging Redcode against known opponents, analyzing warrior strategies, running pmars battles, tuning steps/offsets, or improving average win rates.
---

# CoreWars Warrior Development

Use this skill when the task is to make a Redcode warrior beat specific
opponents or reach target average win rates. Treat every change as a hypothesis
and test it with repeatable battles.

## Workflow

1. Establish the rules.
   Confirm core size, max processes, max cycles, scoring, opponent list, required
   thresholds, and the command used to run battles.

2. Analyze opponents first.
   Read opponent source. Classify each as bomber, scanner, paper/replicator, imp,
   vampire, stone, clear, or hybrid. Record step sizes, timing, launch behavior,
   vulnerable offsets, and defensive patterns.

3. Choose a minimal strategy.
   Start with one archetype that directly counters the hardest opponent. Avoid
   multi-component warriors until the simple version has measurable behavior.

4. Test every change.
   Use `pmars` or the provided test script. Run enough rounds for stable signal,
   test all opponents, and record parameters with results.

5. Tune systematically.
   Grid-search or binary-search step sizes, offsets, gate positions, SPL counts,
   and clear lengths. Predict what each change should improve before testing it.

6. Diagnose losses.
   Run a single debug battle to see how the warrior dies: bombed early, outpaced,
   trapped, overwritten, process-starved, or cleared.

## Opponent Notes

- Slow bombers: fast paper, scanners, or offset choices that dodge predictable
  bombing.
- Papers/replicators: quick scanners, vampires, or fast core clears.
- Imps: imp gates and clears.
- Scanners: decoys, speed, or strategies that punish scanning delay.
- Core clears: early replication or disruption.

## Pitfalls

- Tuning without reading opponent code.
- Changing many parameters at once.
- Optimizing already-passing matchups while failing hard thresholds.
- Copying known warriors without understanding their step math.
- Trusting too few rounds.

## Done Criteria

- All opponents were analyzed.
- Required win rates are met across enough rounds.
- Parameter choices are justified.
- Failure modes for hard opponents are understood.
- Final warrior assembles without warnings or syntax errors.
