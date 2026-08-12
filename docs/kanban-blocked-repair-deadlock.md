# Kanban Blocked Repair Deadlock — Full Debugging Walkthrough

## The Problem

When auditor detects P0/P1 and blocks itself, it creates a "修复方案" task for architect. But the new task has the blocked auditor as its parent dependency. Since `blocked ≠ done`, the repair plan task can never be promoted — the kanban dispatcher rejects it with `claim_rejected (reason: parents_not_done)`.

## Reproduction

```
auditor: block + create("修复方案", assignee=architect)
         ↓
         new task status=todo, parents=[blocked_auditor]
         ↓
watchdog: promote --force
         ↓
gateway:  claim_rejected (parents_not_done)
         ↓
         task back to todo, permanently stuck
```

## Known Root Causes

### RC1: `get_task_deps` Parses Wrong Format

`kanban show` output format uses `parents:` not `Depends on:`:

```
parents:   t_1ef23160    ← NOT "Depends on:"
```

But watchdog's `get_task_deps()` used to match only:

```python
if "Depends on:" in line or "depends on:" in line:
```

**Consequence**: returns `[]` always → watchdog skips unlink → only does `promote --force` → gateway rejects → task stuck.

**Fix** (v1.4.0+): Match all three formats:

```python
if (line_stripped.startswith("parents:") or
    line_stripped.startswith("Depends on:") or
    line_stripped.startswith("depends on:")):
```

**Verify**: `hermes kanban show <task_id> | head -15` to see the actual parents line format.

### RC2: `promote --force` Is Not Enough

`promote --force` changes status to `ready` immediately, but the **kanban dispatcher checks parent dependencies again at claim time**. If the parent is still blocked, claim is rejected.

**Fix**: Must unlink first, then promote:

```bash
hermes kanban unlink <parent_id> <child_id>
hermes kanban promote <child_id>
```

### RC3: State File Locks Out Already-Processed Tasks

After a failed attempt, the task ID is saved to `state["unblocked_plan_ids"]`. Future watchdog runs skip it. A task that was "processed" but not actually unlinked is permanently skipped.

**Recovery**:

```bash
# 1. Check state file
cat ~/.hermes/kanban-watchdog/<board>.json | python3 -c "import sys,json; print(json.load(sys.stdin)['unblocked_plan_ids'])"

# 2. Remove the stuck task ID
python3 -c "
from pathlib import Path
import json
p = Path.home() / '.hermes' / 'kanban-watchdog' / '<board>.json'
state = json.loads(p.read_text())
state['unblocked_plan_ids'] = [x for x in state['unblocked_plan_ids'] if x != '<stuck_task_id>']
p.write_text(json.dumps(state, indent=2, ensure_ascii=False))
"

# 3. Manually unlink and promote
hermes kanban unlink <parent_id> <child_id>
hermes kanban promote <child_id>

# 4. Run watchdog to verify
python3 /path/to/kanban_watchdog.py --board <board>
```

### RC4: Old State File Missing New Fields (KeyError)

When watchdog version upgrades, old state files (created by previous version) may lack new fields.

**Consequence**: `KeyError` at `state["unblocked_plan_ids"]` → watchdog crashes before any processing.

**Fix**: `run_check()` must use `state.setdefault()` to fill all missing fields before use:

```python
state.setdefault("unblocked_plan_ids", [])
state.setdefault("bridged_plan_ids", [])
state.setdefault("unblocked_auditor_ids", [])
state.setdefault("build_checked_exec_ids", [])
state.setdefault("completed_tasks", [])
state.setdefault("notified_completion", False)
```

This is automatic and requires no manual action.

## Duplicate Repair Tasks

Auditor may create a second "第 2 轮" repair task if the first one appears stuck (even though it's actually deadlocked). Both depend on the same blocked auditor. The second one is redundant.

**Recovery**:

```bash
# 1. Archive the duplicate
hermes kanban archive <duplicate_task_id>

# 2. Remove both from state file (see RC3 above)
# 3. Unlink + promote the first one
hermes kanban unlink <parent_id> <first_task_id>
hermes kanban promote <first_task_id>
```

## Compile-Verification Backtracking Bug

When compile-verification was added (v1.3.0), watchdog would re-compile **all historical** repair-execution tasks on first run, including ones from prior rounds whose code state no longer matches the current codebase. This created false-negative compile failures and spawned spurious "编译修复" tasks.

**Fix** (v1.4.0+): `run_check()` establishes a baseline on first run — all currently-done repair-execution tasks are immediately marked as "already checked" so they're never re-verified:

```python
if not state.get("_baseline_done"):
    history_exec = [t["id"] for t in tasks
                    if t["assignee"] == "builder"
                    and "修复执行" in t["title"]
                    and t["status"] == "done"
                    and t["id"] not in state["build_checked_exec_ids"]]
    if history_exec:
        state["build_checked_exec_ids"].extend(history_exec)
    state["_baseline_done"] = True
```

## Wrong Auditor Linking Bug

When compile-verification failed, `_create_build_fix_task()` linked the new "编译修复" task to **all** blocked auditor tasks, regardless of whether they were on the same dependency chain. This would add the new compile-fix task as a parent of unrelated auditors, blocking them permanently.

**Fix** (v1.4.0+): Only link to blocked auditors whose `parents` list contains the repair-execution task that triggered the compile failure:

```python
linked_auditors = []
for t in tasks:
    if t["assignee"] == "auditor" and t["status"] == "blocked":
        deps = get_task_deps(board, t["id"])
        if exec_task["id"] in deps:
            linked_auditors.append(t)
```

## Full Prevention Checklist

- [ ] Watchdog v1.4.0+ deployed (`get_task_deps` parses `parents:` format)
- [ ] `run_check()` has `setdefault()` for all state fields
- [ ] `check_blocked_start()` does unlink before promote
- [ ] `_create_build_fix_task()` only links to same-chain auditors
- [ ] `run_check()` establishes baseline on first run (no backtracking)
- [ ] Cron already deployed: `crontab -l | grep kanban_watchdog`