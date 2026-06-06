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
  password: "s3cr3t"
  project: "MyProject~project~1:admin:my_db"  # optional scope
  release: "R2.5"                              # optional scope

ewm:
  server: "https://ewm-host:9443/ccm"
  user: "ewm_admin"
  password: "s3cr3t"
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
    ├── migrate.py             # Migrator orchestrator + CLI entry point
    ├── synergy/
    │   ├── client.py          # CCMClient — ccm CLI subprocess wrapper
    │   ├── extractor.py       # SynergyExtractor — tasks, artifacts, baselines
    │   └── models.py          # SynergyTask / SynergyObject / SynergyBaseline
    ├── ewm/
    │   ├── client.py          # EWMClient — Jazz auth, OSLC CM, SCM REST
    │   ├── loader.py          # EWMLoader — work items, comments, attachments
    │   └── models.py          # EWMWorkItem / EWMArtifact / EWMBaseline
    └── transform/
        └── mapper.py          # TaskMapper / ArtifactMapper / BaselineMapper
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

**`CCMError: ccm command failed`**
The `ccm` executable is not on PATH or returned an error. Set `ccm_exe` to the full path and confirm you can run `ccm start` manually.

**`EWMAuthError: Authentication failed`**
Check your EWM `user`/`password`. If your server uses a non-standard context root (not `/ccm`), update the `server` URL. For self-signed TLS certificates, set `verify_ssl: false` or point `ca_bundle` at your CA certificate file.

**`EWMAPIError: Project area 'X' not found`**
The `project_area` value must match the EWM project area name exactly, including capitalisation.

**Work item type / state errors after creation**
The values in `type_map` and `status_map` must match the workflow state names configured in your specific EWM project area. Use a custom `mapping_file` to align them.

**Column delimiter collision**
The CCM query formatter uses `|||` as an internal column separator. If your Synergy data contains this string, change `_DELIM` in `synergy_to_ewm/synergy/client.py` to a different sentinel value.

---

## Limitations

- **Source control migration** uses EWM's SCM REST API. For very large repositories (tens of thousands of files), consider using IBM's official Jazz SCM command-line tools or a Git bridge instead, and use this module for work item migration only (`migrate_artifacts: false`, `migrate_baselines: false`).
- **History / blame** is not preserved — all files are checked in as a single commit by the migration user.
- **Links between work items** (parent/child, blocks/depends-on) are not migrated in the current version.
- Tested against Synergy 7.x and EWM 7.x. Older server versions may return slightly different CLI output or API responses.
