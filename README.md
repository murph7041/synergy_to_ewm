# synergy_to_ewm

A Python module for migrating data from **IBM Rational Synergy** (CM Synergy / Telelogic Synergy) to **IBM Engineering Workflow Management** (EWM, formerly Rational Team Concert / RTC).

## What it migrates

| Synergy | EWM |
|---|---|
| Tasks & defects | Work items (Defect, Task, Change Request, etc.) |
| Task notes | Work item comments |
| Task attachments | Work item attachments |
| Custom attributes | Configurable custom field mapping |
| Versioned source objects | Files checked in to an SCM component |
| Baselines & releases | SCM baselines / snapshots |

## Requirements

- Python 3.9+
- The `ccm` executable on your PATH (or provide the full path in config)
- Network access to the EWM server
- An EWM user account with permission to create work items and SCM content

> **Synergy password:** Run `ccm set_password` before migrating to store your Synergy credentials in CCM's own credential store. When a password is stored this way you can omit the `password` field from the `synergy` config section entirely.
>
> **EWM password:** The `password` field in the `ewm` config section is optional. If omitted, you will be prompted on the first run and given the option to save the credential to the system keyring (Windows Credential Manager on Windows; GNOME Keyring or KWallet on Linux) — subsequent runs will use the stored credential without prompting. On headless Linux servers without a keyring daemon the save option is skipped and the password is used only for that run. You can also pre-store it manually: `python -c "import keyring; keyring.set_password('synergy_to_ewm:ewm', '<user>', '<password>')"`. To clear a stored credential: `python -c "import keyring; keyring.delete_password('synergy_to_ewm:ewm', '<user>')"`.

Dependencies: `requests`, `urllib3`, `PyYAML`, `keyring`

```
pip install -r requirements.txt
```

## Quick start

### 1. Install

```bash
pip install -r requirements.txt
# or install as a package
pip install .
```

### 2. Create a config file

Copy `config_example.yaml` and fill in your connection details:

```yaml
synergy:
  server: "http://synergy-host:8400"
  database: "/data/synergy/my_db"
  user: "synergy_admin"
  # password omitted — uses ccm set_password credential store
  project: "MyProject~project~1:admin:my_db"  # optional scope
  release: "R2.5"                              # optional scope

ewm:
  server: "https://ewm-host:9443/ccm"
  user: "ewm_admin"
  # password omitted — prompted on first run with option to save to system keyring
  project_area: "My EWM Project"

migration:
  dry_run: false
```

### 3. Dry run first

```bash
python -m synergy_to_ewm.migrate my_config.yaml --dry-run
```

All operations are logged but nothing is written to EWM.

### 4. Run the migration

```bash
python -m synergy_to_ewm.migrate my_config.yaml
```

Progress is saved to `migration_state.json` after each item. If the run is interrupted, re-running the same command skips already-migrated items.

---

## Extracting Synergy data without migrating

Use the extraction CLI to pull all Synergy data into a self-contained JSON file without touching EWM. This is useful for inspecting data before a migration, archiving, or feeding a custom import pipeline.

Only the `synergy` section of the config is needed — the `ewm` section can be omitted entirely.

```bash
# Default output file: synergy_extract.json
python -m synergy_to_ewm.extract my_config.yaml

# Specify output path
python -m synergy_to_ewm.extract my_config.yaml output/my_extract.json

# After pip install .
synergy-extract my_config.yaml
```

### Output format

The JSON file has four top-level arrays:

| Key | Contents |
|---|---|
| `tasks` | All extracted `SynergyTask` objects (one per CCM task) |
| `defects` | All extracted `SynergyTask` objects with `task_type = "defect"` |
| `artifacts` | All versioned `SynergyObject` file records, oldest-first per file |
| `baselines` | All `SynergyBaseline` snapshots with their member object lists |

Each task includes its comments, attachments, and change history. File and attachment content is stored as **base64-encoded strings**.

Source file content is in `artifacts[*].content`. Baseline membership (which file versions belong to which snapshot) is in `baselines[*].objects`, but those entries do not carry file content — cross-reference with `artifacts` by `spec` if the bytes are needed.

---

## Loading a Synergy extract into EWM

Use the load CLI to push a previously-extracted JSON file into EWM without connecting to Synergy. This is the second half of the two-phase workflow.

Only the `ewm` and `migration` sections of the config are required — the `synergy` section is ignored if present.

