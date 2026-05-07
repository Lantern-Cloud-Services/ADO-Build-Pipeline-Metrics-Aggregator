# Design: Differentiate Wait/Approval Time from Active Execution Time

**Issue**: #1 — "Enhance pipeline usage to report based on pipeline Idle/Wait Time"  
**Date**: 2025-07-14  
**Status**: Design Exploration (Alpha Council Output)

---

## 1. Definitions

### 1.1 Duration Taxonomy

For every pipeline run, the following time buckets are defined:

| Metric | Definition | Computation |
|--------|-----------|-------------|
| **queue_duration** | Time from when the run was queued until it was assigned and started executing. Includes agent availability wait. | `startTime − queueTime` |
| **wall_clock_duration** | Total elapsed time from run start to finish. This is what `avg_duration_seconds` / `total_duration_seconds` currently report. | `finishTime − startTime` |
| **active_duration** | Time agents were actually executing work (jobs running on agents). | `Σ(job.finishTime − job.startTime)` for all `type=Job` timeline records |
| **approval_duration** | Time spent waiting for human approvals (manual interventions, environment checks that require human sign-off). | `Σ(checkpoint.finishTime − checkpoint.startTime)` for `type=Checkpoint` records where sub-records indicate `Checkpoint.Approval` |
| **wait_duration** | All non-execution idle time within the run's wall clock. Encompasses approval waits, gate check waits, agent-unavailability gaps between stages, post-stage delays, etc. | `wall_clock_duration − active_duration` |

### 1.2 The Identity Equation

```
wall_clock_duration = active_duration + wait_duration
wait_duration ≥ approval_duration  (approval is a strict subset of wait)
total_elapsed = queue_duration + wall_clock_duration
```

### 1.3 Where Things Fall

