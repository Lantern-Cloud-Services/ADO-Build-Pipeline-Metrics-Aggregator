# Azure DevOps Pipeline Aggregation Tool

A Python CLI tool that aggregates Azure DevOps pipeline (build) duration metrics across multiple organizations and projects using the Azure DevOps REST API.

## Features

- **Multi-organization support**: Process multiple Azure DevOps organizations in a single run
- **Pipeline aggregation**: Generate per-pipeline statistics including run counts and duration metrics
- **Wait/approval time differentiation** (opt-in): Break wall-clock duration into active execution time vs. wait/approval time so you can spot pipelines bottlenecked by manual approvals or idle gaps
- **Job-level details**: Optional detailed job information with agent pool assignments
- **Human-readable summaries**: Markdown reports with organization and project breakdowns
- **CSV output**: Machine-readable data following documented schemas
- **Parallel processing**: Configurable threading for improved performance

## Prerequisites

- **Python**: 3.10 or higher
- **Platform**: Windows PowerShell 5.1 (cross-platform compatible)
- **Personal Access Token**: Azure DevOps PAT with appropriate permissions
- **Dependencies**: `requests` module (`pip install requests`)

## Installation

1. Clone or download this repository
2. Install dependencies: `pip install requests`
3. Set `AZDO_PAT` environment variable or use `--pat` flag

## Quick Start

### Basic Usage - Single Organization

```powershell
# Generate pipeline aggregates with summary
python get-build-durations.py --org-url https://dev.azure.com/myorg --begin 2024-01-01 --end 2024-01-31 --output pipelines.csv

# This creates:
# - pipelines.csv (pipeline aggregation data)
# - pipelines.summary.md (human-readable summary)
```

### Multi-Organization with Job Details

```powershell
# Process multiple organizations with detailed job information
python get-build-durations.py `
  --org-url https://dev.azure.com/org1 `
  --org-url https://dev.azure.com/org2 `
  --begin 2024-01-01T00:00:00 `
  --end 2024-02-01T00:00:00 `
  --output pipelines.csv `
  --jobs_output jobs.csv `
  --summary_output consolidated_summary.md `
  --threads 8 `
  --verbose
