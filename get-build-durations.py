#!/usr/bin/env python3
import argparse
import csv
import datetime as dt
import hashlib
import os
from pickle import FALSE
import random
import sys
import time

# Import requests only if needed for non-mock mode
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    requests = None

from typing import Dict, Any, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

API_VERSION = "7.0"

PROXIES = {
    #redacted
}

ALL_POOLS_CACHE: List[Dict[str, Any]] = []
POOLS_CACHE: Dict[int, Dict[str, Any]] = {}
AGENT_POOLS_CACHE: Dict[int, Dict[str, Any]] = {}

# Mock mode configuration
# MOCK_MODE will be set based on --mock flag (default: live mode)

def get_org_name(org_url: str) -> str:
    """Extract organization name from Azure DevOps URL.
    
    Supports both URL formats:
    - https://dev.azure.com/myorg -> myorg
    - https://myorg.visualstudio.com -> myorg
    """
    url = org_url.rstrip('/')
    if 'dev.azure.com' in url:
        return url.split('/')[-1]
    elif 'visualstudio.com' in url:
        # https://myorg.visualstudio.com -> myorg
        return url.split('//')[1].split('.')[0]
    # Fallback: last path segment
    return url.split('/')[-1]

def get_pat_for_org(org_url: str, default_pat: str | None, verbose: bool = False) -> str | None:
    """Resolve PAT for a specific organization.
    
    Resolution order:
    1. AZDO_PAT_<ORGNAME> environment variable (case-insensitive org name)
    2. default_pat (from --pat or AZDO_PAT)
    
    Args:
        org_url: The Azure DevOps organization URL
        default_pat: Fallback PAT from --pat argument or AZDO_PAT env var
        verbose: Whether to log PAT resolution details
    
    Returns:
        The resolved PAT or None if not found
    """
    org_name = get_org_name(org_url)
    org_specific_env_var = f"AZDO_PAT_{org_name.upper()}"
    org_specific_pat = os.getenv(org_specific_env_var)
    
    if org_specific_pat:
        if verbose:
            print(f"Using PAT from {org_specific_env_var} for {org_url}", file=sys.stderr)
        return org_specific_pat
    
    if default_pat:
        if verbose:
            print(f"Using default PAT for {org_url}", file=sys.stderr)
        return default_pat
    
    return None

def get_deterministic_seed(org_url: str, begin: str, end: str) -> int:
    """Generate deterministic seed from org URL and date range."""
    key = f"{org_url}|{begin}|{end}|{API_VERSION}"
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16)

def generate_mock_projects(org_url: str, seed: int) -> List[Dict[str, Any]]:
    """Generate deterministic mock projects for an organization."""
    rng = random.Random(seed)
    project_count = rng.randint(3, 8)  # 3-8 projects per org
    
    projects = []
    for i in range(project_count):
        project_id = f"proj-{org_url.split('/')[-1]}-{i+1:03d}"
        project_name = f"Project-{chr(65+i)}"
        projects.append({
            "id": project_id,
            "name": project_name,
            "url": f"{org_url}/{project_name}"
        })
    
    return projects

def generate_mock_pipelines(project_name: str, seed: int) -> List[Dict[str, Any]]:
    """Generate deterministic mock pipelines for a project."""
    rng = random.Random(seed + hash(project_name))
    pipeline_count = rng.randint(2, 6)  # 2-6 pipelines per project
    
    pipelines = []
    pipeline_types = ['Build', 'Deploy', 'Test', 'Release', 'Validate']
    
    for i in range(pipeline_count):
        pipeline_id = 1000 + i
        pipeline_type = rng.choice(pipeline_types)
        pipeline_name = f"{project_name}-{pipeline_type}-Pipeline"
        
        pipelines.append({
            "id": pipeline_id,
            "name": pipeline_name,
            "definition": {
                "id": pipeline_id,
                "name": pipeline_name
            }
        })
    
    return pipelines

def generate_mock_runs(pipeline: Dict[str, Any], begin_dt: dt.datetime, end_dt: dt.datetime, seed: int) -> List[Dict[str, Any]]:
    """Generate deterministic mock pipeline runs within date range.

    A subset of runs (~25%) include a deterministic ``mock_wait_seconds``
    field representing approval/checkpoint wait time embedded within the
    run's wall-clock duration. When the ``--include-wait-time`` flag is
    enabled this value is reported as wait time; otherwise it is ignored,
    keeping existing mock CSV output byte-identical to prior versions.
    """
    pipeline_id = pipeline['id']
    rng = random.Random(seed + pipeline_id)
    
    # Generate 2-15 runs per pipeline in the date range
    run_count = rng.randint(2, 15)
    duration_days = (end_dt - begin_dt).days
    
    runs = []
    for i in range(run_count):
        # Random start time within range
        days_offset = rng.uniform(0, duration_days)
        start_time = begin_dt + dt.timedelta(days=days_offset)
        
        # Duration: 2-45 minutes typically
        duration_minutes = rng.uniform(2, 45)
        finish_time = start_time + dt.timedelta(minutes=duration_minutes)
        
        # Queue time: 0-5 minutes before start
        queue_duration_minutes = rng.uniform(0, 5)
        queue_time = start_time - dt.timedelta(minutes=queue_duration_minutes)
        
        # Status
        status = rng.choices(['succeeded', 'failed', 'canceled'], weights=[85, 12, 3])[0]

        # Approval/idle wait simulation: ~25% of runs have an approval gap
        # embedded within their wall-clock duration. The wait is bounded
        # by the run's own duration to stay self-consistent.
        # Use an INDEPENDENT per-run RNG so adding this feature does not
        # shift the existing rng state and break golden mock outputs.
        # 100003 is just a large prime used to spread per-pipeline seeds
        # across distinct streams; the exact value is not significant.
        wait_rng = random.Random(seed + pipeline_id * 100003 + i + 1)
        if wait_rng.random() < 0.25:
            max_wait = max(0.0, duration_minutes * 60.0 - 30.0)
            mock_wait_seconds = wait_rng.uniform(30.0, min(600.0, max_wait)) if max_wait > 30.0 else 0.0
        else:
            mock_wait_seconds = 0.0

        run_id = 10000 + (pipeline_id * 100) + i
        runs.append({
            "id": run_id,
            "buildNumber": f"{pipeline['name']}-{i+1}",
            "definition": pipeline['definition'],
            "queueTime": queue_time.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            "startTime": start_time.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            "finishTime": finish_time.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            "result": status,
            "mock_wait_seconds": mock_wait_seconds,
            "queue": {
                "pool": {
                    "id": rng.randint(1, 5),
                    "name": f"Pool-{rng.randint(1, 5)}"
                }
            }
        })
    
    return runs

