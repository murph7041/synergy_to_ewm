# synergy_to_ewm: A Migration Framework for IBM Rational Synergy

## Abstract

`synergy_to_ewm` is a Python library and command-line tool that automates the migration of project data from IBM Rational Synergy (CM Synergy) to either IBM Engineering Workflow Management (EWM, formerly Rational Team Concert) or GitLab. It covers the full scope of a Synergy project: work items (tasks and defects), change history, comments, file attachments, versioned source artifacts, and release baselines. The tool is designed for large, production migrations: it is resumable, supports dry-run validation, and emits structured logs throughout.

---

## 1. Background and Problem Statement

IBM Rational Synergy is a change and configuration management (CCM) platform widely used in aerospace, defense, and automotive industries. Organizations that have outgrown Synergy, or that are moving to cloud-based development workflows, face a non-trivial migration challenge: Synergy stores not just work items but also versioned source code, baseline snapshots, task notes, file attachments, and a complete field-change audit trail — all of which must be preserved in the target system to maintain regulatory traceability.

Manual migration of this data is labor-intensive and error-prone. `synergy_to_ewm` automates the extraction, transformation, and loading of this data, and provides the operational controls (resumability, dry-run mode, configurable field mapping) required to run such a migration safely in a production environment.

---

## 2. Target Platforms

The tool supports two target systems:

| Target | Protocol | Work items | Source code |
|--------|----------|------------|-------------|
| IBM EWM (RTC) | OSLC CM 2.0 REST + Jazz SCM | Work items via OSLC CM JSON | Checkins via REST API or Jazz SCM CLI |
| GitLab | GitLab API v4 + git | Issues and milestones | Commits and tags via git |

The extraction side is identical for both targets: the tool always reads from Synergy via the `ccm` command-line interface.

---

## 3. Architecture: Extract-Transform-Load

The module follows a classic ETL (Extract-Transform-Load) pipeline, with each phase implemented as a discrete layer.

```
┌─────────────────────────────────────────────────────────────────────┐
│ Source                                                              │
│   IBM Rational Synergy  (ccm CLI)                                   │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  CCMClient / SynergyExtractor
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Extract layer                                                       │
│   Tasks · Defects · Comments · Attachments · History               │
│   Versioned artifacts (predecessor chain) · Baselines              │
│   → Typed Python model objects (SynergyTask, SynergyObject, …)     │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  Optional: serialise to JSON
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Transform layer                                                     │
│   TaskMapper · ArtifactMapper · BaselineMapper · GitLabMapper      │
│   Configurable YAML field mapping (type, status, priority, …)      │
│   → EWMWorkItem / EWMArtifact / EWMBaseline                        │
│     GitLabIssue / GitLabMilestone                                  │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                    ┌──────────┴──────────┐
                    ▼                     ▼
        ┌───────────────────┐  ┌──────────────────────┐
        │ EWM target        │  │ GitLab target         │
        │ EWMClient (OSLC)  │  │ GitLabClient (API v4) │
        │ JazzSCMClient     │  │ GitClient (git CLI)   │
        │ EWMLoader         │  │ GitLabLoader          │
        └───────────────────┘  └──────────────────────┘
```

### 3.1 Extract Layer

**`CCMClient`** (`synergy/client.py`) is a stateful wrapper around the `ccm` command-line executable. It opens a named Synergy session (`ccm start`), injects `CCM_HOME` into every subsequent subprocess call so that parallel sessions do not conflict, and closes the session cleanly on exit — even when an exception occurs. All `ccm` invocations go through a single `_run_raw()` helper that handles missing executables, non-zero exit codes, and stderr logging.

**`SynergyExtractor`** (`synergy/extractor.py`) uses `CCMClient` to query the Synergy database and build typed model objects. It extracts:

- **Tasks and defects** — via `ccm query`, including all standard attributes plus any custom attributes, which are preserved in a `custom_attrs` dict.
- **Comments (task notes)** — via `ccm task -show_notes`, parsed from Synergy's structured block format.
- **Change history** — via `ccm history`, recording every field mutation with author and timestamp.
- **File attachments** — binary content fetched via `ccm cat -out`.
- **Versioned source artifacts** — the extractor walks the predecessor chain of every file (`get_object_versions`), building a complete version history oldest-first so that check-ins into the target system reproduce the correct chronological order.
- **Baselines** — listed via `ccm baseline -list` and expanded with their member objects.

All extraction operations are individually guarded: a failure to fetch notes, history, or a single attachment is logged and skipped rather than aborting the entire task.

### 3.2 Transform Layer

**`TaskMapper`** (`transform/mapper.py`) converts a `SynergyTask` into an `EWMWorkItem`. The mapping is data-driven: a YAML file (`mapping_default.yaml`) defines how Synergy values translate to EWM values for type, status, priority, and severity. Users can supply an override file and/or a `field_overrides` dict in the config to customise the mapping without touching the defaults. Custom attributes are forwarded via a `custom_field_map` section.

