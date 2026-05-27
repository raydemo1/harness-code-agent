---
name: circuit-fibsqrt
description: Guide for implementing gate-level arithmetic circuits in text-based simulators. Use when building combinational or sequential netlists for integer square root, Fibonacci, adders, comparators, multiplexers, feedback loops, or resource-constrained circuit tasks.
---

# Circuit Fibsqrt

Use this skill for text-based gate or netlist tasks where arithmetic must be
implemented from primitive logic. The priority is simulator semantics first,
then small tested components, then composition.

## Workflow

1. Learn the simulator.
   Read examples before writing gates. Confirm syntax, bit order, mux argument
   order, input preservation, feedback behavior, and convergence semantics.

2. Paper-trace the algorithm.
   Work small examples by hand before encoding logic. For `isqrt`, trace 0, 1,
   perfect squares, and just-above-square values. For Fibonacci, define exactly
   what iteration 0 and 1 produce.

3. Estimate resources.
   Multipliers can explode gate counts. Estimate width, adders, comparators,
   registers, and repeated logic before committing to an approach.

4. Build components first.
   Test primitive gates, muxes, half/full adders, ripple adders, subtractors,
   comparators, registers, and feedback loops independently.

5. Compose the algorithm.
   Keep signal names clear and widths explicit. Verify intermediate outputs such
   as `isqrt(n)` before feeding later stages such as `fib(isqrt(n))`.

6. Optimize only after correctness.
   Reduce gates, share components, or change algorithm only after tests isolate
   the expensive part.

## Checks

- Input bits are preserved if the simulator requires it.
- Mux select semantics are proven with a tiny circuit.
- Bit ordering is documented.
- Adders and comparators pass edge cases.
- Feedback loops converge or step as expected.
- Gate count is below the limit.

## Pitfalls

- Assuming mux argument order.
- Implementing a formula without hand traces.
- Using multiplication when comparison or repeated addition is enough.
- Getting `fib(k - 1)` instead of `fib(k)`.
- Treating event-driven feedback like synchronous clocked logic.

## Done Criteria

- Component tests pass.
- Integration tests cover zero, one, boundaries, and representative large inputs.
- Intermediate values are verified.
- Resource usage is known.
