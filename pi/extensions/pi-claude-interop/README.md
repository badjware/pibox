# pi-claude-interop

A [pi](https://github.com/mariozechner/pi-coding-agent) extension that makes Claude Code assets automatically available inside pi, and registers a bundled provider config from environment variables.

## What it bridges

| Asset | Pi equivalent |
|---|---|
| `models.json.tmpl` (bundled) | Provider registered via `pi.registerProvider()` |
| `~/.claude/commands/**/*.md` | Prompt templates (`/command-name`) |
| `.claude/commands/**/*.md` | Prompt templates (`/command-name`) |
| `~/.claude/skills/` | Skill paths (auto-loaded) |
| `.claude/skills/` | Skill paths (auto-loaded) |
| `.claude/rules/**/*.md` | Injected into the system prompt |

## Provider config templating

At startup the extension reads `models.json.tmpl` (bundled alongside `index.ts`), substitutes `${VAR}` placeholders with `process.env`, and registers each provider via `pi.registerProvider()`. pi awaits the async factory before showing the first prompt, so the models are available immediately.

Registration is silently skipped when required variables are absent: if `baseUrl` is empty the whole provider is dropped; if a model's `id` is empty that model is dropped. The extension is therefore safe to load when not using the Databricks proxy.

To customise the provider config, edit `models.json.tmpl` (it lives next to `index.ts`).

## What it does NOT bridge

- `settings.json` (hooks, allowedTools, deniedTools) — not merged or applied

## Installation

```bash
# One-time global install
pi install /path/to/pi-claude-interop

# Or project-local
pi install -l /path/to/pi-claude-interop

# Or load ad-hoc for a single session
pi -e /path/to/pi-claude-interop/index.ts
```

## Slash commands → Prompt templates

Any `.md` file under `.claude/commands/` or `~/.claude/commands/` is registered
as a pi prompt template. Claude Code and pi use the **same syntax** for
argument interpolation (`$ARGUMENTS`, `$1`, `$@`), so no conversion is needed.

Example — `.claude/commands/review.md`:
```markdown
Review the following code for bugs and style issues:

$ARGUMENTS
```

In pi this becomes available as `/review <code>`.

Subdirectory commands are supported: `.claude/commands/git/commit.md` registers
as `/commit` (using the filename, not the full path).

## Skills

Any directory at `.claude/skills/` or `~/.claude/skills/` is added to pi's
skill search paths. Skills are discovered and loaded automatically by pi.

## Rules

`.md` files under `.claude/rules/` are **not** loaded eagerly (they can be
large). Instead, a list of the available rule files is appended to the system
prompt at the start of each agent turn, instructing the model to `read` the
relevant ones when needed.

## Status command

```
/claude-interop
```

Prints a summary of all discovered commands, skills, and rules for the current
working directory.