def generate_mock_jobs(run: Dict[str, Any], seed: int) -> List[Dict[str, Any]]:
    """Generate deterministic mock jobs for a pipeline run."""
    run_id = run['id']
    rng = random.Random(seed + run_id)
    
    # 1-4 jobs per run
    job_count = rng.randint(1, 4)
    job_names = ['Build', 'Test', 'Deploy', 'Validate']
    
    jobs = []
    run_start = dt.datetime.fromisoformat(run['startTime'].replace('Z', '+00:00'))
    current_time = run_start
    
    for i in range(job_count):
        job_name = job_names[i % len(job_names)]
        
        # Job duration: 30 seconds to 20 minutes
        job_duration_minutes = rng.uniform(0.5, 20)
        job_start = current_time
        job_finish = job_start + dt.timedelta(minutes=job_duration_minutes)
        current_time = job_finish
        
        # Worker and pool
        pool_id = rng.randint(1, 5)
        worker_name = f"Agent-{pool_id}-{rng.randint(1, 10)}"
        
        jobs.append({
            "name": job_name,
            "type": "Job",
            "workerName": worker_name,
            "startTime": job_start.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            "finishTime": job_finish.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            "result": run['result']  # Jobs inherit run result
        })
    
    return jobs

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Get Azure DevOps pipeline (build) durations per pipeline between two dates. "
            "Supports multiple organizations and generates CSV aggregates plus human-readable summaries."
        )
    )
    parser.add_argument(
        "--org-url",
        action="append",
        required=True,
        help="Azure DevOps organization URL, e.g. https://dev.azure.com/myorg (can be specified multiple times)",
    )
    parser.add_argument(
        "--pat",
        help=(
            "Default Azure DevOps Personal Access Token. "
            "If omitted, will read from AZDO_PAT environment variable. "
            "For org-specific PATs, set AZDO_PAT_<ORGNAME> env vars (e.g., AZDO_PAT_MYORG for https://dev.azure.com/myorg)."
        ),
    )
    parser.add_argument(
        "--begin",
        required=True,
        help="Begin datetime (inclusive), e.g. 2024-01-01 or 2024-01-01T00:00:00",
    )
    parser.add_argument(
        "--end",
        required=True,
        help="End datetime (exclusive), e.g. 2024-01-31 or 2024-01-31T00:00:00",
    )
    parser.add_argument(
        "--output",
        help="Output CSV file path. If omitted, writes to stdout.",
    )

    parser.add_argument(
        "--jobs_output",
        help=(
            "Optional second CSV path for per-job details including the "
            "pool assigned to each job. If omitted, no jobs CSV is written."
        ),
    )

    parser.add_argument(
        "--threads",
        type=int,
        default=4,
        help="Number of worker threads for processing projects in parallel (default: 4)",
    )

    parser.add_argument(
        "--max-projects",
        type=int,
        help="Optional maximum number of projects to process (for sampling/debugging)",
    )

    parser.add_argument(
        "--delay",
        type=int,
        default=0,
        help="Delay in ms between requests (default: 0)",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Turn on verbose logging (default: false)",
    )

    parser.add_argument(
        "--summary_output",
        help=(
            "Path for human-readable summary file. "
            "Defaults to '<output>.summary.md' when --output is provided."
        ),
    )

    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use deterministic mock data instead of live Azure DevOps API (for testing)",
    )

    parser.add_argument(
        "--include-wait-time",
        action="store_true",
        help=(
            "Compute approval/idle wait time for each build by fetching its "
            "timeline and inspecting Checkpoint.Approval / ManualIntervention "
            "records. Populates the wait/active duration columns in the "
            "pipeline CSV. Adds one extra API call per build, so this is "
            "opt-in. When omitted, wait/active columns are reported as 0 and "
            "total_duration_seconds retains its existing meaning (wall-clock "
            "from build start to finish, including approval pauses)."
        ),
    )

    return parser.parse_args()


def iso8601_or_die(value: str) -> str:
    """
    Accepts a date or datetime string and returns an ISO 8601 datetime string in UTC.
    Azure DevOps API wants ISO8601; we normalize here.
    """
    # Try date-only format
    formats = ["%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"]
    parsed = None
    for fmt in formats:
        try:
            parsed = dt.datetime.strptime(value, fmt)
            break
        except ValueError:
            continue

    if parsed is None:
        raise ValueError(
            f"Could not parse datetime '{value}'. "
            f"Supported formats: {', '.join(formats)}"
        )

    # Treat naive datetime as UTC
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)

    return parsed.isoformat()


def get_auth(pat: str) -> Tuple[str, str]:
    # Azure DevOps uses basic auth; username can be anything, PAT is the password
    return "", pat


def get_projects(org_url: str, auth: Tuple[str, str]) -> List[Dict[str, Any]]:
    projects: List[Dict[str, Any]] = []
    url = f"{org_url}/_apis/projects"
    params = {"api-version": API_VERSION}
    while True:
        if VERBOSE:
            print(f"Fetching projects: {url} + {params}", file=sys.stderr)
        resp = requests.get(url, params=params, auth=auth, proxies=PROXIES)
        resp.raise_for_status()
        data = resp.json()
        projects.extend(data.get("value", []))

        if "continuationToken" not in resp.headers:
            break
        token = resp.headers["continuationToken"]
        if not token:
            break
        params["continuationToken"] = token

    return projects


