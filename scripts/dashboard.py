#!/usr/bin/env python3
"""Generate an offline, read-only dashboard. Never starts a collector or server."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.parse import quote

TASK_FIELDS = ('id', 'title', 'course', 'due_precision', 'due_date', 'due_at',
               'remaining_minutes', 'effective_estimate_source', 'planning_disposition',
               'planning_note', 'notes', 'is_missing', 'source_status', 'personal_status',
               'due_label', 'priority_score', 'priority_band', 'priority_reason',
               'priority_reasons', 'risk_level', 'risk_flags', 'confidence', 'next_action',
               'scheduled_minutes', 'unscheduled_minutes', 'unscheduled_reason')


def _alerts(plan, notifications, now=None):
    """Build deterministic, actionable alerts without interpreting teacher prose."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    alerts = []
    for task in plan.get('queue', []):
        if task.get('planning_disposition', 'active') != 'active':
            continue
        due = task.get('due_date')
        if task.get('due_precision') == 'time' and task.get('due_at'):
            try:
                due_instant = datetime.fromisoformat(task['due_at'].replace('Z', '+00:00'))
                due = due_instant.astimezone(timezone.utc).date()
            except (TypeError, ValueError):
                due = None
        try:
            day = datetime.strptime(due, '%Y-%m-%d').date() if due else None
        except (TypeError, ValueError):
            day = None
        if day and day < today:
            alerts.append({'kind': 'overdue', 'severity': 3, 'task_id': task.get('id'),
                           'title': '已过日期，提交状态仍需确认', 'course': task.get('course', ''),
                           'task': task.get('title', ''), 'due': due,
                           'action': '先确认 Jupiter 是否已提交；若未提交，优先处理。'})
        elif day and day <= today + timedelta(days=2):
            alerts.append({'kind': 'soon', 'severity': 2, 'task_id': task.get('id'),
                           'title': '很快截止', 'course': task.get('course', ''),
                           'task': task.get('title', ''), 'due': due,
                           'action': '建议今天先完成最小可提交版本。'})
        if task.get('source_status') == 'unknown' and day and day <= today + timedelta(days=2):
            alerts.append({'kind': 'status', 'severity': 2, 'task_id': task.get('id'),
                           'title': '完成状态未知', 'course': task.get('course', ''),
                           'task': task.get('title', ''), 'due': due,
                           'action': 'Jupiter 没有明确提交证据，不要把它当成已完成。'})
    for item in (notifications or [])[-3:]:
        if item.get('type') == 'update' and item.get('changes', {}).get('added', 0):
            alerts.append({'kind': 'new', 'severity': 1, 'task_id': '',
                           'title': '发现新作业', 'course': '', 'task': item.get('message', ''),
                           'due': '', 'action': '先查看“优先任务”中的新增项目。'})
        elif item.get('type') == 'deadline':
            alerts.append({'kind': 'deadline', 'severity': 3, 'task_id': item.get('task_id', ''),
                           'title': item.get('title', '作业即将截止'),
                           'course': item.get('course', ''), 'task': item.get('task', item.get('message', '')),
                           'due': item.get('due', ''),
                           'action': f"距截止约 {item.get('remaining_minutes', '?')} 分钟，建议现在开始。"})
    pending_reviews = plan.get('pending_review_count', 0)
    if pending_reviews:
        alerts.append({'kind': 'notice', 'severity': 1, 'task_id': '',
                       'title': '有老师通知待处理', 'course': '',
                       'task': f'{pending_reviews} 条通知未标记为已读', 'due': '',
                       'action': '打开“老师通知”，读完后点勾。'})
    unique = {}
    for alert in alerts:
        key = (alert.get('kind'), alert.get('task_id'), alert.get('task'))
        unique[key] = alert
    return sorted(unique.values(), key=lambda x: (-x['severity'], x.get('due', ''), x.get('course', ''), x.get('task', '')))


def safe_payload(plan, status, config, notifications=None):
    """Only presentation fields, never account identifiers, profiles or cookies."""
    def tasks(items):
        return [{key: t.get(key) for key in TASK_FIELDS if key in t} for t in items]
    reviews = []
    for item in plan.get('pending_review', []):
        notice = item.get('notice') or {}
        reviews.append({'id': str(item.get('id') or notice.get('id') or ''),
                        'message': item.get('message', ''), 'title': notice.get('title', ''),
                        'text': notice.get('text', ''), 'author': notice.get('author', ''),
                        'date_text': notice.get('date_text', ''),
                        'unread': notice.get('unread') if 'unread' in notice else None})
    coverage = (plan.get('latest_sync') or {}).get('coverage', {})
    completed = [item for item in plan.get('excluded', [])
                 if item.get('planning_disposition') == 'active' and item.get('personal_status') == 'completed']
    return {'timezone': plan.get('timezone', config.get('school_timezone', 'UTC')),
            'generated_at': plan.get('generated_at'), 'queue': tasks(plan.get('queue', [])),
            'excluded': tasks(plan.get('excluded', [])), 'completed': tasks(completed), 'reviews': reviews,
            'pending_review_count': plan.get('pending_review_count', len(reviews)),
            'risk_summary': plan.get('risk_summary', {}),
            'alerts': _alerts(plan, notifications or []),
            'blocks': [{k: b.get(k) for k in ('title', 'course', 'start', 'end', 'minutes')}
                       for b in plan.get('blocks', [])],
            'status': {k: status.get(k) for k in ('status', 'last_attempt', 'last_success', 'last_observed_at', 'coverage_complete')},
            'schedule_configured': config.get('schedule_enabled') is True,
            'schedule_hours': config.get('schedule_hours', []),
            'deadline_reminders_enabled': config.get('deadline_reminders_enabled', True) is not False,
            'deadline_reminder_minutes': config.get('deadline_reminder_minutes', [1440, 120, 30]),
            'ticktick_enabled': config.get('ticktick_enabled', False) is True or
                               isinstance(config.get('ticktick'), dict) and config.get('ticktick', {}).get('enabled') is True,
            'ticktick_project': config.get('ticktick_project_name') or
                               (config.get('ticktick') or {}).get('project_name') or '未设置',
            'course_count': len(coverage.get('courses', [])) or len({t.get('course') for t in plan.get('queue', [])})}


def render_dashboard(plan, status=None, config=None, report_name='study-plan.md', notifications=None):
    data = safe_payload(plan or {}, status or {}, config or {}, notifications)
    # Escape HTML-sensitive characters before embedding JSON in a script tag.
    payload = json.dumps(data, ensure_ascii=False).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    template = (Path(__file__).resolve().parent.parent / 'assets' / 'dashboard.html').read_text(encoding='utf-8')
    report = Path(report_name)
    for marker, value in {'__ICS_LINK__': quote(report.with_suffix('.ics').name),
                          '__REPORT_LINK__': quote(report.name)}.items():
        template = template.replace(marker, value)
    return template.replace('__DASHBOARD_DATA__', payload)


def refresh(instance_path):
    from jupiter_runtime import instance_config, read, commit_files
    location, directory, report, plan_path = instance_config(instance_path)
    plan = read(plan_path, {})
    page = report.with_suffix('.html')
    commit_files({page: render_dashboard(plan, read(directory / 'run-status.json', {}), read(location, {}), report.name,
                                          read(directory / 'notifications.json', []))})
    return page


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', required=True, type=Path)
    args = parser.parse_args()
    print(refresh(args.instance))


if __name__ == '__main__':
    main()
