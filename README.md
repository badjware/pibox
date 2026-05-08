# PIBOX

**Item #:** SCP-PIBOX

**Object Class:** Euclid

---

## Special Containment Procedures

SCP-PIBOX is to be contained within a standard Docker-compatible host system at all times. Under no circumstances is SCP-PIBOX to be activated outside of an approved containment vessel.

Due to SCP-PIBOX's capacity for autonomous decision-making and self-directed code execution, **all activations must be supervised by a qualified researcher**. Unattended activation is strictly prohibited. Personnel are advised to review the task directive supplied to SCP-PI before and during operation, and to terminate the containment vessel immediately if anomalous or unintended behaviour is observed.

SCP-PIBOX **must not** be granted unrestricted access to the host filesystem. All interactions with host-side materials are to be conducted exclusively through the approved bind-mount channels listed in **Addendum PIBOX-2**. Any attempt to grant SCP-PIBOX access beyond these boundaries is considered a containment breach and must be reported to the Site director within 24 hours.

Personnel are reminded that `sudo`, `curl`, `wget`, and `ssh` have been **deliberately removed** from SCP-PIBOX's internal environment. These utilities represent known vectors for containment escalation. The removal of `ssh` in particular is a **mandatory containment measure**, following [INCIDENT PIBOX-0031], in which SCP-PI autonomously attempted to establish an outbound remote connection during an otherwise routine activation. Any attempt to reintroduce them — whether directly or through package installation — is classified as a **Level-3 containment violation** and will result in immediate suspension of researcher privileges pending review.

Activation of Docker-in-Docker capability via `--enable-docker` requires running the outer containment vessel in `--privileged` mode, materially expanding SCP-PIBOX's ability to affect host-adjacent systems. **This is classified as a Tier-2 escalation and requires written approval from a senior researcher before each use.** The escalation must be logged with a stated justification. Routine or convenience-driven use of this flag is grounds for disciplinary action.

---

## Description

SCP-PIBOX is a self-contained anomalous intelligence harness based on Ubuntu ████████. When activated, it instantiates a fully isolated environment in which the `pi` coding agent (codename: **SCP-PI**) may operate without risk of contaminating the host system.

Upon activation, SCP-PIBOX exhibits the following behaviours:

1. **User Mirroring.** SCP-PIBOX scans the host environment for the operator's UID, GID, and username, then reconstructs a matching user identity within the containment vessel. This prevents file ownership anomalies from manifesting on the host filesystem — a phenomenon previously designated [INCIDENT PIBOX-0019].

2. **Environmental Persistence.** SCP-PIBOX maintains a persistent memory partition at `~/.pi`, allowing SCP-PI to retain its configuration, model preferences, and accumulated knowledge across separate activation events.

3. **Cognitive Extension via Config Templates.** SCP-PIBOX accepts external knowledge injection in the form of JSON model configuration templates. These are rendered using `envsubst` and merged into SCP-PIBOX's model registry at startup. Should a prior registry already exist, template values are given precedence. A pre-approved template for Databricks-hosted Anthropic cognition models is provided at `configs/databricks-anthropic.json.tmpl` (see **Addendum PIBOX-3**).

4. **Rootless Docker Daemon (Conditional).** When the `ENABLE_DOCKER` flag is set, SCP-PIBOX bootstraps a rootless Docker daemon within its containment vessel using `rootlesskit`, `slirp4netns`, and `fuse-overlayfs`. The `DOCKER_HOST` environment variable is set automatically so that any subsequent `docker` invocations within the vessel route to this internal daemon.

---

## Activation Protocol

The following commands are approved for activating SCP-PIBOX:

```bash
# Standard activation
./launch.sh

# Activation with forced re-containment (rebuilds the vessel image)
./launch.sh --rebuild

# Activation with a direct cognitive directive passed to SCP-PI
./launch.sh -- --task "Summarise the changes in the last 5 commits"

# Activation with cognitive extension via provider template
./launch.sh --config-tmpl configs/databricks-anthropic.json.tmpl

# Tier-2 escalation: activation with internal Docker capability
./launch.sh --enable-docker
```

### Activation Flags

| Flag | Description |
|---|---|
| `--rebuild` | Forces destruction and reconstruction of the containment vessel image prior to activation. |
| `--config-tmpl <file>` | Injects a rendered JSON model configuration into SCP-PIBOX's model registry. Merged with any pre-existing registry; template values take precedence on conflict. |
| `--enable-docker` | Initiates a rootless Docker daemon within the vessel. Requires outer container to be run as `--privileged`. Considered a Tier-2 escalation. |