def get_builds_for_project(
    org_url: str,
    project_name: str,
    auth: Tuple[str, str],
    min_finish_time: str,
    max_finish_time: str,
    delay_ms: int,
) -> List[Dict[str, Any]]:
    """
    Fetch all builds for a project whose finishTime is between min_finish_time and max_finish_time.
    """
    builds: List[Dict[str, Any]] = []
    url = f"{org_url}/{project_name}/_apis/build/builds"
    params = {
        "api-version": API_VERSION,
        "minTime": min_finish_time,
        "maxTime": max_finish_time,
        "$top": 1000,
        "queryOrder": "finishTimeAscending",
    }

    continuation_token = None
    while True:
        if continuation_token:
            params["continuationToken"] = continuation_token
        elif "continuationToken" in params:
            del params["continuationToken"]

        if delay_ms > 0:
            time.sleep(delay_ms / 1000)

        if VERBOSE:
            print(f"Fetching builds: {url} + {params}", file=sys.stderr)
        resp = requests.get(url, params=params, auth=auth, proxies=PROXIES)
        resp.raise_for_status()
        data = resp.json()
        builds.extend(data.get("value", []))

        continuation_token = resp.headers.get("x-ms-continuationtoken")
        if not continuation_token:
            break

    return builds


def parse_ado_timestamp(ts: str) -> dt.datetime:
    """
    Parse Azure DevOps ISO8601 timestamps like:
      2021-01-18T16:27:34.7200223Z
      2021-01-18T06:01:37.1772993+00:00
    into a timezone-aware datetime (UTC).
    """
    if not ts:
        return None

    original = ts

    # Normalize timezone: strip 'Z' or '+/-HH:MM', assume UTC
    tz = dt.timezone.utc
    if ts.endswith("Z"):
        ts = ts[:-1]
    else:
        t_pos = ts.find("T")
        if t_pos != -1:
            plus = ts.find("+", t_pos)
            minus = ts.find("-", t_pos)
            # pick first offset marker after 'T'
            idx_candidates = [i for i in (plus, minus) if i != -1]
            if idx_candidates:
                ts = ts[:min(idx_candidates)]

    # Now ts is like '2021-01-18T16:27:34.7200223' or without fraction
    if "." in ts:
        base, frac = ts.split(".", 1)
        # Truncate/pad to microseconds (6 digits)
        frac = (frac + "000000")[:6]
        ts = f"{base}.{frac}"

    try:
        dt_obj = dt.datetime.fromisoformat(ts)
    except ValueError as e:
        raise ValueError(f"Failed to parse timestamp '{original}': {e}")

    return dt_obj.replace(tzinfo=tz)


def parse_duration_seconds(start_time: str, finish_time: str) -> float:
    """
    Compute duration in seconds between two ISO8601 timestamps.
    """
    if not start_time or not finish_time:
        return 0.0

    start = parse_ado_timestamp(start_time)
    finish = parse_ado_timestamp(finish_time)
    return (finish - start).total_seconds()


# Timeline record types that represent the build being idle / waiting for
# something other than active execution. Approval checkpoints and manual
# intervention tasks pause the build between stages and inflate the
# wall-clock duration without consuming active build time.
_WAIT_RECORD_TYPES = {"Checkpoint.Approval", "ManualIntervention"}


def extract_wait_seconds_from_timeline(timeline: Dict[str, Any]) -> float:
    """
    Compute the total approval/idle wait time (seconds) for a build from
    its timeline.

    Sums the (finishTime - startTime) of timeline records that represent
    the build being paused waiting on a human or external signal:
      - Checkpoint.Approval records (manual approval gates between stages)
      - ManualIntervention task records (mid-pipeline manual steps)

    Note: We intentionally do NOT include the parent "Checkpoint" record
    type because Checkpoint records are containers whose duration covers
    their child Checkpoint.Approval records; including both would
    double-count.

    Returns 0.0 when timeline is empty/None or contains no wait records.
    Overlapping approvals (e.g. parallel stages) are summed, which gives
    the cumulative human-wait cost rather than wall-clock idle time.
    """
    if not timeline:
        return 0.0
    total = 0.0
    for rec in timeline.get("records", []) or []:
        rtype = rec.get("type")
        if rtype in _WAIT_RECORD_TYPES:
            total += parse_duration_seconds(
                rec.get("startTime"), rec.get("finishTime")
            )
    return total


def get_build_timeline(
    org_url: str,
    project_name: str,
    build_id: int,
    auth: Tuple[str, str],
    delay_ms: int,
) -> Dict[str, Any]:
    """
    Fetch the timeline for a given build, which contains per-job records.
    """
    url = f"{org_url}/{project_name}/_apis/build/builds/{build_id}/timeline"
    params = {"api-version": API_VERSION}
    if delay_ms > 0:
        time.sleep(delay_ms / 1000)

    if VERBOSE:
        print(f"Fetching build timeline: {url} + {params}", file=sys.stderr)
    resp = requests.get(url, params=params, auth=auth, proxies=PROXIES)
    resp.raise_for_status()
    return resp.json()


def get_task_pools(
    org_url: str,
    auth: Tuple[str, str],
    delay_ms: int,
) -> List[Dict[str, Any]]:
    """Fetch all task agent pools in the organization."""
    url = f"{org_url}/_apis/distributedtask/pools"
    if delay_ms > 0:
        time.sleep(delay_ms / 1000)

    if VERBOSE:
        print(f"Fetching task pools: {url}", file=sys.stderr)
    resp = requests.get(url, auth=auth, proxies=PROXIES)
    resp.raise_for_status()
    data = resp.json() or {}
    return data.get("value", [])

def ensure_all_pools_loaded(
    org_url: str,
    auth: Tuple[str, str],
    delay_ms: int,
) -> None:
    """
    Populate ALL_POOLS_CACHE once with all agent pools for the org.
    Safe to call multiple times; subsequent calls are no-ops.
    """
    global ALL_POOLS_CACHE

    if ALL_POOLS_CACHE:
        return

    try:
        pools = get_task_pools(org_url, auth, delay_ms)
        ALL_POOLS_CACHE = pools or []
    except Exception as e:
        print(
            f"Warning: failed to fetch task pools for organization when building ALL_POOLS_CACHE: {e}",
            file=sys.stderr,
        )
        ALL_POOLS_CACHE = []

