---
name: frontend-debugging
description: Browser and UI debugging supplement for rendering, timing, stale state, animation, CSS, or Playwright-only failures. Use when diagnosing frontend visual glitches, React/Vue/Svelte render bugs, frame-loop drift, CSS transition flicker, browser-only behavior, or failing UI automation. Pair with diagnose for the overall bug loop.
---

# Frontend Debugging

Use this as a focused companion to `diagnose`. `diagnose` owns the overall loop:
feedback signal, reproduction, hypotheses, instrumentation, fix, and regression test.
This skill adds frontend-specific tactics once the bug smells like rendering, state,
timing, CSS, or browser automation.

## Workflow

1. Build an automated signal.
   Prefer a component/integration test, Playwright flow, screenshot comparison, or
   deterministic frame-loop harness over asking the user to manually inspect the UI.

2. Split logic from presentation.
   Test calculations and state transitions outside the browser when possible. If
   those pass but the UI is wrong, inspect DOM, computed styles, layout, animation,
   hydration, and browser events.

3. Expose state only in development.
   For hard UI bugs, temporarily expose a small `window.__debug` object in dev mode:

```js
if (import.meta.env.DEV) {
  window.__debug = {
    state,
    derived,
    lastEvent,
    frameTime,
  };
}
```

Remove or gate the diagnostic surface before finishing.

4. Isolate timing.
   Use fake timers, fixed timestamps, seeded randomness, and explicit frame steps.
   For animation loops, test many frames so drift and stale closures show up.

5. Verify in the browser when the symptom is visual.
   Use Playwright for interactions, console errors, network failures, screenshots,
   viewport changes, and focus/keyboard behavior. Do not rely on unit tests alone
   for layout or browser API issues.

6. Turn the repro into a regression check.
   Keep the narrowest stable test that catches the real bug pattern.

## Common Patterns

### CSS Transition Flicker

Symptom: state is correct, but the visual output flickers or lags.

Check:

```bash
rg "transition:|animation:" src
```

Frequently updated elements should usually not transition every frame. Remove or
scope transitions so React/render-loop updates are immediate.

### Stale State

Symptom: UI shows old data after props, store state, route params, or async data
change.

Check effects, dependency arrays, memoized selectors, closure capture, query cache
keys, and component keys. Re-render the component with changed inputs in a test.

### Calculation Drift

Symptom: values slowly diverge from expected output.

Test many deterministic steps:

```js
let state = initialState;
for (let i = 0; i < 10000; i++) {
  state = update(state, 16);
}
expect(state.value).toBeCloseTo(expected, 2);
```

### Browser-Only Failure

Symptom: tests pass outside the browser, but real use fails.

Check console errors, unsupported APIs, focus behavior, pointer events, layout
measurement, device pixel ratio, viewport size, and network timing.

## Anti-Patterns

- Guessing from screenshots without a reproducible check.
- Adding broad console logs instead of targeted instrumentation.
- Fixing CSS symptoms while state is still wrong.
- Testing mocks instead of the user-visible behavior.
- Leaving `window.__debug` or temporary logs in production paths.

## Done Criteria

- The user-reported visual or interaction symptom is reproduced.
- The root cause is classified as state, rendering, CSS, timing, browser API, or
  automation setup.
- A focused regression check catches the bug.
- Temporary diagnostics are removed or dev-only.
- The browser/manual check that originally failed now passes.
