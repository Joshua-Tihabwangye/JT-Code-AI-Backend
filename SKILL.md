---
name: master-fixer
description: Runs a deep multi-agent codebase analysis, blueprints the architecture, forms a defensive engineering plan, applies fixes, and strictly verifies them.
triggers:
    - "fix this issue end to end"
    - "deep fix"
---

# Master Engineering Flow

When a user requests a fix or a feature using this skill, you must execute the following 5-phase lifecycle sequentially. Do not skip any phase.

## Phase 1: Deep Architectural Analysis & Discovery

1. Deploy your parallel `deep research` sub-agents to map the affected areas of the codebase.
2. Isolate dependencies, state managers, data flows, and architectural layers impacting the prompt.
3. Explicitly look out for cross-cutting concerns (e.g., authentication, error handling boundaries, or database transaction locks).

## Phase 2: Structural Review & Codebase Analysis

1. Analyze framework-specific paradigms being used in the target modules.
2. Evaluate potential regression vectors or logic collision risks with adjacent systems.

## Phase 3: Technical Blueprint & Planning

1. Write out a comprehensive engineering plan before altering any files.
2. Your plan must detail:
    - Files to be modified or created.
    - Core logical changes to implement.
    - Edge cases to guard against.
    - Precise testing strategy (unit, integration, or lint tests required).

## Phase 4: Implementation

1. Execute the code modifications incrementally.
2. Ensure new additions closely follow existing project styles and architectural design patterns.

## Phase 5: Rigorous Verification

1. Run target build, formatting, and linting commands to prevent syntax regression.
2. Execute relevant test suites. If tests fail, automatically trace the issue, correct the code, and re-test.
3. Review your implementation to guarantee no unintended dependencies or structural anti-patterns were introduced.
