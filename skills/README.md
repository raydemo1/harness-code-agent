# Skill system

HCA uses two invocation surfaces:

- **User-invoked skills** set `disable-model-invocation: true` and appear as dynamic `/name` commands.
- **Model-invoked skills** omit that flag; only their concise descriptions enter the stable model catalog.

Full instructions and sibling references stay on disk until the selected workflow loads them with `read_skill_file`.

The engineering and productivity foundation is a one-time fork of [mattpocock/skills](https://github.com/mattpocock/skills), based on release `v1.0.1` and commit `6eeb81b5fcfeeb5bd531dd47ab2f9f2bbea27461` from June 18, 2026. It is maintained locally rather than synchronized automatically.

Local adaptations:

- user workflows use HCA dynamic slash commands;
- model-invoked skills are composed through `read_skill_file`, not slash commands;
- `workflows` and `setup-workflows` replace author-branded names;
- excluded upstream workflows: `prototype`, `to-prd`, and `teach`;
- HCA-specific core skills cover PRD ownership, guarded execution, frontend design, and frontend diagnosis.
