# Planner data and CLI contract

Requires Python 3.9+ on macOS or Linux; the engine uses only the standard library. It consumes a snapshot collected from Jupiter by the skill. It does not scrape Jupiter or claim an official Jupiter API.

Keep private data outside the distributable plugin, normally in `work/jupiter-state` beneath the current task directory. Commands below assume the working directory is the plugin root; replace paths with the current task's absolute paths in actual runs.

## Snapshot

```json
{
  "observed_at": "2026-09-25T16:00:00+08:00",
  "timezone": "Asia/Shanghai",
  "coverage": {
    "complete": false,
    "courses": ["Mathematics"],
    "notes": "Only Mathematics was opened in this scan."
  },
  "tasks": [
    {
      "id": "course-stable-id:assignment-stable-id",
      "course": "Mathematics",
      "title": "Problem set",
      "due_date": "2026-09-28",
      "due_at": null,
      "due_precision": "date",
      "raw_due": "Due Mon 9/28",
      "status": "open",
      "kind": "assignment",
      "source_url": "https://login.jupitered.com/",
      "estimated_minutes": null,
      "estimate_source": "provisional",
      "is_missing": false,
      "planning_disposition": "active",
      "planning_note": null
    }
  ]
}
```

- `observed_at` is the actual collection time with an explicit offset. `timezone` is the IANA timezone used to interpret school date-only deadlines, not a guess based on the computer's locale. Resolve an unknown school timezone before interpreting ambiguous dates. All precise timestamps and availability boundaries require an offset; mixed offsets are compared as absolute instants.
- Collector-chosen `id` must remain stable when titles, deadlines, or statuses change. Prefer an observed Jupiter assignment identifier plus course identifier. If unavailable, maintain a local identity map; never build the ID only from a mutable deadline or title. Duplicate IDs in one snapshot are rejected.
- `due_precision` is `time`, `date`, or `unknown`. `time` requires `due_at`, an ISO timestamp with offset. `date` requires `due_date` as `YYYY-MM-DD` and `due_at: null`. `unknown` requires both deadline fields to be null. Retain the exact displayed text in `raw_due`.
- Date-only deadlines retain the original date. A plan separately computes a **conservative finish target at the start of that date**, meaning finish before that day begins. This is not an assertion that the actual deadline is midnight or 23:59. If that conservative target has passed, the engine withholds calendar blocks and asks for deadline verification. This includes a short session requested on the listed due date. It does not claim the task is definitely overdue.
- `status` records only observed source evidence: `open`, `submitted`, `completed`, or `unknown`. `unknown` remains in the active queue. Source `submitted` and `completed` leave the active queue.
- `kind` is `assignment` or `assessment`. `estimated_minutes` is a positive integer or null; `estimate_source` is `provisional`, `user`, or `observed`. Never present an inferred duration as teacher-provided.
- Optional `is_missing` is `true` only for an explicit teacher/Jupiter missing marker, `false` only when reliably observed as not missing, and null/absent when not established. An overdue date alone does not establish missing work.
- Optional `planning_disposition` is `active` (default), `in_class`, `reference`, or `superseded`. Non-active records remain stored with their actual Jupiter status and personal progress but do not enter the queue, consume planned time, or count toward unscheduled minutes. Use `in_class` only when evidence establishes classroom work (for example classroom peer review), `reference` for informational or grade-only items, and `superseded` for an initial summary that has been replaced by individually identified tasks. Do not relabel source status as completed to exclude these records. `planning_note` is an optional string recording the actual reason and, for superseded records, the replacement IDs when available. The machine plan's `excluded` list includes `exclusion_reason` using this note; queue/full-plan Markdown lists these retained records with that reason. Current-session Markdown stays limited to session work. Omitting the field is equivalent to explicit `active`; returning it to active restores queue eligibility while retaining personal overrides.
- For active items, `planning_note` is also included in queue notes and shown in the queue or selected current-session Markdown. Use it to retain concrete task steps, preparation assumptions, and the reason for a derived preparation task; it does not change the original source status or assert that a provisional estimate is confirmed.
- Set `coverage.complete` to true only after every intended course and relevant assignment view was checked. The `courses` list records the courses actually checked; `notes` describes limits, ambiguities, and anomalies (a string or list of strings). Markdown always displays the checked course list and any notes, and phrases completeness as coverage of this scan's specified visible course list, not a claim of access to every possible course. A partial snapshot is still useful but never proves the user has no other work.

## Merge and personal progress

```sh
python3 scripts/planner.py sync --state /absolute/task/work/jupiter-state --snapshot /absolute/task/work/snapshot.json
python3 scripts/planner.py update --state /absolute/task/work/jupiter-state --id 'course-stable-id:assignment-stable-id' --estimate 60 --remaining 25
python3 scripts/planner.py update --state /absolute/task/work/jupiter-state --id 'course-stable-id:assignment-stable-id' --status completed
```

`state.json` stores `source` separately from `personal` for every ID. Sync replaces observed source fields but preserves personal estimates, remaining time, and completion. A personal completion excludes work from the queue and remains labelled “submission needs confirmation” until Jupiter confirms submission or completion. It never changes Jupiter. Reopen locally with `--status open`; this does not override a source-confirmed submission.