```bash
python -m synergy_to_ewm.load my_config.yaml synergy_extract.json

# dry run — connects to EWM but writes nothing
python -m synergy_to_ewm.load my_config.yaml synergy_extract.json --dry-run

# after pip install .
synergy-load my_config.yaml synergy_extract.json
```

The same `migration_state.json` resumability applies: items already marked done are skipped, so a failed run can be re-run safely.

### Two-phase workflow

Run extraction and loading as separate steps — useful when Synergy and EWM are on different networks, or when you want to inspect the data before committing it to EWM:

```bash
# Step 1 — on the Synergy network
synergy-extract synergy_config.yaml synergy_extract.json

# Step 2 — on the EWM network (synergy section not needed)
synergy-load ewm_config.yaml synergy_extract.json
```

---

## Configuration reference

### `synergy` section

| Field | Required | Description |
|---|---|---|
| `server` | Yes | Synergy server URL, e.g. `http://host:8400` |
| `database` | Yes | Database path or name |
| `user` | Yes | Synergy username |
| `password` | No | Synergy password. Omit if credentials are stored via `ccm set_password`. |
| `ccm_exe` | No | Path to `ccm` executable (default: `ccm`) |
| `project` | No | Project spec to scope artifact/baseline extraction |
| `release` | No | Release name to scope task/defect queries |
| `query_extra` | No | Extra CCM query fragment ANDed into all task queries |

### `ewm` section

| Field | Required | Description |
|---|---|---|
| `server` | Yes | EWM server base URL, e.g. `https://host:9443/ccm` |
| `user` | Yes | EWM username |
| `password` | No | EWM password. Omit to be prompted on first run with an option to save to the system keyring. |
| `project_area` | Yes | Exact name of the EWM Project Area |
| `component_name` | No | SCM component name for source artifacts (created if absent) |
| `stream_name` | No | SCM stream name (created if absent) |
| `verify_ssl` | No | Verify TLS certificates (default: `true`) |
| `ca_bundle` | No | Path to a CA bundle for self-signed certificates |

### `migration` section

| Field | Default | Description |
|---|---|---|
| `mapping_file` | `null` | Path to a custom field-mapping YAML (merged on top of built-in defaults) |
| `state_file` | `migration_state.json` | Resumable state tracking file |
| `log_file` | `migration.log` | Log output file (`null` to disable) |
| `batch_size` | `50` | Work items per API request batch |
| `migrate_tasks` | `true` | Migrate tasks and defects |
| `migrate_artifacts` | `true` | Migrate versioned source files |
| `migrate_baselines` | `true` | Migrate baselines (includes their artifacts) |
| `migrate_attachments` | `true` | Migrate task attachments |
| `migrate_comments` | `true` | Migrate task notes as work item comments |
| `dry_run` | `false` | Log operations without writing to EWM |
| `field_overrides` | `{}` | Inline mapping overrides (see Field mapping) |

---

## Field mapping

The built-in mapping (`synergy_to_ewm/mapping_default.yaml`) translates Synergy statuses, priorities, severities, and work item types to their EWM equivalents. Override any part of it without touching the source code.

### Option A — custom YAML file

```yaml
# my_mapping.yaml
work_item:
  type_map:
    task: "Story"          # override: map Synergy tasks to EWM Stories
    defect: "Defect"
  status_map:
    assigned: "Open"
    completed: "Done"
  custom_field_map:
    rtc_ext:customer_name: customer  # EWM attribute: Synergy attribute
    rtc_ext:found_in: found_in_release
```

Reference it from your config:

```yaml
migration:
  mapping_file: "my_mapping.yaml"
```

### Option B — inline overrides in config

```yaml
migration:
  field_overrides:
    work_item:
      custom_field_map:
        rtc_ext:customer: customer_name
```

### Option C — programmatic override

```python
cfg = MigrationConfig(
    ...,
    field_overrides={"work_item": {"type_map": {"task": "Story"}}}
)
```

Merge order (highest priority wins): `field_overrides` > `mapping_file` > built-in defaults.

---

## Programmatic API

The module can be used directly in Python without the CLI.

