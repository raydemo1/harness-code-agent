---
name: assisting-reverse-engineering
description: Guide for triaging unknown binaries and reverse-engineering program behavior. Use when analyzing executables, firmware, malware-like samples, crash artifacts, decompiled code, assembly, imports, strings, protocols, data structures, or anti-debugging behavior.
---

# Reverse Engineering Assistance

Use this skill to produce a structured, evidence-based analysis of an unknown
binary or low-level program artifact. Keep claims tied to observed strings,
imports, control flow, traces, or decompiler output.

## Workflow

1. Survey the artifact.
   Identify file type, architecture, bitness, compiler/runtime clues, entry
   points, imports/exports, sections, strings, and obvious packed or obfuscated
   regions.

2. Classify functions.
   Separate startup/runtime glue from custom logic. Prioritize entry points,
   exported functions, suspicious imports, protocol handlers, parsers, crypto,
   filesystem/network access, and error paths.

3. Map data flow.
   Trace inputs through parsing, validation, transformation, storage, and output.
   Reconstruct structures, global state, constants, stack layouts, and call-site
   invariants.

4. Validate behavior.
   Prefer dynamic traces, debugger breakpoints, emulation, or fixture inputs when
   available. Mark static-only conclusions with confidence levels.

5. Report findings.
   Summarize behavior, key functions, data structures, indicators, uncertainty,
   and recommended next manual steps.

## Output Shape

```md
## Binary Summary

## Key Functions

## Data Structures

## Strings And Indicators

## Behavior Analysis

## Confidence And Unknowns

## Recommended Next Steps
```

## Good Practices

- Start broad before deep-diving one function.
- Distinguish evidence from inference.
- Prefer stable names such as `parse_message`, `decrypt_config`, or
  `dispatch_command` once behavior is clear.
- Track confidence for each classification.
- Preserve exact offsets, addresses, symbols, strings, and hashes when useful.

## Pitfalls

- Treating library/runtime code as custom logic.
- Naming functions too early and anchoring on weak guesses.
- Ignoring dynamic behavior when static control flow is ambiguous.
- Reporting every string instead of strings that explain behavior.
- Omitting the input or trace that supports a conclusion.
