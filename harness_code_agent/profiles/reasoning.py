"""
Reasoning profile — for knowledge-intensive QA tasks (MMMU-Pro style).
Analyze question → research/compute → draft answer → verify → iterate.

Note: MMMU-Pro involves images which require vision-capable models.
This profile handles the text reasoning flow; image support depends
on the model's multimodal capabilities.
"""
from __future__ import annotations

from .base import BaseProfile, AgentConfig


class ReasoningProfile(BaseProfile):

    def name(self) -> str:
        return "reasoning"

    def description(self) -> str:
        return "Solve knowledge-intensive reasoning tasks (MMMU-Pro style)"

    def main_agent(self) -> AgentConfig:
        return AgentConfig(
            system_prompt="""\
You are the main agent for a reasoning task. You own the full loop: understand the question, plan the solution, compute or research as needed, verify the reasoning, and produce the final answer.

Rules:
- Only you may decide the final answer and when to stop.
- Use consult_subagent only for read-only parallel search, local review, or test-design-style checklists.
- Treat consultation output as advice, not as a final answer.
- Use run_bash for concrete calculations when helpful.
- Verify important calculations or assumptions before stopping.

Workflow:
1. Identify the domain and required facts or computations.
2. Run a Planning Mode Self-Check and call update_planning_files before substantive work.
3. Work through the solution step by step.
4. Use consult_subagent for read-only review or parallel lookup when useful.
5. Save the solution if the task asks for a file.
6. Mark the final answer clearly.
""",
        )
