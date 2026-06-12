# How Streams Are Used When Importing Into EWM

## EWM SCM Concepts Involved

Before explaining the tool's behaviour, it helps to name the relevant EWM SCM entities:

| Entity | What it is |
|--------|-----------|
| **Component** | A named container for a set of source files within a project area. Analogous to a repository root. |
| **Stream** | A shared, server-side baseline that acts as the integration target — the "trunk" or "branch" that all team members deliver to. |
| **Workspace** | A personal, client-side working copy that tracks one or more streams. Used only during active development or, in this tool, during a delivery batch. |
| **Changeset** | A bundle of file changes that is committed atomically. Must be explicitly completed/delivered before its content becomes visible in the stream. |
| **Baseline** | A named, immutable snapshot of the stream at a specific point in time. The EWM equivalent of a Synergy baseline. |

---

## Step 1 — One-Time Setup (`_ensure_scm_targets`)

At the start of any migration run that includes source artifacts or baselines, both `Migrator.run()` and `FileLoader.run()` call `_ensure_scm_targets()`. This creates exactly **one component** and **one stream** for the entire migration:

```python
component_id = ewm_client.create_component(component_name, project_area_id)
stream_id    = ewm_client.create_stream(stream_name, project_area_id, component_id)
```

The names come from the config fields `ewm.component_name` and `ewm.stream_name`, falling back to the mapping file's `scm.default_component` / `scm.default_stream`, and ultimately to `"Migrated from Synergy"` / `"Migrated from Synergy Stream"` if nothing is set.

For the **CLI backend**, `ensure_component` and `ensure_stream` are idempotent — they catch the "already exists" error and carry on, so re-running after a partial migration does not duplicate the stream.

For the **REST backend**, the `create_component` / `create_stream` calls are not idempotent. If the migration is interrupted and re-started, the same component and stream names will be used but the calls will be issued again. In practice this is benign because the state file gates which content is actually loaded, but operators should be aware that the REST API may create duplicate SCM objects if a run is restarted from scratch without first cleaning up.

---

## Step 2a — Artifact Migration via the REST Backend

Each versioned file is loaded by `EWMLoader.load_artifact()`, which delegates to `EWMClient.checkin_file()`. The sequence for a single file version is:

```
create_changeset(stream_id, component_id, comment, author)
   → POST /workspaces/{stream_id}/components/{component_id}/changesets
   → returns changeset_id

POST file content to the changeset
   → POST /workspaces/{stream_id}/components/{component_id}/changesets/{id}/content

complete_changeset(stream_id, component_id, changeset_id)
   → POST /workspaces/{stream_id}/components/{component_id}/changesets/{id}/complete
```

After `complete_changeset`, the file content becomes visible in the stream's history. **Each file version produces its own changeset.** Because the extractor walks the predecessor chain oldest-first (`get_object_versions` reverses the chain), the changesets land in the stream in chronological order, faithfully reproducing the Synergy version history.

---

## Step 2b — Artifact Migration via the CLI Backend

The `JazzSCMClient.deliver_artifacts()` method batches an entire set of artifacts (all files belonging to one baseline, or all loose artifacts) into a single delivery:

1. A **temporary workspace** is created with a UUID-based name, linked to the target stream:
   ```
   scm create workspace syn-migration-<uuid12> -s <stream_name>
   ```
2. The workspace is **loaded** into a temporary local directory (the sandbox):
   ```
   scm load <workspace_name> --dir <sandbox>
   ```
3. All artifact files are **written** into the sandbox at their logical paths.
4. New/modified files are **staged, checked in, and delivered** in one shot:
   ```
   scm add .          (run in sandbox)
   scm checkin . -c "<comment>"
   scm deliver
   ```
5. The temporary workspace is **deleted** unconditionally (in a `finally` block):
   ```
   scm delete workspace <workspace_name>
   ```

The temporary workspace is never persisted on the server after the delivery completes. This approach is significantly more efficient than the REST backend for large batches because many files are delivered in a single network round-trip rather than one per file per version.

---

## Step 3 — Baseline Snapshots

After all artifacts for a Synergy baseline have been delivered to the stream, a named EWM baseline is created that captures the current state of the stream at that moment.

**REST backend:**
```python
ewm_client.create_baseline(stream_id, component_id, baseline.name, description)
# → POST /workspaces/{stream_id}/components/{component_id}/baselines
```

**CLI backend:**
```
scm create baseline <name> -s <stream_name> -c <component_name> -d "<description>"
```

Because baselines are processed in the order Synergy returns them (which is generally chronological), the sequence of EWM baselines on the stream mirrors the original Synergy release history.

---

## Summary of the Stream's Role

The stream serves as the single integration target for all migrated source content. It accumulates changesets — one per file version (REST) or one per delivery batch (CLI) — in chronological order, and receives a named baseline snapshot after each Synergy baseline's content has been delivered. The result is an EWM stream whose history directly reflects the Synergy project's version and release history.

```
EWM Stream: "Migrated from Synergy Stream"
│
├─ Changeset: file_a.c v1  (oldest Synergy version)
├─ Changeset: file_b.h v1
├─ Changeset: file_a.c v2
├─ Baseline:  "Release-1.0"   ← snapshot after R1.0 content delivered
├─ Changeset: file_a.c v3
├─ Changeset: file_c.c v1
└─ Baseline:  "Release-2.0"   ← snapshot after R2.0 content delivered
```
