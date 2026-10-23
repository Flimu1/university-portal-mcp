"""Own-account study reads and explicitly acknowledged, private local change snapshots."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from .client import AuthenticationError, PortalError
from .models import SITE, html_text, timestamp


class StudyOverview:
    def __init__(self, client, actions, *, now=time.time):
        self.client, self.actions, self.now = client, actions, now
        self._lock = threading.RLock()

    def _courses(self, course_id=None):
        courses = self.client.courses()['courses']
        if course_id is not None:
            courses = [c for c in courses if c['id'] == course_id]
            if not courses:
                raise PortalError('Course is not in this account enrolled courses.')
        return courses

    def _user(self):
        return self.client.status()['user']['id']

    def grades(self):
        courses = self._courses()
        raw = self.client.call('gradereport_overview_get_course_grades', {'userid': self._user()})
        by_id = {r['courseid']: r for r in raw.get('grades', [])}
        return {'courses': [{**c, 'score': by_id.get(c['id'], {}).get('rawgrade'),
                             'formatted_score': by_id.get(c['id'], {}).get('grade'),
                             'reported': c['id'] in by_id} for c in courses],
                'warnings': raw.get('warnings', []), 'checked_at': timestamp(self.now())}

    def _grade_data(self, course_id):
        course = self._courses(course_id)[0]
        uid = self._user()
        raw = self.client.call('gradereport_user_get_grade_items', {'courseid': course_id, 'userid': uid})
        items = []
        for user in raw.get('usergrades', []):
            if user.get('userid') != uid:
                continue
            for r in user.get('gradeitems', []):
                hidden = bool(r.get('gradeishidden') or r.get('gradehiddenbydate'))
                score = None if hidden else r.get('graderaw')
                feedback = '' if hidden else ' '.join(html_text(r.get('feedback', '')).split())
                items.append({'id': r['id'], 'module_id': r.get('cmid'), 'module_type': r.get('itemmodule'),
                    'name': html_text(r.get('itemname', '')), 'score': score, 'max_score': r.get('grademax'),
                    'formatted_score': None if hidden else r.get('gradeformatted'), 'grade_available': score is not None,
                    'hidden': hidden, 'feedback': feedback[:4000], 'feedback_truncated': len(feedback) > 4000,
                    'feedback_hash': hashlib.sha256(feedback.encode()).hexdigest(),
                    'submitted_at': timestamp(r.get('gradedatesubmitted')), 'graded_at': timestamp(r.get('gradedategraded')),
                    'submitted_timestamp': r.get('gradedatesubmitted'), 'graded_timestamp': r.get('gradedategraded'),
                    'weight': r.get('weightraw')})
        return {'course': course, 'items': items, 'warnings': raw.get('warnings', [])}

    def course_grades(self, course_id, offset=0, limit=50):
        if offset < 0 or not 1 <= limit <= 200:
            raise PortalError('Use offset >= 0 and limit 1–200.')
        result = self._grade_data(course_id)
        total = len(result['items'])
        result.update(items=result['items'][offset:offset+limit], total=total, offset=offset,
                      next_offset=offset+limit if offset+limit < total else None)
        return result

    def progress(self, course_id):
        course = self._courses(course_id)[0]
        sections, _ = self.client._outline(course_id)
        modules = {m['id']: m for s in sections for m in s.get('modules', []) if m.get('uservisible', True)}
        raw = self.client.call('core_completion_get_activities_completion_status', {'courseid': course_id, 'userid': self._user()})
        labels = {0: 'incomplete', 1: 'complete', 2: 'complete_pass', 3: 'complete_fail'}
        rows = [{'module_id': r['cmid'], 'name': modules[r['cmid']].get('name', ''),
                 'state': r.get('state'), 'state_name': labels.get(r.get('state'), 'unknown'),
                 'completed_at': timestamp(r.get('timecompleted')), 'tracking': r.get('tracking')}
                for r in raw.get('statuses', []) if r.get('cmid') in modules and r.get('tracking')]
        done = sum(r['state'] in (1, 2, 3) for r in rows)
        return {'course': course, 'tracked': len(rows), 'completed': done,
                'failed': sum(r['state'] == 3 for r in rows), 'percent': round(done/len(rows)*100, 1) if rows else None,
                'activities': rows, 'warnings': raw.get('warnings', [])}

    def calendar(self, days=30):
        if not 1 <= days <= 90:
            raise PortalError('Use days 1–90.')
        now = int(self.now()); end = now + days*86400
        courses = self._courses(); ids = {c['id'] for c in courses}; uid = self._user()
        params = {'options[timestart]': now, 'options[timeend]': end, 'options[userevents]': 1,
                  'options[siteevents]': 1, 'options[ignorehidden]': 1}
        params.update({f'events[courseids][{i}]': c['id'] for i, c in enumerate(courses)})
        raw = self.client.call('core_calendar_get_calendar_events', params)
        rows = []
        for e in raw.get('events', []):
            course_id = e.get('courseid') or (e.get('course') or {}).get('id') or 0
            starts = e.get('timestart', 0); duration = e.get('timeduration', 0)
            if not e.get('visible', True) or course_id not in ids | {0}:
                continue
            if course_id == 0 and e.get('userid', 0) not in (0, uid):
                continue
            if starts > end or starts+duration < now:
                continue
            rows.append({'id': e['id'], 'name': html_text(e.get('name', '')), 'course_id': course_id,
                         'starts_at': timestamp(starts), 'ends_at': timestamp(starts+duration),
                         'event_type': e.get('eventtype'), 'module_type': e.get('modulename'),
                         'description': html_text(e.get('description', ''))[:4000],
                         'url': SITE + '/calendar/view.php?view=day&time=' + str(starts)})
        rows.sort(key=lambda r: (r['starts_at'], r['id']))
        return {'events': rows, 'total': len(rows), 'days': days, 'timezone': 'Europe/Minsk', 'warnings': raw.get('warnings', [])}

    def announcements(self, days=30, course_id=None):
        if not 1 <= days <= 90:
            raise PortalError('Use days 1–90.')
        cutoff = self.now() - days*86400
        rows, warnings = [], []
        for course in self._courses(course_id):
            try:
                forums = self.client.call('mod_forum_get_forums_by_courses', {'courseids[0]': course['id']})
            except AuthenticationError:
                raise
            except PortalError as exc:
                warnings.append({'course_id': course['id'], 'message': str(exc)}); continue
            for forum in forums:
                if forum.get('type') != 'news':
                    continue
                try:
                    for page in range(20):
                        raw = self.client.call('mod_forum_get_forum_discussions', {'forumid': forum['id'], 'page': page, 'perpage': 50})
                        warnings.extend(raw.get('warnings', [])); discussions = raw.get('discussions', [])
                        for d in discussions:
                            changed = d.get('timemodified') or d.get('modified') or d.get('created') or 0
                            if changed < cutoff:
                                continue
                            text = html_text(d.get('message', ''))
                            did = d.get('discussion') or d.get('id')
                            if not did:
                                continue
                            rows.append({'id': did, 'forum_id': forum['id'], 'course_id': course['id'],
                                'course_name': course['name'], 'title': html_text(d.get('name') or d.get('subject', '')),
                                'text': text[:4000], 'text_truncated': len(text) > 4000,
                                'content_hash': hashlib.sha256(text.encode()).hexdigest(),
                                'modified_at': timestamp(changed), 'created_at': timestamp(d.get('created')),
                                'url': SITE + '/mod/forum/discuss.php?d=' + str(did)})
                        if len(discussions) < 50 or all((d.get('timemodified') or d.get('modified') or d.get('created') or 0) < cutoff for d in discussions):
                            break
                    else:
                        warnings.append({'forum_id': forum['id'], 'message': 'Scan capped at 1000 recent discussions; coverage is partial.'})
                except AuthenticationError:
                    raise
                except PortalError as exc:
                    warnings.append({'forum_id': forum['id'], 'message': str(exc)})
        rows = list({(r['forum_id'], r['id']): r for r in rows}.values())
        rows.sort(key=lambda r: (r['modified_at'] or '', r['id']), reverse=True)
        return {'items': rows, 'total': len(rows), 'days': days, 'warnings': warnings,
                'scope': 'News forums only; up to 1000 discussions per forum. Partial reads are reported.'}

    @contextmanager
    def _snapshot_lock(self, uid, days):
        directory = self.client.data_dir / 'study'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256(f'{SITE}:{uid}:{days}'.encode()).hexdigest()[:20]
        target = directory / f'study-{key}.json'
        with self._lock:
            handle = os.open(str(target)+'.lock', os.O_CREAT | os.O_RDWR, 0o600)
            try:
                if os.name == 'posix':
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX)
                yield target
            finally:
                os.close(handle)

    @staticmethod
    def _fingerprint(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def overview(self, days=30, update_baseline=False):
        if not 1 <= days <= 90:
            raise PortalError('Use days 1–90.')
        uid = self._user(); warnings = []; records = {}; courses = []; recent = []; coverage = set()
        def scope(key):
            return key if key.startswith('submission/') else '/'.join(key.split('/')[:2]) if key.startswith('grade/') else key.split('/')[0]
        def remember(key, kind, data, label):
            records[key] = {'kind': kind, 'hash': self._fingerprint(data), 'data': data, 'label': label}
        def attempt(operation, context):
            try:
                result = operation(); warnings.extend(result.get('warnings', [])); return result
            except AuthenticationError:
                raise
            except PortalError as exc:
                warnings.append({**context, 'message': str(exc)}); return None
        totals = attempt(self.grades, {'component': 'grades'})
        total_by_id = {c['id']: c for c in totals['courses']} if totals else {}
        for course in self._courses():
            if 'psychological' in course['name'].casefold():
                continue
            cid = course['id']; row = {**course, 'grade': total_by_id.get(cid, {}).get('score'), 'progress': None}
            progress = attempt(lambda: self.progress(cid), {'course_id': cid, 'component': 'progress'})
            if progress:
                row['progress'] = {k: progress[k] for k in ('tracked', 'completed', 'failed', 'percent')}
            grades = attempt(lambda: self._grade_data(cid), {'course_id': cid, 'component': 'grades'})
            row['published_grades'] = None if grades is None else sum(i['grade_available'] for i in grades['items'])
            if grades:
                if not grades.get('warnings'):
                    coverage.add(f'grade/{cid}')
                for i in grades['items']:
                    remember(f'grade/{cid}/{i["id"]}', 'grade', {k: i[k] for k in ('score', 'hidden', 'feedback_hash', 'graded_at')}, i['name'])
                    if i['hidden'] or not i['module_id'] or i['module_type'] not in ('assign', 'quiz'):
                        continue
                    latest = max(i.get('submitted_timestamp') or 0, i.get('graded_timestamp') or 0)
                    if latest < self.now() - days*86400:
                        continue
                    recent.append({'course_id': cid, 'course_name': course['name'], **i, '_latest': latest})
            courses.append(row)
        recent.sort(key=lambda r: r['_latest'], reverse=True)
        recent_limit = len(recent) > 30
        recent = recent[:30]
        for item in recent:
            cid, mid = item['course_id'], item['module_id']
            status = attempt(lambda: self.actions.submission_status(cid, mid) if item['module_type'] == 'assign' else self.actions.quiz_status(cid, mid), {'course_id': cid, 'module_id': mid, 'component': 'submission'})
            if status is None:
                item['submission_status'] = 'unknown'
            elif item['module_type'] == 'assign':
                item['submission_status'] = status['status']
            else:
                attempts = status['attempts']
                item['submission_status'] = max(attempts, key=lambda a: a.get('attempt', 0))['state'] if attempts else 'new'
            final = item['submission_status'] in ('submitted', 'finished')
            newer = (item.get('submitted_timestamp') or 0) > (item.get('graded_timestamp') or 0)
            item['assessment_state'] = ('awaiting_grade' if final and (not item['grade_available'] or newer) else
                                        'graded' if item['grade_available'] and item.get('graded_timestamp') else
                                        'grade_available' if item['grade_available'] else 'no_published_grade')
            item.pop('_latest', None)
            if status is not None:
                if not status.get('warnings'):
                    coverage.add(f'submission/{cid}/{mid}')
                remember(f'submission/{cid}/{mid}', 'submission', item['submission_status'], item['name'])
        deadlines = attempt(lambda: self.actions.deadlines(days), {'component': 'deadlines'})
        news = attempt(lambda: self.announcements(days), {'component': 'announcements'})
        calendar = attempt(lambda: self.calendar(days), {'component': 'calendar'})
        for key, result in (('news', news), ('deadline', deadlines), ('calendar', calendar)):
            if result is not None and not result.get('warnings'):
                coverage.add(key)
        for n in (news or {}).get('items', []):
            remember(f'news/{n["forum_id"]}/{n["id"]}', 'announcement', {k: n[k] for k in ('title', 'content_hash', 'modified_at')}, n['title'])
        for d in (deadlines or {}).get('items', []):
            remember(f'deadline/{d["course_id"]}/{d["module_id"]}', 'deadline', d['due_at'], d['name'])
        for e in (calendar or {}).get('events', []):
            remember(f'calendar/{e["id"]}', 'calendar', {k: e[k] for k in ('name', 'starts_at', 'ends_at')}, e['name'])
        changes = []
        with self._snapshot_lock(uid, days) as target:
            previous = None
            try:
                if target.stat().st_size <= 8*1024*1024:
                    previous = json.loads(target.read_text())
                    if previous.get('user_id') != uid or not isinstance(previous.get('records'), dict):
                        previous = None
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError, AttributeError):
                warnings.append({'component': 'baseline', 'message': 'Previous local baseline was unreadable; no historical alerts are emitted.'})
            if previous:
                prior_coverage = set(previous.get('coverage', []))
                # Older snapshots without coverage can establish only observed sources.
                if 'coverage' not in previous:
                    prior_coverage.update(scope(key) for key in previous['records'])
                for key, rec in records.items():
                    old = previous['records'].get(key)
                    if old is None and scope(key) not in prior_coverage:
                        continue
                    if old and old.get('hash') == rec['hash']:
                        continue
                    kind = rec['kind']
                    if kind == 'grade':
                        if rec['data']['score'] is None:
                            if not old or old['data'].get('score') is None:
                                continue
                        kind = 'grade_published' if rec['data']['score'] is not None and (not old or old['data'].get('score') is None) else 'grade_updated'
                    else:
                        kind += '_updated' if old else '_added'
                    changes.append({'key': key, 'kind': kind, 'name': rec['label'], 'before': old.get('data') if old else None, 'after': rec['data']})
            if update_baseline:
                merged = {**(previous or {}).get('records', {}), **records}
                prior = set((previous or {}).get('coverage', []))
                if previous and 'coverage' not in previous:
                    prior.update(scope(key) for key in previous['records'])
                body = {'user_id': uid, 'site': SITE, 'checked_at': timestamp(self.now()),
                        'records': merged, 'coverage': sorted(prior | coverage)}
                fd, tmp = tempfile.mkstemp(prefix='.study-', dir=target.parent)
                try:
                    with os.fdopen(fd, 'w') as handle:
                        json.dump(body, handle, ensure_ascii=False)
                    os.replace(tmp, target)
                finally:
                    if Path(tmp).exists(): Path(tmp).unlink()
        return {'checked_at': timestamp(self.now()), 'timezone': 'Europe/Minsk', 'courses': courses,
                'recent_work': recent, 'recent_work_truncated': recent_limit,
                'deadlines': deadlines, 'announcements': news, 'calendar': calendar, 'changes': changes,
                'baseline_initialized': bool(previous) or update_baseline, 'baseline_updated': update_baseline,
                'warnings': warnings, 'source': 'Moodle; official grades/attendance in other university systems may differ.'}
