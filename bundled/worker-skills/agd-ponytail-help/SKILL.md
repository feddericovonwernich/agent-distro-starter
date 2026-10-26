---
name: agd-ponytail-help
description: "Quick reference for ponytail levels, skills and commands. One-shot display. Use for /agd-ponytail-help, \"ponytail help\", \"how do I use ponytail\"."
---

# Ponytail Help

Display this reference card when invoked. One-shot, do NOT change mode,
write flag files, or persist anything.

## Levels

| Level | Trigger | What change |
|-------|---------|-------------|
| **Lite** | `/agd-ponytail lite` | Build what was asked, name the smaller option in one line. |
| **Full** | `/agd-ponytail` | The smallest complete change, a check where the logic needs one, and a reply that names what was skipped and any risk. Default. |
| **Ultra** | `/agd-ponytail ultra` | Also questions the request and pushes back before building. |

Level sticks until changed or session end.

## Skills

These are native OMP skill IDs in the worker bundle:

| Skill ID | Role |
|----------|------|
| **agd-ponytail** | Lazy mode itself: least new code, clear replies that name skipped work and risks. |
| **agd-ponytail-review** | Quality review of a diff: bugs, security, load, missing tests, speed, what to cut. |
| **agd-ponytail-audit** | Quality review for the whole repo, ranked. |
| **agd-ponytail-debt** | Harvest `shortcut:` comments into a tracked ledger. |
| **agd-ponytail-gain** | Measured-impact scoreboard: less code, less cost, more speed. |
| **agd-ponytail-help** | This card. |

AGD's default worker profile assigns all six. The orchestrator may select a
different profile or an explicit curated skill set for a task. This bundle
does not install or provide Ponytail plugins, hooks, bands, marketplace
updates, environment-variable activation, or a Ponytail config file.

## More

Pinned upstream docs and examples:
https://github.com/DietrichGebert/ponytail/tree/16352ac229eb08a9598dcae48fe9f2b342567d86
