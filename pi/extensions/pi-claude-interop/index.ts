/**
 * pi-claude-interop
 *
 * Bridges Claude Code assets to pi so they interoperate automatically:
 *
 *  - Provider config (models.json.tmpl bundled with this extension)
 *      → rendered with process.env substitution; specs (cost, contextWindow, maxTokens,
 *        reasoning, input) looked up from pi's built-in Anthropic model registry
 *      → only active when the required env vars (ANTHROPIC_BASE_URL, model IDs) are set
 *
 *  - Slash commands (.claude/commands/**\/*.md, ~/.claude/commands/**\/*.md)
 *      → registered as pi prompt templates (syntax is identical: $ARGUMENTS, $1, etc.)
 *
 *  - Skills (.claude/skills/, ~/.claude/skills/)
 *      → registered as pi skill paths
 *
 *  - Rules (.claude/rules/)
 *      → injected into the system prompt with read-on-demand links
 *
 *  - /claude-interop command → show a status report
 */

import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { getModel, getModels } from "@mariozechner/pi-ai";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Recursively find all .md files under `dir`.
 * Returns paths relative to `dir` (e.g. "subdir/command.md").
 */
function findMarkdownFiles(dir: string, rel = ""): string[] {
	const results: string[] = [];
	if (!fs.existsSync(dir)) return results;

	for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
		const relPath = path.join(rel, entry.name);
		if (entry.isDirectory()) {
			results.push(...findMarkdownFiles(path.join(dir, entry.name), relPath));
		} else if (entry.isFile() && entry.name.endsWith(".md")) {
			results.push(relPath);
		}
	}

	return results;
}

// ---------------------------------------------------------------------------
// Extension
// ---------------------------------------------------------------------------