| Scenario | Bucket |
|----------|--------|
| Human clicks "Approve" on environment check | `approval_duration` (and `wait_duration`) |
| Gate check (automated policy evaluation) waiting | `wait_duration` (but NOT `approval_duration` unless it's a manual approval gate) |
| No agent available between stages | `wait_duration` |
| Post-checkout pause / stage transition gap | `wait_duration` |
| Agent actively running a task | `active_duration` |
| Pre-run queue wait (before startTime) | `queue_duration` (separate from the run's wall clock) |
| Parallel jobs executing simultaneously | `active_duration` may exceed `wall_clock_duration` in theory, but per the identity we use wall_clock as the anchor — see §6 Edge Cases |

### 1.4 Why `wait_duration = wall_clock − active` Instead of Summing Checkpoints

Checkpoint records don't capture ALL idle time. Gaps between stages where no timeline record exists (agent spin-up, orchestration overhead, network delays) are invisible to checkpoint-based summation. The subtraction approach (`wall_clock − Σjobs`) is more robust: it catches everything that ISN'T active execution, regardless of whether ADO logged a specific checkpoint for it. The `approval_duration` is then an _annotated subset_ of `wait_duration`.

---

## 2. Data Source

### 2.1 Timeline API Records

The build timeline API (`{org}/{project}/_apis/build/builds/{buildId}/timeline`) returns `records[]`, each with:

```
type:       "Stage" | "Phase" | "Job" | "Task" | "Checkpoint" | "Checkpoint.Approval" | "Checkpoint.TaskCheck"
id:         guid
parentId:   guid (null for top-level Stage records)
name:       string
state:      "pending" | "inProgress" | "completed" | "canceling" | "canceled"
result:     "succeeded" | "failed" | "canceled" | "skipped" | "abandoned"
startTime:  ISO 8601 (nullable — pending records have no startTime)
finishTime: ISO 8601 (nullable)
identifier: string (e.g., "Checkpoint", "Checkpoint.Approval")
```

**Records needed for this feature:**

| Purpose | Filter | Fields Used |
|---------|--------|-------------|
| Active duration | `type == "Job"` | `startTime`, `finishTime` |
| Approval duration | `type == "Checkpoint"` AND has child with `type == "Checkpoint.Approval"`, OR `identifier` contains `"Approval"` | `startTime`, `finishTime` of the Checkpoint parent |
| Gate check duration | `type == "Checkpoint"` AND has child with `type == "Checkpoint.TaskCheck"` | `startTime`, `finishTime` |

**Approval detection heuristic (ordered by reliability):**

1. Look for records with `type == "Checkpoint.Approval"` — these are the approval sub-records. Their `parentId` points to a `Checkpoint` parent. Use the parent's `startTime`/`finishTime` as the approval window (the parent spans the entire checkpoint including any pre/post overhead).
2. Fallback: records with `type == "Checkpoint"` whose `name` contains "Approval" or whose `identifier` == `"Checkpoint.Approval"`.
3. If no approval-typed records exist in a Checkpoint, classify it as a gate/automated check (contributes to `wait_duration` but not `approval_duration`).

### 2.2 Timeline Fetch Strategy

**Current state**: Timeline is ONLY fetched when `--jobs_output` is set (line 961-989 in `process_project_live`). The `process_project_mock` function doesn't generate timeline data at all.

**Proposed strategy — Option A (Recommended): Always fetch timeline when computing wait times**

Add a new flag `--include-wait-times` (see §4). When set:
- Timeline is fetched for EVERY build (not just when `--jobs_output` is set).
- The timeline is used to compute `active_duration` (sum of Job records) and `approval_duration` (sum of approval Checkpoints).
- `wait_duration` is derived via subtraction.
- If `--jobs_output` is ALSO set, the same timeline response is reused (no double-fetch).

**Option B (rejected): Auto-detect from timeline presence**
This would mean always fetching timelines, which is expensive for large orgs. Rejected because the user may not need wait-time decomposition for every run.

**Option C (rejected): Gate behind `--jobs_output`**
Couples job-level detail with wait-time reporting, which are conceptually independent. A user may want pipeline-level wait-time aggregates without caring about individual job details.

### 2.3 Fallback When Timeline Is Unavailable

Scenarios:
- Timeline API returns 404 (purged for old runs, typically >18 months)
- Timeline API returns 200 but `records` is empty
- Network error on timeline fetch

**Fallback behavior:**
- `active_duration` = `wall_clock_duration` (assume all time was active — conservative)
- `wait_duration` = 0
- `approval_duration` = 0
- Emit a verbose warning: `"Timeline unavailable for build {id}; wait-time metrics will be zero"`
- Track `timeline_available_count` vs `total_run_count` per pipeline to let the user know data completeness

---

## 3. Schema Changes

### 3.1 Pipeline Aggregate CSV — New Columns

Append to the RIGHT of existing columns (preserving backward compatibility for consumers that read by position):

```yaml
# Existing columns unchanged:
# org_url, project_id, project_name, pipeline_id, pipeline_name, run_count, avg_duration_seconds, total_duration_seconds

# New columns (only populated when --include-wait-times is used):
- name: avg_active_duration_seconds
  type: integer
  description: Average active execution time per run (sum of job durations / run count)

- name: total_active_duration_seconds
  type: integer
  description: Sum of active execution times across all runs

- name: avg_wait_duration_seconds
  type: integer
  description: Average wait/idle time per run (wall_clock - active)

- name: total_wait_duration_seconds
  type: integer
  description: Sum of wait/idle times across all runs

- name: avg_approval_duration_seconds
  type: integer
  description: Average approval wait time per run

- name: total_approval_duration_seconds
  type: integer
  description: Sum of approval wait times across all runs

- name: avg_queue_duration_seconds
  type: integer
  description: Average pre-run queue time (queueTime to startTime)

- name: total_queue_duration_seconds
  type: integer
  description: Sum of pre-run queue times across all runs
```

**When `--include-wait-times` is NOT set**, these columns are still present in the header but populated with empty strings (or 0). This keeps the schema stable.

**Alternatively**: only emit the extra columns when the flag is set. This is simpler but means downstream consumers see a variable-width CSV. The "always present, default to 0" approach is safer.

**Recommendation**: Always emit all columns. Default to 0 when `--include-wait-times` is not set. This maximizes backward compatibility since existing consumers will just see extra columns they can ignore.

### 3.2 Job Details CSV — New Columns

Currently the job_details CSV has `duration_seconds`. Add:

```yaml
- name: queue_to_job_seconds
  type: integer
  description: Time from run queueTime to this job's startTime
```

No per-job wait/approval columns needed — wait time is a run-level concept (gaps between jobs), not a job-level concept. The job CSV already captures individual job durations.

### 3.3 Summary Markdown — New Sections

Add to each section (Overview, By Organization, By Project):

```markdown
- **Total Active Duration**: X seconds (HH:MM:SS)
- **Total Wait Duration**: Y seconds (HH:MM:SS)
- **Total Approval Duration**: Z seconds (HH:MM:SS)
- **Efficiency Ratio**: XX.X% (active / wall_clock)
```

The efficiency ratio is a derived percentage: `(total_active_duration / total_wall_clock_duration) * 100`. It immediately tells operators what fraction of their pipeline time is productive.

### 3.4 Schema Version Bump

Update `pipeline_aggregate.csv.schema.yaml` version from 1 to 2. Include a `changes` note:

```yaml
version: 2
changes:
  - v2: Added wait-time differentiation columns (avg/total for active, wait, approval, queue durations)
```

---

## 4. CLI / Config

### 4.1 New Flag

```
--include-wait-times    Include wait/approval/active duration breakdown in pipeline aggregates.
                        Requires timeline API access for each build (increases API calls).
                        Default: false
```

**Why a flag (not always-on)?**
- Timeline fetch is 1 API call per build. For an org with 100 pipelines × 50 runs = 5,000 extra API calls. This is significant.
- Users who only need run counts and total durations shouldn't pay this cost.
- Mock mode can honor this flag cheaply, so tests can cover it without API concerns.

### 4.2 Flag Interactions

| `--include-wait-times` | `--jobs_output` | Behavior |
|------------------------|-----------------|----------|
| No | No | Current behavior. No timeline fetched. Pipeline CSV has 8 columns (new columns present but zeroed). |
| No | Yes | Timeline fetched for jobs only. Could opportunistically compute wait metrics from the same data. **Design choice**: populate wait-time columns as a bonus since we have the data anyway. Log a note. |
| Yes | No | Timeline fetched for wait-time computation. Job-level CSV not emitted. |
| Yes | Yes | Timeline fetched once, used for both purposes. |

**Important interaction**: When `--jobs_output` is set but `--include-wait-times` is not, we ALREADY have the timeline data. It would be wasteful to discard it. Recommendation: silently populate the wait-time columns whenever timeline data is available, regardless of whether `--include-wait-times` was explicitly set. The flag controls whether we FORCE timeline fetches, not whether we USE timeline data that's already available.

### 4.3 No Changes to Existing Flags

All existing flags retain their current behavior. `--mock` works with `--include-wait-times`.

---

## 5. Mock Mode

### 5.1 Current State

`generate_mock_runs()` produces runs with `queueTime`, `startTime`, `finishTime` but no timeline records. `generate_mock_jobs()` produces `type=Job` records with sequential start/finish times that perfectly tile the run's wall clock — no gaps, no approvals.

### 5.2 Proposed Changes

#### 5.2.1 New Function: `generate_mock_timeline(run, seed)`

Returns a complete mock timeline including:

1. **Stage records**: 1-3 stages per run (Build, Test, Deploy). Each stage has a `type=Stage`.
2. **Job records**: Existing `generate_mock_jobs` output, nested under stages.
3. **Checkpoint/Approval records**: For ~40% of runs (deterministic via seed), insert a `Checkpoint` record between stages with:
   - A child `Checkpoint.Approval` record
   - Duration: 5-120 minutes (simulating real-world approval waits)
   - The Checkpoint's `startTime` = previous stage's `finishTime`
   - The Checkpoint's `finishTime` = next stage's `startTime`

4. **Adjusting run finishTime**: The mock run's `finishTime` must be extended to account for approval gaps. Currently `finishTime = startTime + job_duration`. With approvals: `finishTime = startTime + Σ(job_durations) + Σ(approval_durations)`.

#### 5.2.2 Deterministic Approval Patterns

Use the seed to create predictable patterns:
- Pipeline IDs ending in even numbers get approvals on 50% of runs
- Pipeline IDs ending in odd numbers get approvals on 20% of runs
- This ensures tests always have SOME pipelines with approvals and some without
- Approval durations: seeded uniform distribution [300, 7200] seconds (5 min to 2 hours)

#### 5.2.3 Mock Timeline Structure Example

```python
{
  "records": [
    {"type": "Stage", "name": "Build", "startTime": "T1", "finishTime": "T2", ...},
    {"type": "Job", "name": "Build Job", "parentId": "<stage-id>", "startTime": "T1", "finishTime": "T2", ...},
    {"type": "Checkpoint", "name": "Approval Check", "startTime": "T2", "finishTime": "T3", ...},
    {"type": "Checkpoint.Approval", "name": "Manual Approval", "parentId": "<checkpoint-id>", "startTime": "T2", "finishTime": "T3", ...},
    {"type": "Stage", "name": "Deploy", "startTime": "T3", "finishTime": "T4", ...},
    {"type": "Job", "name": "Deploy Job", "parentId": "<stage-id>", "startTime": "T3", "finishTime": "T4", ...},
  ]
}
```

#### 5.2.4 Integration with `process_project_mock`

When `--include-wait-times` is set (or `--jobs_output` is set):
- Call `generate_mock_timeline(run, seed)` for each run
- Extract job records from timeline (instead of separate `generate_mock_jobs`)
- Compute `active_duration`, `wait_duration`, `approval_duration` from timeline
- Feed these per-run values into the aggregation function

---

## 6. Edge Cases

### 6.1 Runs That Never Started

**Scenario**: `startTime` is null, `finishTime` is null, `queueTime` exists. Run was queued but never picked up (perhaps canceled while queued).

**Handling**: 
- Skip from duration aggregation entirely (current behavior already handles this via `parse_duration_seconds` returning 0).
- Do NOT count toward `run_count` for averaging purposes, OR count with all durations as 0.
- **Recommendation**: Exclude from `run_count`. A run that never started doesn't contribute meaningful duration data. Add a `skipped_run_count` field if needed for visibility.

### 6.2 Canceled Mid-Approval

**Scenario**: Run was waiting for approval, user canceled. `Checkpoint` record has `state=canceled`, `result=canceled`. The `finishTime` of the checkpoint reflects when the cancel happened.

**Handling**:
- The checkpoint's `(finishTime - startTime)` still represents time spent waiting (even though it ended in cancellation).
- Include this in `approval_duration` and `wait_duration`.
- The run's `finishTime` should also reflect the cancel time — so `wall_clock_duration` captures the full elapsed time.

### 6.3 Multi-Stage Pipelines with Multiple Checkpoints

**Scenario**: Pipeline has stages: Build → [Approval] → Staging → [Approval] → Production. Two approval checkpoints.

**Handling**:
- Sum ALL Checkpoint.Approval durations for the run.
- `approval_duration = Σ(all approval checkpoint durations)`.
- This correctly captures the total human-wait time even across multiple stages.

### 6.4 Parallel Stages with Overlapping Waits

**Scenario**: Two stages run in parallel, each with its own approval checkpoint. Approval A runs from T1-T3, Approval B runs from T2-T4. Naive sum = (T3-T1) + (T4-T2), but actual wall-clock wait is only (T4-T1).

**Handling**:
- For `approval_duration`: Use the NAIVE SUM (add up all approval durations individually). This represents total approval-person-hours, not wall-clock approval time.
- For `wait_duration`: Use the subtraction method (`wall_clock - Σjobs`), which naturally handles overlap correctly since parallel jobs' durations don't double-count wall-clock time.
- Document this: "approval_duration may exceed wait_duration for pipelines with parallel approval stages; it represents cumulative approval effort, not wall-clock approval time."

### 6.5 Parallel Jobs Within a Stage

**Scenario**: Stage has 3 parallel jobs running simultaneously. Sum of job durations = 45 min, but the stage only takes 20 min wall-clock.

**Handling**:
- `active_duration = Σ(job durations)` — this represents total agent-time consumed, not wall-clock.
- `wait_duration = wall_clock - active_duration` — this could go NEGATIVE if jobs run in parallel.
- **Solution**: Clamp `wait_duration` to `max(0, wall_clock - active_duration)`. When `active_duration > wall_clock`, it means parallel execution — there's no "waiting" but there IS parallelism.
- **Alternative**: Redefine `active_duration` for the purpose of the identity as `min(Σjobs, wall_clock)`. But this loses the "total agent compute time" insight.
- **Recommendation**: Report BOTH `active_duration` (agent-compute-seconds, can exceed wall clock) and `wait_duration` (clamped to ≥ 0). Add a note in docs that `active + wait = wall_clock` holds only for serial pipelines; for parallel pipelines, `active ≥ wall_clock` and `wait = 0`.

### 6.6 Timeline Unavailable (Purged or 404)

**Scenario**: Very old runs where Azure DevOps has purged timeline data (typically after 18 months), or API errors.

**Handling**: See §2.3 Fallback. Active = wall_clock, wait = 0, approval = 0. Verbose warning emitted. Track per-pipeline `timeline_coverage_pct = timeline_available_count / run_count * 100` so users know data quality.

### 6.7 Division by Zero in Averages

**Scenario**: `run_count = 0` for a pipeline (empty date range).

**Handling**: Already handled — `aggregate_pipelines_new_format` skips pipelines with empty runs. The `avg = total / count if count > 0 else 0` pattern is already used and should be extended to all new avg columns.

### 6.8 Timeline Records with Missing Timestamps

**Scenario**: A Job record exists but `startTime` or `finishTime` is null (e.g., job was skipped, or still pending when timeline was fetched for a partially-completed run).

**Handling**:
- Skip records with null `startTime` or `finishTime` from duration computation.
- Use `parse_duration_seconds` which already returns 0.0 for unparseable timestamps.
- Do NOT include skipped/pending jobs in `active_duration`.

### 6.9 Runs with Only a Single Stage and No Approvals

**Scenario**: Simple CI pipeline — one stage, one job, no checkpoints.

**Handling**:
- `active_duration ≈ wall_clock_duration` (minus minor orchestration overhead)
- `approval_duration = 0`
- `wait_duration = wall_clock - active ≈ 0` (small positive number for orchestration overhead)
- This is the common case and should Just Work.

---

## 7. Implementation Sketch (Non-Code)

### 7.1 Per-Run Duration Extraction

```
function extract_run_durations(run, timeline):
    wall_clock = run.finishTime - run.startTime
    queue = run.startTime - run.queueTime
    
    if timeline is available:
        active = sum(job.finish - job.start for job in timeline.records if job.type == "Job" and job.startTime and job.finishTime)
        approval = sum(cp.finish - cp.start for cp in timeline.records if is_approval_checkpoint(cp))
        wait = max(0, wall_clock - active)
    else:
        active = wall_clock
        approval = 0
        wait = 0
    
    return { wall_clock, queue, active, wait, approval }
```

### 7.2 Aggregation

```
function aggregate_pipeline_with_waits(runs_with_durations):
    totals = sum each metric across runs
    averages = totals / run_count for each metric
    return { run_count, avg_*, total_*, ... }
```

### 7.3 Data Flow

```
                    ┌─────────────┐
                    │  Build List  │
                    │  (API/Mock)  │
                    └──────┬──────┘
                           │
                   ┌───────▼────────┐
                   │ For each build: │
                   │ Fetch Timeline  │◄── only if --include-wait-times OR --jobs_output
                   └───────┬────────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
        ┌─────▼─────┐ ┌───▼───┐  ┌─────▼──────┐
        │  Extract   │ │Extract│  │  Extract   │
        │  Job rows  │ │Active │  │  Approval  │
        │ (for CSV)  │ │Duration│  │  Duration  │
        └─────┬─────┘ └───┬───┘  └─────┬──────┘
              │            │            │
              │     ┌──────▼──────┐     │
              │     │  Compute    │◄────┘
              │     │  wait_dur = │
              │     │  wall-active│
              │     └──────┬──────┘
              │            │
        ┌─────▼────────────▼─────┐
        │     Aggregate per      │
        │     pipeline           │
        └─────────┬──────────────┘
                  │
    ┌─────────────┼──────────────┐
    │             │              │
┌───▼───┐   ┌────▼────┐   ┌─────▼─────┐
│Pipeline│   │  Jobs   │   │  Summary  │
│  CSV   │   │  CSV    │   │    .md    │
└────────┘   └─────────┘   └───────────┘
```

---

## Open Questions

1. **Approval vs. Gate distinction in the issue text**: The issue says "wait/approval time differentiation" — does the user want to distinguish between *manual approvals* and *automated gate checks* (e.g., Azure Monitor health checks, business hours gates)? Or is "approval" a catch-all for any non-execution wait? The design above separates them, but we should confirm priority.

2. **Should `wait_duration` include `queue_duration`?** The issue mentions "time that includes approval times" suggesting the user's pain is about within-run waits (between stages), not pre-run queue. The design keeps them separate, but the user may want a single "total non-productive time" column that sums both.

3. **Per-run CSV output**: Currently there's no per-run (as opposed to per-pipeline-aggregate or per-job) CSV. Should we add one? The wait-time metrics are most interesting at the individual run level — a pipeline's average hides the variance. A per-run CSV would show exactly which runs had long approval waits.

4. **Historical data completeness**: For the user's existing Azure DevOps instance, how far back does timeline data exist? If they're analyzing 2+ year windows, many older runs may lack timeline data. Should we surface `timeline_coverage_pct` as a column to make data quality transparent?

5. **Should canceled/failed runs be included or excluded from wait-time averages?** A run canceled mid-approval inflates `approval_duration` averages. The user may want separate success-only aggregates (the legacy `aggregate_builds_for_project` already computes `success_duration_seconds` separately).

6. **Existing `total_duration_seconds` rename?** Currently `avg_duration_seconds` and `total_duration_seconds` represent wall-clock time. Should we rename them to `avg_wall_clock_seconds` / `total_wall_clock_seconds` for clarity alongside the new columns? Or is that a breaking change we should avoid?

---

## Wild Ideas

### 🔥 Efficiency Ratio as a First-Class Metric
Add `efficiency_pct` = `(active_duration / wall_clock_duration) * 100` as a column. Pipelines with < 50% efficiency are spending more time waiting than working. This is the single most actionable metric for identifying pipelines that need approval workflow redesign.

### 📊 Per-Stage Breakdown CSV
A new output file (`--stages_output stages.csv`) with one row per stage per run:
```
org_url, project_id, pipeline_id, run_id, stage_name, stage_order, stage_duration_seconds, has_approval, approval_duration_seconds
```
This enables analysis like "the Deploy stage always takes 2 hours because of approvals, but Build+Test are 10 minutes."

### 🚨 "Chronically Blocked" Pipeline Detection
In the summary markdown, add a section: **"Pipelines with Highest Wait Ratios"** — top 10 pipelines by `wait_duration / wall_clock_duration`. These are the pipelines where human process is the bottleneck, not compute. Could include:
```markdown
## ⚠️ Highest Wait Ratios
| Pipeline | Project | Efficiency | Avg Wait | Avg Approval |
|----------|---------|-----------|----------|-------------|
| prod-deploy | WebApp | 12% | 45min | 42min |
| staging-release | API | 23% | 30min | 28min |
```

### 📈 Trend Detection
If the user runs this tool periodically (e.g., monthly), support a `--compare previous.csv` flag that loads a prior pipeline CSV and computes deltas: "approval_duration increased 40% since last month." This turns the tool from a snapshot reporter into a trend analyzer.

### 🎯 Approval SLA Tracking
Let users define an `--approval-sla-minutes 30` flag. Any run where `approval_duration > SLA` gets flagged. The summary reports how many runs breached SLA. This directly measures whether the team's approval process is meeting expectations.

### 🔄 Real-Time / Streaming Mode
A `--watch` mode that polls for new completed runs every N seconds and appends to the CSV. Combined with wait-time metrics, this enables real-time dashboarding of pipeline efficiency. (Probably over-engineered for a CLI tool, but the data plumbing would be the same.)