Tasks absent from a later snapshot are retained, never deleted or automatically marked complete. They are flagged `unverified` with their `last_seen` time and a reason distinguishing a partial scan from absence in a complete scan. A new observation clears that flag. An older snapshot is rejected to avoid rolling back fresher data.

Sync emits `added`, `deadline_changed`, `status_changed`, `missing_changed`, and `planning_disposition_changed` events. Repeating the same snapshot produces no new change events. Equivalent precise deadline instants in different offsets do not generate a deadline change. `latest_sync.events` holds only this scan's new changes; `events` preserves the history. Source title, description, planning note, and other metadata may update without a change event. A scan itself is not a reason to notify the user.

Writes are atomic. Updates use a local file lock to prevent a manual update and a scheduled sync from losing progress.

## Queue, one-time session, and confirmed availability

```sh
python3 scripts/planner.py plan --state /absolute/task/work/jupiter-state --output /absolute/task/work/queue.md
python3 scripts/planner.py plan --state /absolute/task/work/jupiter-state --minutes 20 --output /absolute/task/work/session.md
python3 scripts/planner.py plan --state /absolute/task/work/jupiter-state --availability /absolute/task/work/availability.json --output /absolute/task/work/plan.md
```

The first command creates a ranked queue (`mode: ranked_queue`) with no invented calendar windows. Earliest known planning cutoff comes first; unknown deadlines remain visible at the end. Source-confirmed submissions and locally completed tasks are excluded, with local completion awaiting submission separately disclosed. A remaining estimate of zero does not silently change completion status.

`--minutes 20` grants only one window beginning now, creates `mode: current_session`, and does not save future availability. The Markdown shows only work selected for that session plus relevant warnings; it does not display the entire future queue as today's plan. The machine JSON retains the queue and remaining unscheduled work. A session can be shorter than the requested window when known deadlines, conservative date targets, required breaks, or remaining work constrain it.

`--availability` uses explicit, dated, timezone-aware boundaries:

```json
{
  "windows": [
    {"start": "2026-09-25T16:00:00+08:00", "end": "2026-09-25T16:45:00+08:00"},
    {"start": "2026-09-26T10:00:00+08:00", "end": "2026-09-26T11:30:00+08:00"}
  ],
  "busy": [
    {"start": "2026-09-26T10:20:00+08:00", "end": "2026-09-26T10:40:00+08:00"}
  ]
}
```

`availability` is accepted as an alias for `windows`. Overlapping windows are merged, busy intervals are subtracted, and time before the current instant is removed. The scheduler splits work into blocks of at most 40 minutes with at least 5 minutes between work blocks. It never exceeds an explicit window or a known deadline/conservative date target, never overlaps work or busy time, and reports each task's `unscheduled_minutes`. Subminute gaps are not rounded up. This deterministic earliest-deadline heuristic is not a promise of an optimal schedule.

Use `--now '2026-09-25T16:00:00+08:00'` for reproducible planning. Without it the engine reads the current clock. `--minutes` and `--availability` are mutually exclusive.

### 可解释的优先级和风险

每个 `queue` 条目还会带上纯规则生成的 `priority_score`、`priority_band`、`priority_reasons`、`risk_flags`、`risk_level`、`confidence` 和 `next_action`。这些字段不会调用模型，也不会把不确定的通知变成新的截止日期：

- 截止时间在 24 小时、3 天或 7 天内，以及保守完成目标已过，会提高紧迫度；
- `marked_missing`、`completion_unknown`、`scan_unverified`、`time_unknown`、`deadline_unknown` 和 `estimate_provisional` 分别标出来源状态、覆盖范围、日期精度和用时估计的风险；
- `priority_reason` 说明本次为什么排在这里，`next_action` 给出可执行的下一步。日期未知的项目仍会显示，但默认建议先核对日期，不会因为“看起来重要”而伪造时段；
- `pending_review_count` 和 `risk_summary` 位于计划根部，分别统计尚未确认的教师通知/日期项目与各优先级数量。

因此同一份状态、同一个 `--now` 和同样的可用分钟会得到同样的优先级。用户可以用 `--minutes N` 只规划当前这一段时间；规划器不会猜测未来每天有多少空闲时间。

Each plan writes the requested Markdown file and a neighbouring `.json` file containing blocks, queue, source evidence, exclusions, coverage, and unscheduled work. If the requested Markdown filename itself ends in `.json`, the machine output instead uses `.plan.json` to avoid overwriting it.

## Configuration and tests

Optional state-directory `config.json`:

```json
{
  "default_estimates": {"assignment": 30, "assessment": 45},
  "max_block_minutes": 40,
  "break_minutes": 5
}
```

Default durations remain explicitly provisional until corrected from user input or reliable observation. Work blocks may be configured from 1–45 minutes; breaks from 1–60 minutes. Configuration supplies durations, never recurring availability.

Run meaningful regression checks from the plugin root:

```sh
python3 -m unittest discover -s tests -v
```