```python
from synergy_to_ewm import Migrator, MigrationConfig, SynergyConfig, EWMConfig

cfg = MigrationConfig(
    synergy=SynergyConfig(
        server="http://synergy-host:8400",
        database="/data/synergy/my_db",
        user="admin",
        password="s3cr3t",
        release="R2.5",
    ),
    ewm=EWMConfig(
        server="https://ewm-host:9443/ccm",
        user="ewm_admin",
        password="s3cr3t",
        project_area="My EWM Project",
    ),
    migrate_tasks=True,
    migrate_artifacts=True,
    migrate_baselines=True,
    dry_run=False,
)

stats = Migrator(cfg).run()
print(stats)
# {'tasks_ok': 412, 'tasks_skipped': 0, 'tasks_failed': 2,
#  'baselines_ok': 8, 'artifacts_ok': 1503, 'errors': [...]}
```

### Using individual components

Extract only, without loading:

```python
from synergy_to_ewm.synergy import CCMClient, SynergyExtractor

with CCMClient("http://host:8400", "/data/db", "user", "pass") as ccm:
    extractor = SynergyExtractor(ccm, release="R2.5")
    tasks = extractor.extract_tasks()
    baselines = extractor.extract_baselines(project_spec="MyProject~project~1:admin:db")
```

Load only, from your own data:

```python
from synergy_to_ewm.ewm import EWMClient, EWMLoader
from synergy_to_ewm.ewm.models import EWMWorkItem

with EWMClient("https://ewm:9443/ccm", "user", "pass") as ewm:
    project_id = ewm.find_project_area("My EWM Project")
    loader = EWMLoader(ewm, project_id)
    wi = EWMWorkItem(title="Bug #123", description="...", work_item_type="Defect",
                     status="New", priority="High", severity="Critical",
                     filed_against="Component A", owned_by="dev1",
                     submitted_by="tester1", created="", modified="")
    url = loader.load_work_item(wi)
```

---

## Package structure

```
synergy_to_ewm/
├── setup.py
├── requirements.txt
├── config_example.yaml
└── synergy_to_ewm/
    ├── config.py              # MigrationConfig / SynergyConfig / EWMConfig
    ├── mapping_default.yaml   # Built-in field/status/priority/type maps
    ├── migrate.py             # Migrator orchestrator + CLI entry point (synergy-to-ewm)
    ├── extract.py             # Synergy-only extraction CLI (synergy-extract)
    ├── load.py                # EWM load-from-file CLI (synergy-load)
    ├── synergy/
    │   ├── client.py          # CCMClient — ccm CLI subprocess wrapper
    │   ├── extractor.py       # SynergyExtractor — tasks, artifacts, baselines
    │   └── models.py          # SynergyTask / SynergyObject / SynergyBaseline
    ├── ewm/
    │   ├── client.py          # EWMClient — Jazz auth, OSLC CM, SCM REST
    │   ├── loader.py          # EWMLoader — work items, comments, attachments
    │   ├── scm_cli.py         # JazzSCMClient — Jazz SCM CLI backend
    │   └── models.py          # EWMWorkItem / EWMArtifact / EWMBaseline
    ├── gitlab/
    │   ├── client.py          # GitLabClient — REST API (issues, milestones, uploads)
    │   ├── git_client.py      # GitClient — git CLI wrapper for pushing source code
    │   ├── loader.py          # GitLabLoader — orchestrates GitLab migration
    │   └── models.py          # GitLabIssue / GitLabMilestone / GitLabNote
    └── transform/
        ├── mapper.py          # TaskMapper / ArtifactMapper / BaselineMapper (→ EWM)
        └── gitlab_mapper.py   # GitLabMapper (→ GitLab)
```

---

## Resuming an interrupted migration

Every successfully migrated item is recorded in `migration_state.json`. Simply re-run the same command — items already in the state file are skipped:

```
[INFO] Skipping task 1042 (already migrated)
[INFO] Skipping task 1043 (already migrated)
[INFO] Creating work item: [1044] Login page crash...
```

To force a full re-migration, delete `migration_state.json` before running.

---

## Troubleshooting

**`CCMError: ccm executable not found`**
The Synergy client is not installed or `ccm` is not on your PATH. Install the Synergy client and ensure `ccm` is accessible, or set `ccm_exe` in the `synergy` config section to the full path of the executable.

**`CCMError: ccm command failed`**
The `ccm` executable ran but returned an error. Confirm you can run `ccm start` manually and check the logged stderr output for details.

**`EWMAuthError: Authentication failed`**
Check your EWM `user`/`password`. If your server uses a non-standard context root (not `/ccm`), update the `server` URL. For self-signed TLS certificates, set `verify_ssl: false` or point `ca_bundle` at your CA certificate file.

