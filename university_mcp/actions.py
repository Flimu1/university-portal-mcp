"""Explicitly authorized own-account coursework actions and deadline discovery."""
from pathlib import Path
import re
import time

from bs4 import BeautifulSoup

from .client import MoodleClient, PortalError, AuthenticationError
from .files import MaterialStore, MAX_DOWNLOAD_BYTES
from .models import SITE, file_metadata, html_text, timestamp


class MoodleActions:
    def __init__(self, client: MoodleClient, submission_dir: Path, *, now=time.time):
        self.client = client
        self.submission_dir = Path(submission_dir).resolve()
        self.submission_dir.mkdir(parents=True, exist_ok=True)
        self.now = now

    def _assignment(self, course_id, module_id):
        module, _ = self.client._module(course_id, module_id)
        if module.get('modname') != 'assign' or not module.get('uservisible', True):
            raise PortalError('Target is not an accessible assignment in this enrolled course.')
        rows, _ = self.client._assignments(course_id)
        assignment = next((a for a in rows if a['cmid'] == module_id), None)
        if not assignment:
            raise PortalError('Assignment requirements are not accessible through this account.')
        return assignment

    def submission_status(self, course_id, module_id):
        assignment = self._assignment(course_id, module_id)
        raw = self.client.call('mod_assign_get_submission_status', {'assignid': assignment['id']})
        last = raw.get('lastattempt', {})
        submission = last.get('submission') or last.get('teamsubmission') or {}
        files, online_text = [], ''
        for plugin in submission.get('plugins', []):
            for area in plugin.get('fileareas', []):
                files.extend(area.get('files', []))
            for field in plugin.get('editorfields', []):
                online_text += field.get('text', '')
        return {'course_id': course_id, 'module_id': module_id, 'assignment_id': assignment['id'],
                'name': assignment['name'], 'status': submission.get('status', 'new'),
                'can_edit': bool(last.get('canedit')), 'locked': bool(last.get('locked')),
                'files': files, 'online_text': online_text, 'due_at': timestamp(assignment.get('duedate')),
                'cutoff_at': timestamp(assignment.get('cutoffdate')), 'submissiondrafts': bool(assignment.get('submissiondrafts')),
                'requires_submission_statement': bool(assignment.get('requiresubmissionstatement')),
                'warnings': raw.get('warnings', [])}

    def _quiz(self, course_id, module_id):
        module, _ = self.client._module(course_id, module_id)
        if module.get('modname') != 'quiz' or not module.get('uservisible', True):
            raise PortalError('Target is not an accessible quiz in this enrolled course.')
        raw, _ = self.client._get('mod_quiz_get_quizzes_by_courses', {'courseids[0]': course_id})
        quiz = next((q for q in raw.get('quizzes', []) if q.get('coursemodule') == module_id), None)
        if quiz is None:
            raise PortalError('Quiz is not accessible to this account.')
        return quiz

    def quiz_status(self, course_id, module_id):
        quiz = self._quiz(course_id, module_id)
        attempts = self.client.call('mod_quiz_get_user_attempts', {'quizid': quiz['id'], 'status': 'all'})
        access = self.client.call('mod_quiz_get_quiz_access_information', {'quizid': quiz['id']})
        return {'course_id': course_id, 'module_id': module_id, 'quiz_id': quiz['id'], 'name': quiz['name'],
                'opens_at': timestamp(quiz.get('timeopen')), 'due_at': timestamp(quiz.get('timeclose')),
                'time_limit_seconds': quiz.get('timelimit', 0), 'attempts': attempts.get('attempts', []),
                'can_attempt': bool(access.get('canattempt')), 'prevent_access_reasons': access.get('preventaccessreasons', []),
                'rules': access.get('accessrules', []), 'warnings': attempts.get('warnings', []) + access.get('warnings', [])}

    def deadlines(self, days=30, overdue_days=0, include_completed=False):
        if not 1 <= days <= 90 or not 0 <= overdue_days <= 30:
            raise PortalError('Use days 1–90 and overdue_days 0–30.')
        now = self.now()
        items, warnings = [], []
        for course in self.client.courses()['courses']:
            if 'psychological' in course['name'].casefold():
                continue
            try:
                rows, source_warnings = self.client._assignments(course['id'])
                warnings.extend({'course_id': course['id'], 'type': 'assignment', **w} for w in source_warnings)
            except AuthenticationError:
                raise
            except PortalError as exc:
                warnings.append({'course_id': course['id'], 'type': 'assignment', 'message': str(exc)})
                rows = []
            try:
                quizzes, _ = self.client._get('mod_quiz_get_quizzes_by_courses', {'courseids[0]': course['id']})
                warnings.extend({'course_id': course['id'], 'type': 'quiz', **w} for w in quizzes.get('warnings', []))
            except AuthenticationError:
                raise
            except PortalError as exc:
                warnings.append({'course_id': course['id'], 'type': 'quiz', 'message': str(exc)})
                quizzes = {}
            for kind, rows_of_kind in [('assignment', rows), ('quiz', quizzes.get('quizzes', []))]:
                for row in rows_of_kind:
                    due = row.get('duedate' if kind == 'assignment' else 'timeclose', 0)
                    if not now - overdue_days * 86400 <= due <= now + days * 86400:
                        continue
                    mid = row['cmid' if kind == 'assignment' else 'coursemodule']
                    try:
                        status = self.submission_status(course['id'], mid) if kind == 'assignment' else self.quiz_status(course['id'], mid)
                        warnings.extend({'course_id': course['id'], 'module_id': mid, **w} for w in status.get('warnings', []))
                    except AuthenticationError:
                        raise
                    except PortalError as exc:
                        warnings.append({'course_id': course['id'], 'module_id': mid, 'message': str(exc)})
                        continue
                    complete = status['status'] == 'submitted' if kind == 'assignment' else any(a['state'] == 'finished' for a in status['attempts'])
                    if complete and not include_completed:
                        continue
                    closed = (kind == 'quiz' and due <= now) or (kind == 'assignment' and 0 < row.get('cutoffdate', 0) <= now)
                    items.append({'course_id': course['id'], 'course_name': course['name'], 'module_id': mid,
                                  'type': kind, 'name': row['name'], 'due_at': timestamp(due), 'seconds_until_due': int(due - now),
                                  'closed': closed, 'completed': complete, 'status': status})
        items.sort(key=lambda i: i['seconds_until_due'])
        return {'items': items, 'total': len(items), 'timezone': 'Europe/Minsk', 'warnings': warnings,
                'submission_directory': str(self.submission_dir), 'checked_at': timestamp(now)}

    def _validated_path(self, name):
        path = Path(name).expanduser().resolve()
        if not path.is_relative_to(self.submission_dir) or not path.is_file():
            raise PortalError('Only generated submission files inside the configured outputs/submissions directory can be uploaded.')
        if path.suffix.lower() not in {'.pdf', '.docx', '.xlsx', '.pptx', '.txt', '.csv', '.zip'}:
            raise PortalError('This file type is not allowed as a generated coursework submission.')
        if not 0 < path.stat().st_size <= MAX_DOWNLOAD_BYTES:
            raise PortalError('Submission file is empty or exceeds 128 MiB.')
        return path

    def _upload(self, path, item_id=0, filename=None):
        try:
            with Path(path).open('rb') as handle:
                response = self.client.http.post(SITE + '/webservice/upload.php',
                    data={'token': self.client._token, 'itemid': str(item_id), 'filepath': '/'},
                    files={'file_1': (filename or Path(path).name, handle, 'application/octet-stream')})
            result = response.json()
        except Exception as exc:
            raise PortalError(f'Coursework file upload failed ({type(exc).__name__}); no submission is claimed.') from None
        if response.status_code != 200 or not isinstance(result, list) or not result or not result[0].get('itemid'):
            raise PortalError('Moodle did not confirm upload to the private draft file area.')
        return int(result[0]['itemid'])

    def save_assignment(self, course_id, module_id, file_paths, online_text=None):
        paths = [self._validated_path(name) for name in file_paths]
        if not paths and not online_text:
            raise PortalError('Provide generated files or online text before saving a submission.')
        before = self.submission_status(course_id, module_id)
        if before['status'] == 'submitted' or before['locked'] or not before['can_edit']:
            raise PortalError('Assignment is submitted, locked or not editable; it will not be overwritten.')
        data = {'assignmentid': before['assignment_id']}
        draft = 0
        # Copy existing own draft files so adding a generated answer cannot silently delete them.
        if paths:
            new_names = {p.name for p in paths}
            existing_store = MaterialStore(self.client, self.client.data_dir / 'own-draft-files')
            for raw in before['files']:
                if raw.get('filename') in new_names:
                    continue
                meta = file_metadata(raw, course_id, module_id, 'own_submission')
                if meta:
                    old = existing_store.download(meta)
                    draft = self._upload(Path(old['local_path']), draft, raw['filename'])
            for path in paths:
                draft = self._upload(path, draft)
            data['plugindata[files_filemanager]'] = draft
        text = online_text if online_text is not None else before['online_text']
        if text:
            data.update({'plugindata[onlinetext_editor][text]': text,
                         'plugindata[onlinetext_editor][format]': 1, 'plugindata[onlinetext_editor][itemid]': 0})
        warnings = self.client._request_api('mod_assign_save_submission', data)
        after = self.submission_status(course_id, module_id)
        if warnings or after['status'] not in {'draft', 'submitted'}:
            raise PortalError('Save was not verified as draft/submitted. Inspect get_submission_status before retrying.')
        expected_names = {p.name for p in paths} | {f['filename'] for f in before['files']}
        if not expected_names.issubset({f['filename'] for f in after['files']}):
            raise PortalError('Saved submission did not contain all expected attachments. Inspect get_submission_status before retrying.')
        if text and ' '.join(html_text(after['online_text']).split()) != ' '.join(html_text(text).split()):
            raise PortalError('Requested online answer text was not verified in the saved submission.')
        return {'saved': True, 'verified_submitted': after['status'] == 'submitted', 'submission': after}

    def submit_assignment(self, course_id, module_id, accept_submission_statement=False):
        before = self.submission_status(course_id, module_id)
        if before['status'] == 'submitted':
            return {'verified_submitted': True, 'already_submitted': True, 'submission': before}
        if before['status'] != 'draft' or before['locked']:
            raise PortalError('Only an existing unlocked draft can be submitted for grading.')
        if before['requires_submission_statement'] and not accept_submission_statement:
            raise PortalError('Moodle requires an explicit submission statement. Read its terms before accepting.')
        self.client._request_api('mod_assign_submit_for_grading', {'assignmentid': before['assignment_id'],
                                  'acceptsubmissionstatement': int(accept_submission_statement)})
        after = self.submission_status(course_id, module_id)
        if after['status'] != 'submitted':
            raise PortalError('Final submitted state was not verified. Inspect get_submission_status before retrying.')
        return {'verified_submitted': True, 'submission': after}

    def _owned_attempt(self, course_id, module_id, attempt_id):
        status = self.quiz_status(course_id, module_id)
        user_id = self.client.status()['user']['id']
        attempt = next((a for a in status['attempts'] if a['id'] == attempt_id), None)
        if not attempt or attempt.get('userid') != user_id or attempt.get('quiz') != status['quiz_id']:
            raise PortalError('Attempt does not belong to this account and this quiz.')
        return attempt, status

    def start_quiz(self, course_id, module_id):
        status = self.quiz_status(course_id, module_id)
        unfinished = next((a for a in status['attempts'] if a['state'] in {'inprogress', 'overdue'}), None)
        if unfinished:
            return {'attempt': unfinished, 'reused': True, 'quiz': status}
        if any(a['state'] == 'finished' for a in status['attempts']):
            return {'already_finished': True, 'quiz': status}
        if not status['can_attempt'] or status['prevent_access_reasons']:
            raise PortalError('Quiz access rules currently prevent starting an attempt.')
        raw = self.client._request_api('mod_quiz_start_attempt', {'quizid': status['quiz_id'], 'forcenew': 0})
        attempt = raw.get('attempt', {})
        if not attempt.get('id'):
            raise PortalError('Moodle did not confirm an attempt ID. Check quiz status before retrying.')
        self._owned_attempt(course_id, module_id, attempt['id'])
        return {'attempt': attempt, 'reused': False, 'quiz': status, 'note': 'The real quiz timer may now be running.'}

    def attempt(self, course_id, module_id, attempt_id, page=0):
        owned, _ = self._owned_attempt(course_id, module_id, attempt_id)
        if owned['state'] not in {'inprogress', 'overdue'}:
            raise PortalError('Attempt is no longer in progress. Use get_quiz_status for finished state.')
        data = self.client.call('mod_quiz_get_attempt_data', {'attemptid': attempt_id, 'page': page})
        for question in data.get('questions', []):
            soup = BeautifulSoup(question.get('html', ''), 'html.parser')
            fields = []
            for node in soup.select('input[name], select[name], textarea[name]'):
                name = node.get('name', '')
                if not re.fullmatch(r'q\d+:\d+_.+', name):
                    continue
                label = soup.find('label', attrs={'for': node.get('id')}) if node.get('id') else None
                label_text = label.get_text(' ', strip=True) if label else ''
                if not label_text:
                    labelled = [soup.find(id=ref) for ref in node.get('aria-labelledby', '').split()]
                    label_text = ' '.join(part.get_text(' ', strip=True) for part in labelled if part is not None)
                label_text = ' '.join((label_text or node.get('aria-label', '')).split())
                fields.append({'name': name, 'type': node.get('type', node.name), 'value': node.get('value', node.get_text() if node.name == 'textarea' else ''),
                               'label': label_text, 'checked': node.has_attr('checked'),
                               'options': [{'value': o.get('value', ''), 'text': o.get_text(' ', strip=True),
                                            'selected': o.has_attr('selected')} for o in node.select('option')]})
            question['question_text'] = html_text(question.get('html', ''))
            question['fields'] = fields
        return data

    def save_answers(self, course_id, module_id, attempt_id, page, answers):
        if not answers or len(answers) > 200:
            raise PortalError('Provide 1–200 answer fields from the current attempt page.')
        current = self.attempt(course_id, module_id, attempt_id, page)
        fields = [f for q in current.get('questions', []) for f in q['fields']]
        data = []
        prefixes = set()
        for answer in answers:
            candidates = [f for f in fields if f['name'] == answer.get('name') and f['type'] != 'hidden']
            if not candidates:
                raise PortalError('Answer field is not an editable field from the current question page.')
            value = str(answer.get('value', ''))
            allowed = {f['value'] for f in candidates if f['type'] in {'radio', 'checkbox'}}
            if any(f['type'] == 'checkbox' for f in candidates):
                allowed.update(f['value'] for f in fields if f['name'] == answer['name'] and f['type'] == 'hidden')
            allowed.update(o['value'] for f in candidates for o in f.get('options', []))
            if allowed and value not in allowed:
                raise PortalError('Selected answer value is not one of the current question options.')
            if len(value) > 50000:
                raise PortalError('Answer text is too large.')
            data.append({'name': answer['name'], 'value': value})
            prefixes.add(answer['name'].split('_', 1)[0])
        for field in fields:
            if field['type'] == 'hidden' and field['name'].endswith('_:sequencecheck') and field['name'].split('_', 1)[0] in prefixes:
                data.append({'name': field['name'], 'value': field['value']})
        params = {'attemptid': attempt_id, 'finishattempt': 0, 'timeup': 0}
        for i, field in enumerate(data):
            params[f'data[{i}][name]'] = field['name']; params[f'data[{i}][value]'] = field['value']
        result = self.client._request_api('mod_quiz_process_attempt', params)
        return {**result, 'attempt_id': attempt_id, 'saved_field_count': len(answers), 'finished': result.get('state') == 'finished'}

    def finish_quiz(self, course_id, module_id, attempt_id):
        attempt, _ = self._owned_attempt(course_id, module_id, attempt_id)
        if attempt['state'] == 'finished':
            return {'verified_finished': True, 'already_finished': True, 'attempt': attempt}
        summary = self.client.call('mod_quiz_get_attempt_summary', {'attemptid': attempt_id})
        missing = [q['slot'] for q in summary.get('questions', []) if q.get('type') != 'description' and
                   (q.get('state') in {'todo', 'invalid'} or 'not yet answered' in q.get('status', '').casefold())]
        if missing:
            raise PortalError(f'Quiz has unanswered/invalid question slots: {missing}. Save all answers before finishing.')
        self.client._request_api('mod_quiz_process_attempt', {'attemptid': attempt_id, 'finishattempt': 1, 'timeup': 0})
        after, _ = self._owned_attempt(course_id, module_id, attempt_id)
        if after['state'] != 'finished':
            raise PortalError('Finished quiz state was not verified. Check attempt state before retrying.')
        return {'verified_finished': True, 'attempt': after}