```

### Output to stdout

```powershell
# Write CSV to stdout, specify summary separately
python get-build-durations.py --org-url https://dev.azure.com/myorg --begin 2024-01-01 --end 2024-01-31 --summary_output summary.md > output.csv
```

## Command Line Arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `--org-url` | ✓ | Azure DevOps organization URL (can specify multiple times) |
| `--begin` | ✓ | Start date (inclusive): `2024-01-01` or `2024-01-01T00:00:00` |
| `--end` | ✓ | End date (exclusive): `2024-01-31` or `2024-01-31T00:00:00` |
| `--pat` | | Personal Access Token (or set `AZDO_PAT` env var) |
| `--output` | | Output CSV file path (defaults to stdout) |
| `--jobs_output` | | Optional jobs CSV file path |
| `--summary_output` | | Summary markdown file path (auto-generated if `--output` provided) |
| `--threads` | | Number of worker threads (default: 4) |
| `--max-projects` | | Limit number of projects processed (for testing) |
| `--delay` | | Delay in ms between requests (default: 0) |
| `--verbose` | | Enable verbose logging to stderr |
| `--mock` | | Use deterministic mock data instead of live API (for testing) |
| `--include-wait-breakdown` | | Fetch build timelines to differentiate wait/approval time from active execution time. Appends 7 columns to the pipeline CSV and adds a "Wait Time Analysis" section to the summary. Reuses timelines already fetched for `--jobs_output` (no extra API calls when both are set). |

## Output Files

### Pipeline CSV (`pipelines.csv`)
Main aggregation file with one row per pipeline:

| Column | Type | Description |
|--------|------|-------------|
| `org_url` | string | Organization URL |
| `project_id` | string | Project identifier |
| `project_name` | string | Project display name |
| `pipeline_id` | integer | Pipeline identifier |
| `pipeline_name` | string | Pipeline display name |
| `run_count` | integer | Number of runs in date range |
| `avg_duration_seconds` | integer | Average run duration (wall-clock, includes wait/approval time) |
| `total_duration_seconds` | integer | Sum of all run durations (wall-clock, includes wait/approval time) |

#### Wait/Approval Time Breakdown (opt-in)

When `--include-wait-breakdown` is specified, 7 additional columns are appended (in this order) to differentiate wait/approval time from active execution time:

| Column | Type | Description |
|--------|------|-------------|
| `wait_analyzed_run_count` | integer | Number of runs whose timeline was successfully analyzed (≤ `run_count`). Honestly reflects partial coverage when timeline fetches fail. |
| `avg_active_seconds` | integer | Average active execution time per analyzed run (sum of Job-record intervals, with overlapping parallel jobs unioned, not double-counted). |
| `total_active_seconds` | integer | Sum of active execution time across analyzed runs. |
| `avg_wait_seconds` | integer | Average wait/idle time per analyzed run. Includes manual approval gates, environment checks, and any other gaps where no Job was running. |
| `total_wait_seconds` | integer | Sum of wait/idle time across analyzed runs. |
| `avg_approval_seconds` | integer | Average time per analyzed run spent specifically in approval/checkpoint records (a *subset* of wait time). |
| `total_approval_seconds` | integer | Sum of approval time across analyzed runs. |

Identity per analyzed run: `active_seconds + wait_seconds == total_duration_seconds` (modulo integer rounding) and `approval_seconds <= wait_seconds <= total_duration_seconds`.

The base 8-column schema is unchanged when `--include-wait-breakdown` is not set, so existing CSV consumers continue to work without modification.

### Jobs CSV (`jobs.csv`) - Optional
Detailed job information when `--jobs_output` specified:

| Column | Type | Description |
|--------|------|-------------|
| `org_url` | string | Organization URL |
| `project_id` | string | Project identifier |
| `pipeline_id` | integer | Pipeline identifier |
| `run_id` | integer | Pipeline run identifier |
| `job_id` | integer | Job identifier |
| `job_name` | string | Job display name |
| `duration_seconds` | integer | Job duration |
| `pool_id` | integer | Agent pool identifier |
| `pool_name` | string | Agent pool name |

### Summary Markdown (`summary.md`)
Human-readable report including:
- **Overview**: Total organizations, projects, pipelines, runs
- **By Organization**: Aggregates per organization
- **By Project**: Aggregates per project within each organization
- **Wait Time Analysis** (only when `--include-wait-breakdown` is set and at least one run was analyzed): Per-section wait/approval bullets plus a "Top Pipelines by Wait %" table sorted descending by `total_wait_seconds / total_duration_seconds`, surfacing pipelines most bottlenecked by approvals or idle time.

## Authentication

The tool requires an Azure DevOps Personal Access Token (PAT) with the following permissions:
- **Build**: Read
- **Project and team**: Read
- **Agent Pools**: Read (if using `--jobs_output`)

### Single Organization

For a single organization, use either an environment variable or command line flag:

```powershell
# Option 1: Environment variable (recommended)
$env:AZDO_PAT = "your_pat_token_here"
python get-build-durations.py --org-url https://dev.azure.com/myorg --begin 2024-01-01 --end 2024-01-31

# Option 2: Command line flag
python get-build-durations.py --pat "your_pat_token_here" --org-url https://dev.azure.com/myorg --begin 2024-01-01 --end 2024-01-31
```

### Multiple Organizations with Different PATs

Azure DevOps PATs are organization-specific. When querying multiple organizations, you can use organization-specific environment variables:

```powershell
# Set org-specific PATs using AZDO_PAT_<ORGNAME> pattern
$env:AZDO_PAT_CONTOSO = "pat_for_contoso_org"
$env:AZDO_PAT_FABRIKAM = "pat_for_fabrikam_org"

# Run against multiple orgs - each will use its org-specific PAT
python get-build-durations.py `
  --org-url https://dev.azure.com/contoso `
  --org-url https://dev.azure.com/fabrikam `
  --begin 2024-01-01 --end 2024-01-31 `
  --output multi_org_pipelines.csv
```

**PAT Resolution Order** (per organization):
1. `AZDO_PAT_<ORGNAME>` - Organization-specific environment variable (e.g., `AZDO_PAT_CONTOSO`)
2. `AZDO_PAT` - Default fallback environment variable
3. `--pat` - Command line argument (used as fallback for all orgs)

