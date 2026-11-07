# Example: a weekly newsletter (lite mode)

Tierwright lite for non-engineering work. There are no job files and no shell
commands: the finish line is written as plain checks.

```bash
pip install git+https://github.com/Milokucia/Tierwright
cd examples/newsletter
tw init --lite                 # answers: backend, budget, goal, where the result goes, brand.md
tw adopt --no-model            # one step: the goal, gated by your checks
tw step show
```

Ask Claude Code to write `out/newsletter.md` (the `tw-ops` agent runs on
Sonnet, which is enough for a draft). Then:

```bash
tw check 'words: out/newsletter.md 300-500'
tw check 'contains: out/newsletter.md: acme.com/demo'
tw check 'lacks: out/newsletter.md: revolutionary, game-changing'
tw step check                  # runs every check; review: runs last, on Haiku
tw status
```

`goal.yaml` for this example:

```yaml
goal: "A weekly newsletter announcing this week's feature, on brand."
done:
  - "file: out/newsletter.md"
  - "words: out/newsletter.md 300-500"
  - "contains: out/newsletter.md: acme.com/demo"
  - "lacks: out/newsletter.md: revolutionary, game-changing, unleash"
  - "review: out/newsletter.md against brand.md: one call to action, short paragraphs"
```

Put the cheap checks first. `review:` is the only check that calls a model,
and it only runs once the others pass. A failed review names the brand rule
that was broken. To stop agents from rewriting your guide, list it under
`protected_paths` in `tierwright.yaml`.
