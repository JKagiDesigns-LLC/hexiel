# hexiel benchmark

Measures the trade-off the project cares about: **accuracy** (do the hidden tests pass?), **speed** (wall time) and
**cloud cost** (tokens sent to the cloud model) for different ways of running hexiel.

## Tasks

Each folder in `tasks/` is a small project with a prompt, and a hidden test-suite that decides whether the result is right.

| id | what it checks |
|---|---|
| `intervals` | fixing subtly wrong helpers so they match a README (edge cases: touching intervals, mutation, empty input) |
| `ttlcache` | implementing a class from a precise spec (LRU + TTL rules, boundary conditions, thread safety) |
| `rename` | a multi-file rename that must not touch look-alike names (`calc_total_tax`, `line_items`) |
| `csvstats` | a command-line tool with exact output formats, sort rules, error messages and exit codes |

```
tasks/<id>/task.json   prompt, verify command, timeout
tasks/<id>/seed/       what the model starts with (its tests/ are the visible ones)
tasks/<id>/hidden/     the real tests, copied in only after the run
tasks/<id>/solution/   a reference solution, used by --self-check
```

## Configurations

| name | what runs |
|---|---|
| `local` | the local model alone |
| `local+advice` | local model that can `consult` the cloud model (and is asked for advice when stuck) |
| `delegate` | `/delegate`: cloud model plans, local model does each step |
| `delegate+review` | as above, plus the cloud model reviews the diff and the local model fixes what it finds, plus advice |
| `cloud` | the cloud model alone |

## Running

```
python bench/run.py --self-check                       # the tasks themselves: seed must fail, reference solution must pass
python bench/run.py --list
python bench/run.py --configs cloud                    # all tasks, one configuration
python bench/run.py --tasks intervals,rename --configs local,delegate+review --repeat 2
```

`--local` and `--cloud` name the model profiles from your `~/.config/hexiel/config.toml` (defaults `gmktec`, `anthropic-sonnet`).
Every run uses a fresh temporary project and an isolated config, so your own config, sessions and projects are untouched.
Results are written to `bench/results/<timestamp>.json` (plus per-run logs); the table at the end shows pass/fail,
mean time and mean cloud tokens per task and per configuration.

**Heads up:** `local*` and `delegate*` runs keep the local model server busy for their whole duration (minutes per run on
modest hardware), so other hexiel sessions using it will be slow meanwhile. Cloud tokens are counted from provider usage
reports; convert to money with your provider's current prices.

Small samples are noisy: a single run per cell shows a tendency, not a measurement. Use `--repeat` before drawing conclusions.
