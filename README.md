# arz-powerbi-cli

A command-line tool for Power BI. It edits PBIP/TMDL projects (tables, measures, Power Query
imports, themes, pages, visuals and custom visuals) and talks to the Power BI REST API
(workspaces, datasets, refresh, DAX queries). It also has a REPL and `--json` output for scripts
and AI agents.

## Install

Python 3.10 or newer:

```bash
pip install "git+https://github.com/arz67mangos/arz-powerbi-cli.git#subdirectory=powerbi/agent-harness"
cli-anything-powerbi --help
```

Or clone and install in editable mode:

```bash
git clone https://github.com/arz67mangos/arz-powerbi-cli.git
cd arz-powerbi-cli/powerbi/agent-harness
pip install -e .
```

## Quick start

```bash
cli-anything-powerbi project new ./MyReport -n Shop
cli-anything-powerbi -p ./MyReport/Shop.pbip import csv ./orders.csv -t Orders
cli-anything-powerbi -p ./MyReport/Shop.pbip page template Dashboard -t Orders --create-measures
```

Open the `.pbip` in Power BI Desktop to see the result.

## Docs

- [Usage guide](powerbi/agent-harness/cli_anything/powerbi/README.md): every command, with examples
- [POWERBI.md](powerbi/agent-harness/POWERBI.md): how GUI actions map to CLI commands, and the file format notes
- [Agent skill](skills/cli-anything-powerbi/SKILL.md): for Claude Code and other AI agents
- [Tests](powerbi/agent-harness/cli_anything/powerbi/tests/TEST.md): `pip install -e .[test]` then `pytest cli_anything/powerbi/tests`

## License

MIT. See [LICENSE](LICENSE).