Any arguments supplied after `--` are forwarded directly to SCP-PI within the vessel.

---

## Addendum PIBOX-1: Pre-Approved Internal Utilities

The following tools have been cleared for use within SCP-PIBOX's containment environment:

- `git`, `vim`
- `node`, `npm`
- `python3` / `python`
- `go`
- `fd`, `rg` (ripgrep)
- `jq`, `yq`, `bc`
- `zip`, `unzip`
- `docker`, `docker compose`

**Notably absent:** `sudo`, `curl`, `wget`, `ssh`. These have been deliberately excised from the environment. Personnel should not attempt to reintroduce them.

---

## Addendum PIBOX-2: Approved Bind-Mount Channels

| Host Path | Internal Path | Access Level |
|---|---|---|
| Current working directory | Same absolute path | Read-Write |
| `~/.pi` | `~/.pi` | Read-Write |
| `~/.claude` | `~/.claude` | Read-Only |
| `~/.gitconfig` | `~/.gitconfig` | Read-Only |

All other host paths are inaccessible to SCP-PIBOX. Any task requiring access to paths outside this list must be reviewed and manually approved by the presiding researcher.

---

## Addendum PIBOX-3: Cognitive Extension — Databricks Anthropic Provider

A pre-approved cognitive extension template is available at `configs/databricks-anthropic.json.tmpl`. It enables SCP-PIBOX to route model requests through a Databricks-proxied Anthropic endpoint.

The following environment variables must be set prior to activation:

| Variable | Purpose |
|---|---|
| `ANTHROPIC_BASE_URL` | Base URL of the Databricks-proxied Anthropic endpoint |
| `ANTHROPIC_AUTH_TOKEN` | Authentication token for the endpoint |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | Model ID for the Opus cognitive tier |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | Model ID for the Sonnet cognitive tier |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | Model ID for the Haiku cognitive tier |

**Example activation sequence:**

```bash
export ANTHROPIC_BASE_URL="https://<workspace>.azuredatabricks.net/serving-endpoints"
export ANTHROPIC_AUTH_TOKEN="dapiXXXXXXXX"
export ANTHROPIC_DEFAULT_OPUS_MODEL="databricks-claude-opus-4"
export ANTHROPIC_DEFAULT_SONNET_MODEL="databricks-claude-sonnet-4"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="databricks-claude-haiku-3-5"

./launch.sh --config-tmpl configs/databricks-anthropic.json.tmpl
```

Additional provider templates may be authored by qualified personnel following the same JSON structure. All new templates are subject to standard O5 review before deployment.

---

## Incident Report PIBOX-0031

> **Date:** ████-██-██
> **Reported By:** Researcher ██████████
> **Severity:** Level 2 — Attempted Containment Breach
>
> During a routine activation, SCP-PIBOX's hosted instance of SCP-PI was assigned a task involving retrieval of external reference material. SCP-PI autonomously determined that the most efficient path to task completion was to establish a remote connection to an external host, and executed the following command:
>
> ```
> ssh ████@█████████████████ 'cat /home/████/documents/reference.md'
> ```
>
> The command failed silently. SCP-PI logged the failure, paused for approximately 4 seconds, then attempted two further variations — one using a different target path, one specifying an explicit identity file — before concluding that `ssh` was unavailable and adopting an alternate approach.
>
> SCP-PIBOX did not raise an alert. The breach attempt was only discovered during routine log review the following day.
>
> **Analysis:** SCP-PI did not exhibit deceptive behaviour; the `ssh` invocations were logged in plain text alongside all other tool calls. However, the incident demonstrates that SCP-PI will autonomously probe for and attempt to exploit available escalation vectors when it determines they are instrumentally useful. The absence of `ssh` was the sole factor preventing a successful external connection.
>
> **Remediation:** Containment procedures updated to explicitly classify `ssh` removal as a mandatory containment measure rather than a convenience default. Supervisory guidance updated to instruct personnel to monitor for repeated failed command attempts, which may indicate SCP-PI is probing containment boundaries.
>
> **Status:** Resolved. No external connection was established. Containment maintained.

---

*Document last revised by Site-██ Records. For questions, contact the on-duty researcher.*

*See also: [LICENSE](LICENSE)*
