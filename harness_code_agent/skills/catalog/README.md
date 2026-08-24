# Skill system

VeriForge uses two invocation surfaces:

- **User-invoked skills** set `disable-model-invocation: true` and appear as dynamic `/name` commands.
- **Model-invoked skills** omit that flag; only their concise descriptions enter the stable model catalog.

Full instructions and sibling references stay on disk until the selected workflow loads them with `read_skill_file`.

The engineering and productivity foundation is a reviewed fork of [mattpocock/skills](https://github.com/mattpocock/skills), synchronized from `main` at commit `5b15a47f2d7150f545fbcacbfe381787fc0230dc` (August 21, 2026). It is maintained locally and updated selectively rather than synchronized automatically.

Local adaptations:

- user workflows use VeriForge dynamic slash commands;
- model-invoked skills are composed through `read_skill_file`, not slash commands;
- `workflows` and `setup-workflows` replace author-branded names;
- upstream agent metadata is excluded because VeriForge owns registration and invocation;
- `to-spec`, `to-tickets`, and `implement` are optional, scope-sensitive workflow stages rather than a mandatory document chain;
- `skill-creator` and `find-skills` are explicit user workflows adapted to this repository catalog;
- excluded upstream workflows include `prototype`, `wayfinder`, `wizard`, `teach`, and other workflows that do not match this product's execution model;
- VeriForge-specific core skills cover guarded execution, frontend design, and frontend diagnosis.