export default async function claudeInteropExtension(pi: ExtensionAPI) {
	// ---------------------------------------------------------------------------
	// Provider config — render bundled models.json.tmpl with process.env, then
	// enrich each resolved model with specs from pi's built-in Anthropic model
	// registry (cost, contextWindow, maxTokens, reasoning, input).  Falls back to
	// safe defaults for model IDs not yet in the registry.  Skipped when the
	// required env vars are absent (e.g. direct Anthropic API usage).
	// ---------------------------------------------------------------------------
	const tmplPath = path.join(__dirname, "models.json.tmpl");
	if (fs.existsSync(tmplPath)) {
		const raw = fs.readFileSync(tmplPath, "utf8");
		const rendered = raw.replace(/\$\{([^}]+)\}/g, (_, k) => process.env[k] ?? "");
		const config = JSON.parse(rendered) as { providers?: Record<string, any> };
		for (const [name, providerCfg] of Object.entries(config.providers ?? {})) {
			const { compat: providerCompat, models, ...rest } = providerCfg;
			// Skip entirely if the base URL wasn't resolved
			if (!rest.baseUrl) continue;
			// Filter out models whose ID wasn't resolved (env var not set)
			const resolvedModels = (models ?? []).filter((m: any) => m.id);
			if (resolvedModels.length === 0) continue;
			pi.registerProvider(name, {
				...rest,
				models: resolvedModels.map((m: any) => {
					// Exact match first; fall back to the registry model whose canonical ID
					// appears as a substring of the Databricks alias (e.g.
					// "databricks-claude-sonnet-4-6" → "claude-sonnet-4-6"), preferring
					// the longest matching canonical ID.
					const known =
						getModel("anthropic", m.id) ??
						getModels("anthropic")
							.filter((r) => m.id.includes(r.id))
							.sort((a, b) => b.id.length - a.id.length)[0];
					return {
						// Fill in full specs from pi's registry; fall back to safe defaults
						// Strip " (latest)" suffix — only meaningful inside pi's own registry.
						name: (known?.name ?? m.id).replace(/ \(latest\)$/i, ""),
						reasoning: known?.reasoning ?? true,
						input: (known?.input ?? ["text", "image"]) as ("text" | "image")[],
						cost: known?.cost ?? { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
						contextWindow: known?.contextWindow ?? 200000,
						maxTokens: known?.maxTokens ?? 64000,
						// Template fields take precedence (id, reasoning overrides, etc.)
						...m,
						// Merge provider-level compat → registry compat → model-level compat
						compat: { ...providerCompat, ...(known as any)?.compat, ...m.compat },
					};
				}),
			});
		}
	}

	const home = os.homedir();
	let cwd = process.cwd();

	// Populated during resources_discover / before_agent_start; reused by /claude-interop
	let discoveredSkillPaths: string[] = [];
	let discoveredCommandFiles: string[] = [];
	let discoveredRuleFiles: string[] = [];

	// ---------------------------------------------------------------------------
	// session_start — capture working directory
	// ---------------------------------------------------------------------------
	pi.on("session_start", (_event, ctx) => {
		cwd = ctx.cwd;
	});

	// ---------------------------------------------------------------------------
	// resources_discover — register skills and commands (as prompt templates)
	// ---------------------------------------------------------------------------
	pi.on("resources_discover", (_event) => {
		const skillPaths: string[] = [];
		const promptPaths: string[] = [];

		// ── Skills ──────────────────────────────────────────────────────────────
		for (const dir of [
			path.join(home, ".claude", "skills"),
			path.join(cwd, ".claude", "skills"),
		]) {
			if (fs.existsSync(dir)) skillPaths.push(dir);
		}

		// ── Commands → Prompt templates ──────────────────────────────────────────
		// Claude Code commands use $ARGUMENTS / $1 / $@ – identical to pi templates.
		// Files are enumerated individually so subdirectory commands are included.
		const commandFiles: string[] = [];
		for (const dir of [
			path.join(home, ".claude", "commands"),
			path.join(cwd, ".claude", "commands"),
		]) {
			const files = findMarkdownFiles(dir).map((rel) => path.join(dir, rel));
			commandFiles.push(...files);
			promptPaths.push(...files);
		}

		discoveredSkillPaths = skillPaths;
		discoveredCommandFiles = commandFiles;

		return { skillPaths, promptPaths };
	});

	// ---------------------------------------------------------------------------
	// before_agent_start — inject .claude/rules/ into the system prompt
	// ---------------------------------------------------------------------------
	pi.on("before_agent_start", (event, _ctx) => {
		const rulesDir = path.join(cwd, ".claude", "rules");
		discoveredRuleFiles = findMarkdownFiles(rulesDir);

		if (discoveredRuleFiles.length === 0) return;

		const list = discoveredRuleFiles.map((f) => `- .claude/rules/${f}`).join("\n");

		return {
			systemPrompt:
				event.systemPrompt +
				`\n\n## Project Rules (Claude Code)\n\n` +
				`The following project rules are available in \`.claude/rules/\`:\n\n` +
				`${list}\n\n` +
				`When working on tasks that relate to these rules, use the read tool ` +
				`to load the relevant file(s) before proceeding.`,
		};
	});

	// ---------------------------------------------------------------------------
	// /claude-interop command — show a status report
	// ---------------------------------------------------------------------------
	pi.registerCommand("claude-interop", {
		description: "Show Claude Code interop status (commands, skills, rules)",
		handler: (_args, ctx) => {
			const lines: string[] = ["── Claude Code Interop Status ──────────────────────────"];

			// Commands / Prompt templates
			lines.push("");
			lines.push(`Commands → Prompt templates (${discoveredCommandFiles.length}):`);
			if (discoveredCommandFiles.length === 0) {
				lines.push("  (none — create .md files in .claude/commands/)");
			} else {
				for (const f of discoveredCommandFiles) {
					const rel = f.startsWith(home) ? `~${f.slice(home.length)}` : path.relative(cwd, f);
					lines.push(`  /${path.basename(f, ".md")}  ← ${rel}`);
				}
			}

			// Skills
			lines.push("");
			lines.push(`Skill paths (${discoveredSkillPaths.length}):`);
			if (discoveredSkillPaths.length === 0) {
				lines.push("  (none — create skills in .claude/skills/ or ~/.claude/skills/)");
			} else {
				for (const p of discoveredSkillPaths) {
					lines.push(`  ${p.startsWith(home) ? `~${p.slice(home.length)}` : p}`);
				}
			}

			// Rules
			lines.push("");
			lines.push(`Rules injected into system prompt (${discoveredRuleFiles.length}):`);
			if (discoveredRuleFiles.length === 0) {
				lines.push("  (none — create .md files in .claude/rules/)");
			} else {
				for (const f of discoveredRuleFiles) lines.push(`  .claude/rules/${f}`);
			}

			lines.push("");
			lines.push("────────────────────────────────────────────────────────");

			ctx.ui.notify(lines.join("\n"), "info");
		},
	});
}
