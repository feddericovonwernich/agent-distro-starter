---
name: distro-customize
description: Customize supported private agent distro configuration.
---

# Supported private configuration

`config/workers.json` and `config/worker-skills/` are the supported worker-skill configuration paths. They define flat profile skill lists and local native bundles only; they are not a general configuration system. Keep `agd-` bundle IDs, matching `SKILL.md` frontmatter names, and regular local resources. Inspect with `worker skills list` and `worker profiles list`. Other `config/` paths remain unsupported unless documented by a command.