The generated EWM work item title embeds the original Synergy task number for traceability. The description body preserves the original Synergy metadata (type, status, release, submitter, resolver, custom attributes) as a formatted appendix. Change history entries are prepended as comments so the audit trail is visible in EWM.

**`ArtifactMapper`** maps `SynergyObject` instances to `EWMArtifact` records, skipping directories (which EWM creates implicitly) and objects with no binary content.

**`BaselineMapper`** maps a `SynergyBaseline` to an `EWMBaseline` by running each member object through `ArtifactMapper` and assembling the result.

**`GitLabMapper`** (`transform/gitlab_mapper.py`) performs the equivalent mapping for the GitLab target, converting tasks to issues (with labels derived from status and type) and baselines to milestones.

### 3.3 Load Layer — EWM

**`EWMClient`** (`ewm/client.py`) authenticates via Jazz form-based authentication (the `j_security_check` redirect dance required by all Jazz servers) and exposes the OSLC CM 2.0 endpoints needed to create and update work items, upload attachments, and manage SCM components, streams, changesets, and baselines. It uses a `requests.Session` with a retry adapter (5 retries, exponential backoff, retry on 429/5xx) and injects a configurable timeout on every call.

**`JazzSCMClient`** (`ewm/scm_cli.py`) provides an alternative SCM backend that shells out to the IBM Jazz `scm` CLI instead of the REST API. This is useful when the target EWM server's SCM REST service is unavailable or has not been fully configured. It supports workspace setup, artifact delivery, baseline snapshot creation, login, and logout.

**`EWMLoader`** (`ewm/loader.py`) ties the client to the migration pipeline. It creates a work item, then iterates over its comments and attachments to add them individually. For SCM content it checks files into a pre-created stream/component via the configured backend (REST or CLI).

### 3.4 Load Layer — GitLab

**`GitLabClient`** (`gitlab/client.py`) wraps the GitLab API v4, resolving the project ID on first use and exposing methods to create milestones, issues, notes, and file uploads. Token authentication uses a Personal Access Token or Project Access Token read from the config or the `GITLAB_TOKEN` environment variable.

**`GitClient`** (`gitlab/git_client.py`) manages a persistent local clone of the target GitLab repository. For baseline migration it writes artifact files into the working tree, commits with the baseline name as the message, and creates a git tag — reproducing the release history as a tagged commit graph. Loose artifacts are delivered as a single commit.

**`GitLabLoader`** (`gitlab/loader.py`) orchestrates the full GitLab load: milestones first (so issues can reference them), then issues, then source code. Attachment content is uploaded to GitLab's file storage and embedded as markdown links in the issue description.

---

## 4. Execution Modes

### 4.1 Integrated (live migration)

```
synergy_to_ewm.migrate  config.yaml [--dry-run]
```

Opens a Synergy session and an EWM session simultaneously, extracts and loads in a single pass. Suitable when both systems are reachable from the migration host and the data volume is manageable in one run.

### 4.2 Decoupled (extract then load)

```
synergy-extract  config.yaml  extract.json
synergy-load     config.yaml  extract.json  [--target ewm|gitlab]
```

Phase 1 reads from Synergy and writes a self-contained JSON snapshot. Binary content (file attachments, artifact bytes) is base64-encoded so the file is portable and human-inspectable. Phase 2 reads the JSON and loads to the target with no Synergy connection required. This is the recommended approach when the Synergy and target systems are on different network segments, or when the extract is slow and must be run on a schedule.

The `--target` flag selects between EWM and GitLab loaders at load time, so a single extract can be used to populate both systems during evaluation.

---

## 5. Operational Features

### 5.1 Resumable Migration

Every successfully loaded item is recorded in a JSON state file (`migration_state.json` by default). On a subsequent run, items already present in the state file are skipped. Items that failed are recorded separately and are automatically retried on the next run without any operator intervention. The state file is written atomically (write to `.tmp`, rename over the real file) to prevent corruption if the process is killed mid-save.

### 5.2 Dry-run Mode

Pass `--dry-run` on the command line (or set `dry_run: true` in the config). All read operations execute normally, but no write calls are made to the target system. The log output shows exactly what would be created, making dry-run a reliable pre-flight check before committing a large migration.

### 5.3 Configurable Field Mapping

The bundled `mapping_default.yaml` provides sensible defaults for type, status, priority, and severity translation. A per-project override file can be supplied to remap values without touching the defaults. Overrides are deep-merged, so a user can change a single status value while inheriting the rest. In-line overrides can also be supplied as a `field_overrides` dict in the config, which is useful for scripted or programmatic invocations.

### 5.4 Partial Scope Selection