**`EWMAPIError: Project area 'X' not found`**
The `project_area` value must match the EWM project area name exactly, including capitalisation.

**Work item type / state errors after creation**
The values in `type_map` and `status_map` must match the workflow state names configured in your specific EWM project area. Use a custom `mapping_file` to align them.

**Column delimiter collision**
The CCM query formatter uses `|||` as an internal column separator. If your Synergy data contains this string, change `_DELIM` in `synergy_to_ewm/synergy/client.py` to a different sentinel value.

---

## Loading into GitLab

Pass `--target gitlab` to `synergy-load` to push data into a GitLab project instead of EWM. The `ewm` config section is ignored; add a `gitlab` section instead.

### What is migrated

| Synergy | GitLab |
|---|---|
| Tasks and defects | Issues (with labels, milestone link) |
| Task notes | Issue notes/comments |
| Task attachments | Uploaded to the project and linked in the issue description |
| Change history | Issue notes (prepended, oldest-first) |
| Baselines / releases | Milestones + git tags |
| Versioned source files | Git commits (one per baseline, oldest-first) |

### Configuration

Add a `gitlab` section to your config file:

```yaml
gitlab:
  server: "https://gitlab.com"           # or your self-hosted URL
  project: "mygroup/myproject"           # namespace/project-name or numeric ID
  token: "glpat-xxxxxxxxxxxxxxxxxxxx"    # PAT with api + write_repository scopes
                                         # or omit and export GITLAB_TOKEN=...
  default_branch: "main"
  git_workdir: "gitlab_migration_repo"   # persistent local clone (created if absent)
  verify_ssl: true
```

### Running

```bash
# Dry run — connects to GitLab but creates nothing
python -m synergy_to_ewm.load config.yaml synergy_extract.json --target gitlab --dry-run

# Full run
python -m synergy_to_ewm.load config.yaml synergy_extract.json --target gitlab

# After pip install .
synergy-load config.yaml synergy_extract.json --target gitlab
```

### Issue labels

Every migrated issue is tagged `migrated-from-synergy` plus scoped labels derived from the mapping:

- `type::task`, `type::defect`, etc. (from `type_map`)
- `status::new`, `status::resolved`, etc. (from `status_map`)
- `priority::high`, `priority::medium`, etc. (from `priority_map`)

### Source code workflow

For each baseline the loader writes all its file versions to a persistent local clone, commits, creates a git tag matching the baseline name, then pushes. Loose artifacts (when migrating without baselines) are batched into a single commit. The local clone in `git_workdir` persists between runs so an interrupted migration can resume from the last committed baseline.

---

## Jazz SCM CLI backend

By default the tool checks files into EWM using the REST API (one HTTP call per file). For large repositories this can be slow. Switch to IBM's official `scm` command-line tool, which batches files per baseline delivery and uses the same binary protocol as the Eclipse client.

### Requirements

- The EWM client (`scm` executable) installed and on your PATH, or set `scm_exe` to its full path.
- A pre-existing login session **or** supply the password — the `scm login` command stores credentials in the user profile, similar to `ccm set_password`.

### Configuration

Add two fields to the `ewm` section of your config:

```yaml
ewm:
  server: "https://ewm-host:9443/ccm"
  user: "ewm_admin"
  project_area: "My EWM Project"
  scm_backend: cli          # switch from 'rest' (default) to 'cli'
  scm_exe: scm              # full path if not on PATH; e.g. /opt/jazz/scm
```

### How it works

For each baseline the CLI backend:
1. Creates a temporary workspace targeted at the stream
2. Writes all baseline files to a local sandbox directory
3. Runs `scm add .` → `scm checkin` → `scm deliver` in one batch
4. Creates the baseline snapshot with `scm create baseline`
5. Deletes the temporary workspace

Loose artifacts (when migrating without baselines) are batched into a single delivery.

Work item migration always uses the REST API regardless of `scm_backend`.

---

## Limitations

- **Source control migration** supports two backends — see [Jazz SCM CLI backend](#jazz-scm-cli-backend) below. The default REST backend is convenient but slow for large repositories. Switch to `scm_backend: cli` for tens of thousands of files.
- **History / blame** is not preserved — all files are checked in as a single commit by the migration user.
- **Links between work items** (parent/child, blocks/depends-on) are not migrated in the current version.
- Tested against Synergy 7.x and EWM 7.x. Older server versions may return slightly different CLI output or API responses.
