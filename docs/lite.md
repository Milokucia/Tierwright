# Tierwright lite: for people who aren't engineers

You use Claude Code for real work (marketing, research, operations), and you
want it to cost less and finish properly. Lite mode does three things:

1. **The right model for each task.** Quick extraction and checking run on
   Haiku, drafts on Sonnet, strategy on Opus. You don't choose each time;
   the project decides.
2. **A budget.** You set a daily limit and a limit per goal. When either is
   reached, work stops and `owner-queue.md` says so. Spending is never
   silent.
3. **A finish line Claude can't fake.** You write what "done" means in plain
   words, and Tierwright checks it.

## Setup (5 minutes)

You need Claude Code and Python 3.10 or newer.

```bash
pip install git+https://github.com/Milokucia/Tierwright
cd my-project-folder
tw init --lite
```

It asks:

- **Subscription, API or both?** Pick subscription if you use a Claude plan.
- **Daily budget and goal budget, in USD.**
- **Your goal**, in one sentence.
- **Where the finished result will be saved**, for example
  `out/newsletter.md`.
- **A guide it must follow**, if any, for example `brand.md`.

## Writing the finish line

`goal.yaml` lists checks. All of them must pass.

| Check | Means |
|---|---|
| `file: out/post.md` | the file exists and isn't empty |
| `words: out/post.md 300-500` | word count in range (also `>=300` or `<=500`) |
| `contains: out/post.md: Acme, acme.com/demo` | every term appears (any capitalization) |
| `lacks: out/post.md: lorem, TODO, revolutionary` | none of these appear |
| `review: out/post.md against brand.md: one call to action` | a Haiku reviewer checks the file against your guide; a fail names the broken rule |

Put the cheap checks first. `review:` is the only one that calls a model, and
it only runs once the others pass.

## Day to day

```bash
tw status                  # where things stand, in under 20 lines
tw step show               # the one thing to work on now
tw check 'words: out/post.md 300-500'    # try any check by hand
tw step check              # run the step's checks; passing moves you on
tw spend                   # what each kind of task cost
tw mine                    # after a week: turn what Claude repeats into one-call tools
```

Tell Claude Code to "work on the current Tierwright step". The `CLAUDE.md`
that `tw init` wrote tells it how.

## Keep your guide safe

Add it to `tierwright.yaml` so agents can't edit it:

```yaml
hooks:
  protected_paths: ["brand.md", "approved/*"]
```