Each migration phase can be individually enabled or disabled via `migrate_tasks`, `migrate_artifacts`, and `migrate_baselines` flags. A `since` date filter limits artifact extraction to objects modified on or after a given date, which is critical for incremental top-up runs after an initial migration. Multiple releases can be specified in `releases`, and the tool iterates over them automatically.

### 5.5 Credential Management

Passwords may be supplied in the config file, passed interactively via `getpass`, or stored in the OS credential store (Windows Credential Manager or the system keyring via the `keyring` library). EWM passwords are offered for secure storage after first interactive entry.

### 5.6 Logging

All operations are logged at INFO level to stdout and optionally to a file. Each session startup, query, work item creation, file checkin, and error produces a timestamped log line. Fatal errors at the session level (authentication failure, network unreachable, SSL certificate error) produce specific, actionable messages rather than raw tracebacks.

---

## 6. Data Coverage

| Synergy entity | EWM target | GitLab target |
|----------------|------------|---------------|
| Task | Work item (type mapped) | Issue (labels from status/type) |
| Defect | Work item (type mapped) | Issue |
| Task notes | Work item comments | Issue notes |
| Field change history | Work item comments (prepended) | Issue notes |
| File attachments | Work item attachments | Uploaded to GitLab, linked in description |
| Custom attributes | Custom work item fields | Appended to issue description |
| Versioned source files | SCM checkins (one per version, chronological) | Git commits |
| Baselines | EWM SCM baseline snapshots | Git tags on tagged commits |

---

## 7. Configuration Reference

Configuration is supplied as a YAML file. The relevant sections are:

```yaml
synergy:
  server: "http://synergy-host:8400"
  database: "/path/to/synergy/db"
  user: "jsmith"
  password: "secret"         # optional; omit to prompt or use keyring
  ccm_exe: "ccm"             # full path if not on PATH
  project: "MyProject~1:admin:db"
  releases: ["R1.0", "R2.0"] # or single: release: "R1.0"
  since: "2023-01-01"        # skip artifacts unmodified before this date

ewm:
  server: "https://ewm-host:9443/ccm"
  user: "jsmith"
  project_area: "My EWM Project"
  verify_ssl: true
  ca_bundle: "/path/to/ca.crt"  # for self-signed certificates
  scm_backend: "rest"            # or "cli"

gitlab:
  server: "https://gitlab.example.com"
  project: "group/project-name"
  token: "glpat-xxxx"
  default_branch: "main"

migration:
  state_file: "migration_state.json"
  log_file:   "migration.log"
  migrate_tasks:     true
  migrate_artifacts: true
  migrate_baselines: true
  dry_run: false
  mapping_file: "my_overrides.yaml"  # optional
```

---

## 8. Limitations and Considerations

- **CCM CLI dependency.** The Synergy client (`ccm`) must be installed and accessible on the migration host. The tool will not connect to Synergy over a REST API; it exclusively uses the `ccm` subprocess interface.

- **EWM project area must exist.** The tool creates work items and SCM components within an existing EWM project area. It does not create the project area itself.

- **Synergy task numbering is preserved in titles.** Work item titles are prefixed with the original task number (e.g., `[1234] Fix memory leak`) to maintain traceability, but the original Synergy task number is not stored as a native EWM attribute unless a custom field mapping is configured.

- **Binary artifact size.** Fetching file content for large projects can be slow and memory-intensive. The `since` filter and the decoupled extract/load mode are the primary mitigations.

- **GitLab issue creation timestamps.** GitLab only accepts `created_at` overrides from users with admin permissions. On non-admin accounts, issues will be created with the current timestamp regardless of the original Synergy creation date.

---

## 9. Module Structure

```
synergy_to_ewm/
├── config.py              Configuration dataclasses
├── migrate.py             Integrated migration orchestrator (Migrator)
├── extract.py             Standalone Synergy extraction CLI
├── load.py                Standalone EWM/GitLab load CLI (FileLoader)
├── mapping_default.yaml   Bundled default field mapping
├── synergy/
│   ├── client.py          CCMClient — ccm CLI wrapper
│   ├── extractor.py       SynergyExtractor — typed extraction logic
│   └── models.py          SynergyTask, SynergyObject, SynergyBaseline, …
├── ewm/
│   ├── client.py          EWMClient — OSLC CM + Jazz REST
│   ├── loader.py          EWMLoader — work item and SCM loading
│   ├── scm_cli.py         JazzSCMClient — Jazz scm CLI wrapper
│   └── models.py          EWMWorkItem, EWMArtifact, EWMBaseline, …
├── gitlab/
│   ├── client.py          GitLabClient — GitLab API v4
│   ├── git_client.py      GitClient — git CLI wrapper
│   ├── loader.py          GitLabLoader — issue and source loading
│   └── models.py          GitLabIssue, GitLabMilestone, …
└── transform/
    ├── mapper.py           TaskMapper, ArtifactMapper, BaselineMapper
    └── gitlab_mapper.py    GitLabMapper
```