def ensure_all_agents_loaded(
    org_url: str,
    auth: Tuple[str, str],
    delay_ms: int,
) -> None:
    """
    Populate POOLS_CACHE with agents for all pools, and build a global
    mapping from agent name -> pool (AGENT_POOLS_CACHE).

    Safe to call multiple times; subsequent calls are no-ops if caches
    are already populated.
    """
    # If we've already built the agent cache, do nothing
    if AGENT_POOLS_CACHE:
        return

    for pool in ALL_POOLS_CACHE:
        pool_id = pool.get("id")
        pool_name = pool.get("name")
        if pool_id is None:
            continue

        if pool_id not in POOLS_CACHE:
            url = f"{org_url}/_apis/distributedtask/pools/{pool_id}/agents"
            params = {"api-version": API_VERSION}
            if delay_ms > 0:
                time.sleep(delay_ms / 1000)
            try:
                if VERBOSE:
                    print(
                        f"Fetching agents for pool {pool_id}: {url} + {params}",
                        file=sys.stderr,
                    )
                resp = requests.get(url, params=params, auth=auth, proxies=PROXIES)
                resp.raise_for_status()
                data = resp.json() or {}
            except Exception as e:
                print(
                    f"Warning: failed to fetch agents for pool {pool_id} when building agent cache: {e}",
                    file=sys.stderr,
                )
                POOLS_CACHE[pool_id] = []
                continue

            agents = data.get("value", [])
            POOLS_CACHE[pool_id] = agents
        else:
            agents = POOLS_CACHE[pool_id]

        for agent in agents:
            name = agent.get("name")
            if not name:
                continue

            # normalize like in resolve_agent_pool (strip trailing .NN)
            base = name
            if "." in base:
                left, right = base.rsplit(".", 1)
                if right.isdigit():
                    base = left
            key = base.lower()

            # only set if not already mapped
            if key not in AGENT_POOLS_CACHE:
                AGENT_POOLS_CACHE[key] = {
                    "pool_id": pool_id,
                    "pool_name": pool_name,
                }

def resolve_agent_pool(
    worker_name: str,
    org_url: str,
    auth: Tuple[str, str],
    delay_ms: int,
) -> Dict[str, Any]:
    """Resolve the actual pool for a given agent (workerName).
    """
    if not worker_name:
        return {}

    base = worker_name
    if "." in base:
        left, right = base.rsplit(".", 1)
        if right.isdigit():
            base = left

    key = base.lower()

    if key in AGENT_POOLS_CACHE:
        return AGENT_POOLS_CACHE[key]

    return {}

def extract_job_rows_for_build(
    project_name: str,
    build: Dict[str, Any],
    timeline: Dict[str, Any],
    org_url: str,
    auth: Tuple[str, str],
    delay_ms: int,
) -> List[Dict[str, Any]]:
    """Extract per-job rows from a build's timeline, including pool info.

    Attempts to resolve the actual agent pool for each job via the
    Distributed Task API, based on the workerName (agent). If resolution
    fails, falls back to the build-level queue.pool.
    """
    definition = build.get("definition") or {}
    pipeline_id = definition.get("id")
    pipeline_name = definition.get("name") or "<unknown>"

    queue = build.get("queue") or {}
    queue_pool = queue.get("pool") or {}
    queue_pool_id = queue_pool.get("id")
    queue_pool_name = queue_pool.get("name")

    build_id = build.get("id")
    build_number = build.get("buildNumber")

    rows: List[Dict[str, Any]] = []
    for rec in timeline.get("records", []):
        if rec.get("type") != "Job":
            continue

        job_name = rec.get("name")
        worker_name = rec.get("workerName")
        job_start = rec.get("startTime")
        job_finish = rec.get("finishTime")
        job_result = rec.get("result")

        job_duration = parse_duration_seconds(job_start, job_finish)
        job_duration_hms = seconds_to_hms(job_duration)

        queue_time = build.get("queueTime")
        queue_duration = parse_duration_seconds(queue_time, job_start)
        queue_duration_hms = seconds_to_hms(queue_duration)

        # Resolve actual pool for this worker; fall back to queue pool
        resolved = resolve_agent_pool(
            worker_name,
            org_url,
            auth,
            delay_ms,
        )
        pool_id = resolved.get("pool_id") if resolved else queue_pool_id
        pool_name = resolved.get("pool_name") if resolved else queue_pool_name

        rows.append(
            {
                "project": project_name,
                "pipeline_id": pipeline_id,
                "pipeline_name": pipeline_name,
                "build_id": build_id,
                "build_number": build_number,
                "job_name": job_name,
                "worker_name": worker_name,
                "pool_id": pool_id,
                "pool_name": pool_name,
                "job_result": job_result,
                "job_start_time": job_start,
                "job_finish_time": job_finish,
                "job_duration_seconds": job_duration,
                "job_duration_hms": job_duration_hms,
                "queue_time": queue_time,
                "queue_duration_seconds": queue_duration,
                "queue_duration_hms": queue_duration_hms,
            }
        )

    return rows


def aggregate_pipelines_new_format(
    org_url: str,
    project_id: str,
    project_name: str,
    runs_by_pipeline: Dict[int, List[Dict[str, Any]]],
    wait_seconds_by_build_id: Dict[Any, float] = None,
) -> List[Dict[str, Any]]:
    """Aggregate pipeline runs into new CSV format following pipeline_aggregate.csv.schema.yaml

    When `wait_seconds_by_build_id` is provided, each run's idle/approval
    wait time (looked up by build id) is subtracted from total duration
    to compute "active" duration. When not provided (default), wait
    metrics are reported as 0 and active duration equals total duration,
    preserving backward-compatible behavior.
    """
    results = []
    wait_lookup = wait_seconds_by_build_id or {}

    for pipeline_id, runs in runs_by_pipeline.items():
        if not runs:
            continue

        pipeline_name = runs[0].get('definition', {}).get('name', f'Pipeline-{pipeline_id}')

        total_duration_seconds = 0
        total_wait_seconds = 0.0
        run_count = len(runs)

        for run in runs:
            duration = parse_duration_seconds(
                run.get('startTime'),
                run.get('finishTime')
            )
            total_duration_seconds += duration

            wait = wait_lookup.get(run.get('id'), 0.0)
            # Defensive clamping: the live timeline shouldn't normally
            # report wait > active build duration, but timeline records
            # can have missing/inconsistent timestamps, clock skew, or
            # pending approvals on in-progress builds. Clamp to [0, duration]
            # so reported active time is never negative.
            if wait < 0:
                wait = 0.0
            if wait > duration:
                wait = duration
            total_wait_seconds += wait

        avg_duration_seconds = int(total_duration_seconds / run_count) if run_count > 0 else 0
        # Compute integer versions first so the reported active = duration - wait
        # holds exactly in integer arithmetic for downstream consumers.
        total_duration_int = int(total_duration_seconds)
        total_wait_int = int(total_wait_seconds)
        total_active_int = total_duration_int - total_wait_int
        avg_wait_seconds = total_wait_int // run_count if run_count > 0 else 0
        avg_active_seconds = total_active_int // run_count if run_count > 0 else 0

        results.append({
            'org_url': org_url,
            'project_id': project_id,
            'project_name': project_name,
            'pipeline_id': pipeline_id,
            'pipeline_name': pipeline_name,
            'run_count': run_count,
            'avg_duration_seconds': avg_duration_seconds,
            'total_duration_seconds': total_duration_int,
            'total_wait_seconds': total_wait_int,
            'avg_wait_seconds': avg_wait_seconds,
            'total_active_seconds': total_active_int,
            'avg_active_seconds': avg_active_seconds,
        })

    return results

