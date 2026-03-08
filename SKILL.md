---
name: openproject-pm-knowledge
description: Manage OpenProject work packages and lightweight project knowledge artifacts (weekly summaries and decision logs). Use when work requires reading project/work package status and metadata, creating or updating work packages, managing relations, adding comments, generating weekly markdown status updates, or logging project decisions.
---

# OpenProject PM + Knowledge Skill

Purpose: use OpenProject as project source of truth and keep lightweight knowledge artifacts in markdown.

## Safety Rules

- Never execute arbitrary shell commands.
- Only use the local Python wrapper: `scripts/openproject_cli.py`.
- Never expose API tokens, credentials, or secret values.
- Do not perform delete operations in v1.
- Fail closed with clear errors when permissions, transitions, or endpoints are not available.
- Treat OpenProject wiki API operations as unsupported in this skill.

## Environment Variables

- `OPENPROJECT_BASE_URL` (required): OpenProject base URL, e.g. `https://openproject.example.org`.
- `OPENPROJECT_API_TOKEN` (required for default auth): API token used with username `apikey`.
- `OPENPROJECT_DEFAULT_PROJECT` (optional): default project id/identifier used when `--project` is omitted.
- `OPENPROJECT_DECISION_LOG_DIR` (optional): directory for decision markdown files. Default: `project-knowledge/decisions`.

## Supported Operations

Use `python scripts/openproject_cli.py <command> [args]`.

- `list-projects`
  - List visible projects with project ID, identifier, and name.
- `list-work-packages --project <id|identifier> [--status ...] [--assignee ...] [--limit N]`
  - List work packages with WP ID, subject, status, assignee, and updated date.
  - Applies filtering conservatively (API-side where practical, otherwise client-side).
- `create-work-package --project <id|identifier> --subject "..." [--type Task] [--description "..."]`
  - Create a work package and print created ID and subject.
  - Resolves type by name with helpful errors if type is unknown.
- `update-work-package-status --id <wp_id> --status "..."`
  - Resolve status by name (case-insensitive) and patch the work package.
  - Prints confirmation including WP ID and resulting status.
- `get-work-package --id <wp_id>`
  - Fetch full details for a single work package (status/type/priority/assignee/dates/description).
- `update-work-package --id <wp_id> [--subject ...] [--description ...] [--status ...] [--assignee ...] [--priority ...] [--type ...] [--start-date YYYY-MM-DD] [--due-date YYYY-MM-DD]`
  - Patch one or more mutable fields in a single call using transition-safe status resolution.
- `add-comment --id <wp_id> --comment "..."`
  - Best-effort comment creation using OpenProject API v3.
  - Returns a clear message when endpoint behavior differs by version/config.
- `list-statuses`
  - List available work package statuses.
- `list-types [--project <id|identifier>]`
  - List available work package types (project-scoped when provided).
- `list-priorities`
  - List available priority values.
- `list-users [--query ...] [--limit N]`
  - List visible users and optionally filter by name/login/id.
- `list-relations --id <wp_id> [--limit N]`
  - List relations for one work package.
- `create-relation --from-id <wp_id> --to-id <wp_id> --type <relation_type> [--description ...] [--lag N]`
  - Create a work package relation (non-destructive link operation).
- `weekly-summary --project <id|identifier> [--output path.md]`
  - Build compact markdown grouped by completion/in-progress/blockers/next focus.
  - Writes output to provided path or default `project-knowledge/status/YYYY-MM-DD-weekly-status.md`.
- `log-decision --project <id|identifier> --title "..." --decision "..." [--context ...] [--impact ...] [--followup ...]`
  - Create a decision markdown entry in `project-knowledge/decisions`.
- `log-spent-hours --project <id|identifier> --work-package "..." --hours <N> --activity "..." [--spent-on YYYY-MM-DD] [--comment "..."] [--type Task]`
  - Find a work package by exact subject in the project (or create it when absent) and log a time entry against it.
  - Use `--activity` to specify the time entry category (e.g. `"Internal discussion and meetings"`).
  - `--hours` accepts decimal values (e.g. `1`, `1.5`).
  - `--spent-on` defaults to today when omitted.

Wiki commands may exist in the CLI for legacy compatibility, but they are out of scope for this skill and should not be used in normal workflows.

## Agent Behavior

### Project status

- Prefer `list-work-packages` against the project in scope.
- Summarize by status buckets and include key WP IDs in outputs.
- Flag uncertainty explicitly when status labels are ambiguous.

### Creating tasks

- Use `create-work-package` with clear subject and optional description.
- Keep task type explicit when not defaulting to `Task`.
- Return created WP ID for traceability.

### Updating tasks

- Use `update-work-package` for multi-field updates and `update-work-package-status` for status-only changes.
- Resolve status by name and handle transition/workflow errors clearly.
- Validate date inputs as `YYYY-MM-DD` before sending updates.
- Confirm updates with WP ID and key resulting fields.

### Metadata and lookup

- Use `list-statuses`, `list-types`, `list-priorities`, and `list-users` before creating/updating when values are uncertain.
- Prefer explicit metadata lookups over guesswork when type/status/priority names vary between OpenProject instances.

### Relations

- Use `list-relations` to inspect dependencies and ordering constraints.
- Use `create-relation` for new links (`relates`, `blocks`, `follows`, etc.) and include `--lag` only when needed.

### Weekly status summary

- Use `weekly-summary`.
- Produce compact markdown with sections:
  - Wins / completed
  - In progress
  - Blockers / risks
  - Next focus
- Save summary in `project-knowledge/status/` with date-based filename unless output path is provided.

### Wiki requests

- Explain that wiki read/write is not supported by this skill due to inconsistent API behavior.
- When documentation updates are requested, create or update local markdown artifacts instead (for example in `project-knowledge/` or `templates/`) and note that wiki sync is manual.

### Logging spent hours

- Use `log-spent-hours` to record time against a work package.
- The command automatically finds an existing work package by exact subject match, or creates a new one when none is found.
- Activity names are validated via `/time_entries/activities`; if an activity is unknown, the error message will list the valid activity options for this OpenProject instance.
- Return the time entry ID and work package ID for traceability.
- Omit `--spent-on` to default to today's date.

### Decision logging

- Use `log-decision` for durable decisions.
- Capture context, decision, impact, and follow-up actions.
- Store files under `project-knowledge/decisions/` with date + slug naming.

## Output Style

- Keep outputs concise and structured.
- Always include work package IDs (e.g., `#123`) when referencing tasks.
- Prefer markdown lists and short sections over long prose.