**Note**: The organization name is extracted from the URL and converted to uppercase for the environment variable lookup. For `https://dev.azure.com/myorg`, the script looks for `AZDO_PAT_MYORG`.

## Examples

### Example 1: Monthly Report for Single Organization
```powershell
$env:AZDO_PAT = "your_pat_token"
python get-build-durations.py `
  --org-url https://dev.azure.com/contoso `
  --begin 2024-01-01 `
  --end 2024-02-01 `
  --output january_pipelines.csv `
  --verbose
```

### Example 2: Multi-Organization with Org-Specific PATs
```powershell
# Set organization-specific PATs
$env:AZDO_PAT_CONTOSO = "pat_for_contoso"
$env:AZDO_PAT_FABRIKAM = "pat_for_fabrikam"
$env:AZDO_PAT_NORTHWIND = "pat_for_northwind"

python get-build-durations.py `
  --org-url https://dev.azure.com/contoso `
  --org-url https://dev.azure.com/fabrikam `
  --org-url https://dev.azure.com/northwind `
  --begin 2024-01-01 `
  --end 2024-04-01 `
  --output q1_pipelines.csv `
  --jobs_output q1_jobs.csv `
  --summary_output q1_report.md `
  --threads 6
```

### Example 3: Rate-Limited Processing
```powershell
# Add delays between API requests to avoid rate limiting
python get-build-durations.py `
  --org-url https://dev.azure.com/testorg `
  --begin 2024-01-01 `
  --end 2024-01-31 `
  --output pipelines.csv `
  --max-projects 5 `
  --delay 100 `
  --verbose
```

### Example 4: Wait/Approval Time Breakdown
```powershell
# Differentiate wall-clock time into active execution vs. wait/approval time.
# Adds 7 columns to the CSV and a "Wait Time Analysis" / "Top Pipelines by Wait %"
# section to the summary so you can spot pipelines bottlenecked by manual approvals.
$env:AZDO_PAT = "your_pat_token"
python get-build-durations.py `
  --org-url https://dev.azure.com/contoso `
  --begin 2024-01-01 `
  --end 2024-02-01 `
  --output pipelines.csv `
  --include-wait-breakdown
```

> Note: `--include-wait-breakdown` fetches the timeline for every build (1 extra
> API call per build). When `--jobs_output` is also set, the same timeline is
> reused, so combining the two flags adds no extra API calls beyond what
> `--jobs_output` already requires.

## Error Handling

The tool provides clear error messages and exits with appropriate codes:

- **Exit Code 0**: Success
- **Exit Code 1**: Error (missing PAT, invalid dates, invalid arguments)

Common errors:
```powershell
# Missing PAT
ERROR: Provide PAT via --pat or AZDO_PAT env var.

# Invalid date format
ERROR: Could not parse datetime 'invalid-date'. Supported formats: %Y-%m-%d, %Y-%m-%dT%H:%M, %Y-%m-%dT%H:%M:%S

# Missing required argument
get-build-durations.py: error: the following arguments are required: --org-url
```

## Performance

- **Threading**: Configurable parallel processing (default: 4 threads)
- **Memory**: Streaming CSV writes for large datasets
- **Delays**: Configurable request delays for rate limiting

## Technical Details

- **Language**: Python 3.10+
- **Dependencies**: `requests` module for Azure DevOps API calls
- **Architecture**: Single-file CLI script
- **Output**: CSV and Markdown generation

## Project Structure

```
get-build-durations/
├── get-build-durations.py     # Main CLI script
├── README.md                  # This file
├── specs/                     # Feature documentation
│   └── 001-aggregate-ado-pipelines/
│       ├── spec.md            # Feature specification
│       ├── plan.md            # Implementation plan
│       ├── tasks.md           # Task breakdown
│       ├── quickstart.md      # Quick reference
│       └── contracts/         # Output schemas
├── test/                      # Test data and mock mode documentation
│   └── README.md              # Testing guide with mock data
└── .gitignore                 # Git ignore patterns
```

## Testing

The script supports a mock mode for testing without Azure DevOps access. See `test/README.md` for detailed instructions on running tests with mock data.

## Contributing

This tool follows a specification-driven development approach. See `specs/001-aggregate-ado-pipelines/` for detailed feature documentation, implementation plans, and task tracking.

## License

This project is provided as-is for demonstration and testing purposes.