def write_pipeline_csv(file_handle: Any, pipeline_rows: List[Dict[str, Any]]) -> None:
    """T006: Write pipeline CSV following contracts/pipeline_aggregate.csv.schema.yaml"""
    fieldnames = [
        'org_url',
        'project_id', 
        'project_name',
        'pipeline_id',
        'pipeline_name',
        'run_count',
        'avg_duration_seconds',
        'total_duration_seconds',
        'total_wait_seconds',
        'avg_wait_seconds',
        'total_active_seconds',
        'avg_active_seconds',
    ]
    
    writer = csv.DictWriter(file_handle, fieldnames=fieldnames)
    writer.writeheader()
    
    for row in pipeline_rows:
        writer.writerow(row)

def seconds_to_hms(seconds: float) -> str:
    seconds_int = int(round(seconds))
    h, rem = divmod(seconds_int, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def write_jobs_csv(file_handle: Any, job_rows: List[Dict[str, Any]]) -> None:
    """Write jobs CSV following contracts/job_details.csv.schema.yaml"""
    fieldnames = [
        'org_url',
        'project_id',
        'pipeline_id', 
        'run_id',
        'job_id',
        'job_name',
        'duration_seconds',
        'pool_id',
        'pool_name'
    ]
    
    writer = csv.DictWriter(file_handle, fieldnames=fieldnames)
    writer.writeheader()
    
    for row in job_rows:
        writer.writerow(row)

def write_summary_markdown(file_path: str, pipeline_rows: List[Dict[str, Any]]) -> None:
    """T007: Write human-readable summary following contracts/summary.md.schema.yaml"""
    if not pipeline_rows:
        return
    
    # Global aggregates
    total_runs = sum(row['run_count'] for row in pipeline_rows)
    total_duration_seconds = sum(row['total_duration_seconds'] for row in pipeline_rows)
    avg_duration_seconds = int(total_duration_seconds / total_runs) if total_runs > 0 else 0
    total_wait_seconds = sum(row.get('total_wait_seconds', 0) for row in pipeline_rows)
    total_active_seconds = sum(row.get('total_active_seconds', 0) for row in pipeline_rows)
    avg_wait_seconds = int(total_wait_seconds / total_runs) if total_runs > 0 else 0
    avg_active_seconds = int(total_active_seconds / total_runs) if total_runs > 0 else 0
    # Surface wait/active sections in the summary only when at least one
    # build reported approval/idle time. This intentionally doubles as the
    # "is the --include-wait-time flag effective?" signal: when the flag is
    # off, every per-pipeline total is 0 and these sections are omitted; when
    # the flag is on but no builds had approvals, there's no meaningful
    # delta between active and total duration to display.
    has_wait_data = total_wait_seconds > 0

    org_count = len(set(row['org_url'] for row in pipeline_rows))
    project_count = len(set(f"{row['org_url']}|{row['project_id']}" for row in pipeline_rows))
    pipeline_count = len(pipeline_rows)
    
    # Group by org and project for detailed sections
    by_org = {}
    by_project = {}
    
    for row in pipeline_rows:
        org_url = row['org_url']
        project_key = f"{row['org_url']}|{row['project_id']}"
        
        if org_url not in by_org:
            by_org[org_url] = {
                'pipelines': [], 'total_runs': 0, 'total_duration': 0,
                'total_wait': 0, 'total_active': 0,
            }
        by_org[org_url]['pipelines'].append(row)
        by_org[org_url]['total_runs'] += row['run_count']
        by_org[org_url]['total_duration'] += row['total_duration_seconds']
        by_org[org_url]['total_wait'] += row.get('total_wait_seconds', 0)
        by_org[org_url]['total_active'] += row.get('total_active_seconds', 0)
        
        if project_key not in by_project:
            by_project[project_key] = {
                'project_name': row['project_name'],
                'org_url': row['org_url'],
                'pipelines': [],
                'total_runs': 0,
                'total_duration': 0,
                'total_wait': 0,
                'total_active': 0,
            }
        by_project[project_key]['pipelines'].append(row)
        by_project[project_key]['total_runs'] += row['run_count']
        by_project[project_key]['total_duration'] += row['total_duration_seconds']
        by_project[project_key]['total_wait'] += row.get('total_wait_seconds', 0)
        by_project[project_key]['total_active'] += row.get('total_active_seconds', 0)
    
    # Write markdown
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write("# Azure DevOps Pipeline Aggregation Summary\n\n")
        
        # Overview section
        f.write("## Overview\n\n")
        f.write(f"- **Organizations**: {org_count}\n")
        f.write(f"- **Projects**: {project_count}\n") 
        f.write(f"- **Pipelines**: {pipeline_count}\n")
        f.write(f"- **Total Runs**: {total_runs}\n")
        f.write(f"- **Total Duration**: {total_duration_seconds:,} seconds ({seconds_to_hms(total_duration_seconds)})\n")
        f.write(f"- **Average Duration**: {avg_duration_seconds} seconds ({seconds_to_hms(avg_duration_seconds)})\n")
        if has_wait_data:
            wait_pct = (total_wait_seconds / total_duration_seconds * 100) if total_duration_seconds > 0 else 0
            f.write(f"- **Total Active Duration**: {total_active_seconds:,} seconds ({seconds_to_hms(total_active_seconds)})\n")
            f.write(f"- **Average Active Duration**: {avg_active_seconds} seconds ({seconds_to_hms(avg_active_seconds)})\n")
            f.write(f"- **Total Approval/Wait Time**: {total_wait_seconds:,} seconds ({seconds_to_hms(total_wait_seconds)}) — {wait_pct:.1f}% of total\n")
            f.write(f"- **Average Approval/Wait Time**: {avg_wait_seconds} seconds ({seconds_to_hms(avg_wait_seconds)})\n")
        f.write("\n")
        
        # By Organization section
        f.write("## By Organization\n\n")
        for org_url, data in by_org.items():
            org_pipeline_count = len(data['pipelines'])
            org_avg_duration = int(data['total_duration'] / data['total_runs']) if data['total_runs'] > 0 else 0
            
            f.write(f"### {org_url}\n\n")
            f.write(f"- **Pipelines**: {org_pipeline_count}\n")
            f.write(f"- **Total Runs**: {data['total_runs']}\n")
            f.write(f"- **Total Duration**: {data['total_duration']:,} seconds ({seconds_to_hms(data['total_duration'])})\n")
            f.write(f"- **Average Duration**: {org_avg_duration} seconds ({seconds_to_hms(org_avg_duration)})\n")
            if has_wait_data:
                f.write(f"- **Total Active Duration**: {data['total_active']:,} seconds ({seconds_to_hms(data['total_active'])})\n")
                f.write(f"- **Total Approval/Wait Time**: {data['total_wait']:,} seconds ({seconds_to_hms(data['total_wait'])})\n")
            f.write("\n")
        
        # By Project section  
        f.write("## By Project\n\n")
        for project_key, data in by_project.items():
            project_pipeline_count = len(data['pipelines'])
            project_avg_duration = int(data['total_duration'] / data['total_runs']) if data['total_runs'] > 0 else 0
            
            f.write(f"### {data['project_name']} ({data['org_url']})\n\n")
            f.write(f"- **Pipelines**: {project_pipeline_count}\n")
            f.write(f"- **Total Runs**: {data['total_runs']}\n")
            f.write(f"- **Total Duration**: {data['total_duration']:,} seconds ({seconds_to_hms(data['total_duration'])})\n")
            if has_wait_data:
                f.write(f"- **Total Active Duration**: {data['total_active']:,} seconds ({seconds_to_hms(data['total_active'])})\n")
                f.write(f"- **Total Approval/Wait Time**: {data['total_wait']:,} seconds ({seconds_to_hms(data['total_wait'])})\n")
            f.write("\n")

def aggregate_builds_for_project(
    project_name: str,
    builds: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Aggregate builds for a single project into per-pipeline stats.
    Returns a list of dicts, one per pipeline.
    Also computes total and successful queue time (queueTime -> startTime).
    """
    results: Dict[int, Dict[str, Any]] = {}

    for b in builds:
        definition = b.get("definition") or {}
        pipeline_id = definition.get("id")
        pipeline_name = definition.get("name") or "<unknown>"
        if pipeline_id is None:
            continue

        entry = results.get(pipeline_id)
        if not entry:
            entry = {
                "project": project_name,
                "pipeline_id": pipeline_id,
                "pipeline_name": pipeline_name,
                "total_runs": 0,
                "success_runs": 0,
                "total_duration_seconds": 0.0,
                "success_duration_seconds": 0.0,
                "total_queue_seconds": 0.0,
                "success_queue_seconds": 0.0,
            }
            results[pipeline_id] = entry

        entry["total_runs"] += 1
        result = (b.get("result") or "").lower()

        duration = parse_duration_seconds(
            b.get("startTime"), b.get("finishTime")
        )
        queue_duration = parse_duration_seconds(
            b.get("queueTime"), b.get("startTime")
        )

        entry["total_duration_seconds"] += duration
        entry["total_queue_seconds"] += queue_duration
        if result == "succeeded":
            entry["success_runs"] += 1
            entry["success_duration_seconds"] += duration
            entry["success_queue_seconds"] += queue_duration

    rows: List[Dict[str, Any]] = []
    for entry in results.values():
        entry = dict(entry)
        entry["total_duration_hms"] = seconds_to_hms(
            entry["total_duration_seconds"]
        )
        entry["success_duration_hms"] = seconds_to_hms(
            entry["success_duration_seconds"]
        )
        entry["total_queue_hms"] = seconds_to_hms(
            entry.get("total_queue_seconds", 0.0)
        )
        entry["success_queue_hms"] = seconds_to_hms(
            entry.get("success_queue_seconds", 0.0)
        )
        rows.append(entry)

    return rows


def write_csv_rows(writer: csv.DictWriter, rows: List[Dict[str, Any]]) -> None:
    for row in rows:
        writer.writerow(row)

def process_project_live(
    idx: int,
    total: int,
    proj: Dict[str, Any],
    org_url: str,
    auth: Tuple[str, str],
    min_finish_time: str,
    max_finish_time: str,
    delay_ms: int,
    emit_jobs: bool,
    include_wait_time: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Process a project using live Azure DevOps API calls"""
    project_name = proj["name"]
    project_id = proj.get("id", proj.get("name", "unknown"))
    
    if VERBOSE:
        print(f"Processing project (live API) {idx}/{total}: {project_name}", file=sys.stderr)

    try:
        # Get builds from live API
        builds = get_builds_for_project(
            org_url, project_name, auth, min_finish_time, max_finish_time, delay_ms
        )
        
        # Group builds by pipeline and aggregate
        runs_by_pipeline = {}
        for build in builds:
            definition = build.get("definition") or {}
            pipeline_id = definition.get("id")
            if pipeline_id is not None:
                if pipeline_id not in runs_by_pipeline:
                    runs_by_pipeline[pipeline_id] = []
                runs_by_pipeline[pipeline_id].append(build)

        # When wait-time and/or job extraction is requested we need each
        # build's timeline. Fetch each timeline at most once and reuse it
        # for both purposes to avoid duplicate API calls.
        timelines_by_build_id: Dict[Any, Dict[str, Any]] = {}
        wait_seconds_by_build_id: Dict[Any, float] = {}
        if emit_jobs or include_wait_time:
            for build in builds:
                build_id = build.get("id")
                if build_id is None:
                    continue
                try:
                    timeline = get_build_timeline(
                        org_url, project_name, build_id, auth, delay_ms
                    )
                    timelines_by_build_id[build_id] = timeline
                    if include_wait_time:
                        wait_seconds_by_build_id[build_id] = (
                            extract_wait_seconds_from_timeline(timeline)
                        )
                except Exception as e:
                    if VERBOSE:
                        print(
                            f"Warning: Failed to get timeline for build {build_id}: {e}",
                            file=sys.stderr,
                        )

        # Convert to new aggregation format
        pipeline_rows = aggregate_pipelines_new_format(
            org_url,
            project_id,
            project_name,
            runs_by_pipeline,
            wait_seconds_by_build_id if include_wait_time else None,
        )

        # Handle jobs if requested
        job_rows = []
        if emit_jobs:
            # Ensure pools cache is populated for this org
            ensure_all_pools_loaded(org_url, auth, delay_ms)
            ensure_all_agents_loaded(org_url, auth, delay_ms)

            for build in builds:
                build_id = build.get("id")
                if not build_id:
                    continue
                timeline = timelines_by_build_id.get(build_id)
                if timeline is None:
                    # Either the timeline fetch failed in the loop above
                    # (warning was logged at that point) or the build had
                    # no id. Either way, skip job extraction for this build.
                    continue
                try:
                    build_jobs = extract_job_rows_for_build(project_name, build, timeline, org_url, auth, delay_ms)

                    # Convert to new job format
                    for job in build_jobs:
                        job_rows.append({
                            'org_url': org_url,
                            'project_id': project_id,
                            'pipeline_id': job.get('pipeline_id'),
                            'run_id': build_id,
                            'job_id': f"{build_id}_{job.get('job_name', 'unknown')}",
                            'job_name': job.get('job_name'),
                            'duration_seconds': int(job.get('job_duration_seconds', 0)),
                            'pool_id': job.get('pool_id'),
                            'pool_name': job.get('pool_name')
                        })
                except Exception as e:
                    if VERBOSE:
                        print(f"Warning: Failed to extract jobs for build {build_id}: {e}", file=sys.stderr)
                    
        return pipeline_rows, job_rows
        
    except Exception as e:
        print(f"ERROR: Failed to process project {project_name}: {e}", file=sys.stderr)
        return [], []

def process_project_mock(
    org_url: str,
    project: Dict[str, Any],
    begin_dt: dt.datetime,
    end_dt: dt.datetime,
    seed: int,
    emit_jobs: bool = False,
    include_wait_time: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """T009: Process a project using deterministic mocks with threading/delay awareness"""
    project_id = project['id']
    project_name = project['name']
    
    if VERBOSE:
        print(f"Processing project (mock): {project_name}", file=sys.stderr)
    
    # Generate mock pipelines and runs
    pipelines = generate_mock_pipelines(project_name, seed)
    
    runs_by_pipeline = {}
    all_jobs = []
    wait_seconds_by_build_id: Dict[Any, float] = {}

    for pipeline in pipelines:
        runs = generate_mock_runs(pipeline, begin_dt, end_dt, seed)
        runs_by_pipeline[pipeline['id']] = runs

        if include_wait_time:
            for run in runs:
                wait_seconds_by_build_id[run['id']] = float(
                    run.get('mock_wait_seconds', 0.0)
                )

        if emit_jobs:
            for run in runs:
                jobs = generate_mock_jobs(run, seed)
                # Convert jobs to contract format
                for job_idx, job in enumerate(jobs):
                    pool_info = resolve_mock_pool(job.get('workerName', ''), seed)
                    duration = parse_duration_seconds(job['startTime'], job['finishTime'])
                    
                    all_jobs.append({
                        'org_url': org_url,
                        'project_id': project_id,
                        'pipeline_id': pipeline['id'],
                        'run_id': run['id'],
                        'job_id': run['id'] * 100 + job_idx,
                        'job_name': job['name'],
                        'duration_seconds': int(duration),
                        'pool_id': pool_info['pool_id'],
                        'pool_name': pool_info['pool_name']
                    })
    
    # Aggregate to contract format
    pipeline_rows = aggregate_pipelines_new_format(
        org_url,
        project_id,
        project_name,
        runs_by_pipeline,
        wait_seconds_by_build_id if include_wait_time else None,
    )
    
    return pipeline_rows, all_jobs

def resolve_mock_pool(worker_name: str, seed: int) -> Dict[str, Any]:
    """Resolve mock pool info for a worker"""
    rng = random.Random(seed + hash(worker_name or 'default'))
    pool_id = rng.randint(1, 5)
    return {
        'pool_id': pool_id,
        'pool_name': f'Pool-{pool_id}'
    }

def process_project(
    idx: int,
    total: int,
    proj: Dict[str, Any],
    org_url: str,
    auth: Tuple[str, str],
    min_finish_time: str,
    max_finish_time: str,
    delay_ms: int,
    emit_jobs: bool,
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    project_name = proj["name"]
    print(f"Queueing project {idx}/{total}: {project_name}", file=sys.stderr)

    builds = get_builds_for_project(
        org_url, project_name, auth, min_finish_time, max_finish_time, delay_ms
    )
    rows = aggregate_builds_for_project(project_name, builds)

    job_rows: List[Dict[str, Any]] = []
    if emit_jobs:
        for b in builds:
            build_id = b.get("id")
            if not build_id:
                continue
            try:
                timeline = get_build_timeline(
                    org_url, project_name, build_id, auth, delay_ms
                )
            except Exception as e:
                print(
                    f"Warning: failed to fetch timeline for build {build_id} "
                    f"in project {project_name}: {e}",
                    file=sys.stderr,
                )
                continue
            job_rows.extend(
                extract_job_rows_for_build(
                    project_name,
                    b,
                    timeline,
                    org_url,
                    auth,
                    delay_ms,
                )
            )

    return project_name, rows, job_rows

def main() -> None:
    global VERBOSE
    args = parse_args()

    # Set mock mode based on --mock flag (live is now the default)
    MOCK_MODE = args.mock
    
    VERBOSE = bool(getattr(args, "verbose", False))
    if VERBOSE:
        print(f"Verbose logging enabled. Mock mode: {MOCK_MODE}", file=sys.stderr)

    # Get default PAT (used as fallback for org-specific PATs)
    default_pat = args.pat or os.getenv("AZDO_PAT")

    # Validate and parse date range
    try:
        min_finish_time = iso8601_or_die(args.begin)
        max_finish_time = iso8601_or_die(args.end)
        begin_dt = dt.datetime.fromisoformat(min_finish_time.replace('Z', '+00:00'))
        end_dt = dt.datetime.fromisoformat(max_finish_time.replace('Z', '+00:00'))
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    # Process multiple org URLs
    org_urls = []
    if args.org_url:
        for org_arg in args.org_url:
            # Support comma-separated lists in addition to repeated flags
            if ',' in org_arg:
                org_urls.extend([url.strip().rstrip('/') for url in org_arg.split(',')])
            else:
                org_urls.append(org_arg.strip().rstrip('/'))
    
    if not org_urls:
        print("ERROR: At least one --org-url must be provided.", file=sys.stderr)
        sys.exit(1)

    # Validate that each org has a resolvable PAT (unless in mock mode)
    if not MOCK_MODE:
        for org_url in org_urls:
            org_pat = get_pat_for_org(org_url, default_pat, verbose=False)
            if not org_pat:
                org_name = get_org_name(org_url)
                print(f"ERROR: No PAT found for organization '{org_name}' ({org_url}).", file=sys.stderr)
                print(f"       Set AZDO_PAT_{org_name.upper()} environment variable, or provide --pat or AZDO_PAT as fallback.", file=sys.stderr)
                sys.exit(1)

    if VERBOSE:
        print(f"Processing {len(org_urls)} organization(s): {org_urls}", file=sys.stderr)
        print(f"Date range: {args.begin} to {args.end}", file=sys.stderr)

    # Determine summary output path
    summary_path = args.summary_output
    if not summary_path and args.output:
        summary_path = args.output.replace('.csv', '.summary.md')
        if not summary_path.endswith('.summary.md'):
            summary_path = args.output + '.summary.md'

    # Collect all pipeline and job data
    all_pipeline_rows = []
    all_job_rows = []

    # Process each organization
    for org_idx, org_url in enumerate(org_urls):
        if VERBOSE:
            print(f"Processing organization {org_idx + 1}/{len(org_urls)}: {org_url}", file=sys.stderr)

        # Generate deterministic seed for this org
        seed = get_deterministic_seed(org_url, args.begin, args.end)
        
        # Get org-specific PAT
        org_pat = get_pat_for_org(org_url, default_pat, verbose=VERBOSE)
        
        # Get projects (mock or real)
        if MOCK_MODE:
            projects = generate_mock_projects(org_url, seed)
        else:
            if not REQUESTS_AVAILABLE:
                print("ERROR: requests module not available for live API mode. Install with: pip install requests", file=sys.stderr)
                sys.exit(1)
            # Use real Azure DevOps API with org-specific PAT
            auth = get_auth(org_pat)
            try:
                projects = get_projects(org_url, auth)
            except Exception as e:
                print(f"ERROR: Failed to fetch projects from {org_url}: {e}", file=sys.stderr)
                sys.exit(1)

        total_projects = len(projects)
        print(f"Found {total_projects} projects in {org_url}", file=sys.stderr)

        # Apply max-projects limit if specified
        if args.max_projects is not None and args.max_projects >= 0:
            projects = projects[:args.max_projects]
            print(f"Limited to first {len(projects)} projects due to --max-projects", file=sys.stderr)

        # Process projects (respecting --threads and --delay)
        with ThreadPoolExecutor(max_workers=args.threads) as executor:
            if VERBOSE:
                print(f"Processing {len(projects)} projects with {args.threads} threads", file=sys.stderr)
                
            futures = []
            for idx, project in enumerate(projects):
                if MOCK_MODE:
                    futures.append(
                        executor.submit(
                            process_project_mock,
                            org_url,
                            project,
                            begin_dt,
                            end_dt,
                            seed,
                            bool(args.jobs_output),
                            bool(args.include_wait_time),
                        )
                    )
                else:
                    futures.append(
                        executor.submit(
                            process_project_live,
                            idx + 1,
                            len(projects),
                            project,
                            org_url,
                            get_auth(org_pat),
                            min_finish_time,
                            max_finish_time,
                            args.delay,
                            bool(args.jobs_output),
                            bool(args.include_wait_time),
                        )
                    )
                
                # Apply delay between submissions if specified
                if args.delay > 0:
                    time.sleep(args.delay / 1000)

            # Collect results
            for future in as_completed(futures):
                try:
                    if MOCK_MODE:
                        pipeline_rows, job_rows = future.result()
                    else:
                        pipeline_rows, job_rows = future.result()
                    all_pipeline_rows.extend(pipeline_rows)
                    all_job_rows.extend(job_rows)
                except Exception as e:
                    print(f"Warning: Processing failed for a project: {e}", file=sys.stderr)

    # Write pipeline CSV output
    if args.output:
        with open(args.output, 'w', newline='', encoding='utf-8') as f:
            write_pipeline_csv(f, all_pipeline_rows)
            print(f"Pipeline CSV written to: {args.output}", file=sys.stderr)
    else:
        write_pipeline_csv(sys.stdout, all_pipeline_rows)

    # Write jobs CSV if requested
    if args.jobs_output and all_job_rows:
        with open(args.jobs_output, 'w', newline='', encoding='utf-8') as f:
            write_jobs_csv(f, all_job_rows)
            print(f"Jobs CSV written to: {args.jobs_output}", file=sys.stderr)

    # Write summary if path determined
    if summary_path and all_pipeline_rows:
        write_summary_markdown(summary_path, all_pipeline_rows)
        print(f"Summary written to: {summary_path}", file=sys.stderr)
    elif not summary_path and all_pipeline_rows:
        print("INFO: No summary output path specified. Use --summary_output to generate human-readable summary.", file=sys.stderr)

    print(f"Processing complete. {len(all_pipeline_rows)} pipeline records, {len(all_job_rows)} job records.", file=sys.stderr)

if __name__ == "__main__":
    main()