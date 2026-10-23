import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from test_client import SITE, TOKEN

NOW = 1800000000
QUESTION = {'slot': 1, 'type': 'multichoice', 'page': 0, 'state': 'complete', 'status': 'Answer saved',
            'html': '<div class="qtext">Choose one</div><input type="hidden" name="q900:1_:sequencecheck" value="1"><input type="radio" name="q900:1_answer" value="0" id="a"><label for="a">Correct option</label>'}


def actions(tmp_path, *, submitted=False, finished=False, locked=False, wrong_owner=False, drop_files=False, drop_text=False, existing_file=False, restricted_status=False):
    from university_mcp.client import MoodleClient
    from university_mcp.actions import MoodleActions
    writes = []
    state = {'submission': 'submitted' if submitted else 'new', 'started': False,
             'quiz': 'finished' if finished else 'inprogress', 'online_text': 'OLD ANSWER',
             'files': ([{'filename':'old.txt','fileurl':SITE+'/webservice/pluginfile.php/9/mod_assign/submission_files/1/old.txt','filesize':15}] if existing_file else []), 'uploaded': []}
    assignment = {'id': 30, 'cmid': 3, 'name': 'Task', 'intro': 'Requirements', 'duedate': NOW + 3600,
                  'cutoffdate': NOW + 7200, 'submissiondrafts': 1, 'requiresubmissionstatement': 0, 'introfiles': [], 'introattachments': []}
    quiz = {'id': 40, 'coursemodule': 4, 'course': 569, 'name': 'Test', 'timeclose': NOW + 1800, 'timeopen': NOW - 100, 'timelimit': 600}

    def handle(request):
        if request.method == 'GET':
            return httpx.Response(200, content=b'Existing answer', headers={'Content-Type':'text/plain'})
        if request.url.path == '/webservice/upload.php':
            writes.append('upload')
            assert b'Generated answer' in request.content or b'Existing answer' in request.content
            import re
            filename = re.search(rb'filename="([^"]+)"',request.content).group(1).decode()
            state['uploaded'].append(filename)
            return httpx.Response(200, json=[{'itemid': 1234, 'filename': filename}])
        args = parse_qs(request.content.decode())
        name = args['wsfunction'][0]
        if name == 'core_webservice_get_site_info': data = {'userid': 9991, 'fullname': 'Student', 'functions': []}
        elif name == 'core_enrol_get_users_courses': data = [{'id': 569, 'fullname': 'Accounting'}]
        elif name == 'core_course_get_contents': data = [{'id': 1, 'name': 'Topic', 'modules': [
            {'id': 3, 'instance': 30, 'name': 'Task', 'modname': 'assign', 'uservisible': True},
            {'id': 4, 'instance': 40, 'name': 'Test', 'modname': 'quiz', 'uservisible': True}]}]
        elif name == 'mod_assign_get_assignments': data = {'courses': [{'id': 569, 'assignments': [assignment]}], 'warnings': []}
        elif name == 'mod_quiz_get_quizzes_by_courses': data = {'quizzes': [quiz], 'warnings': []}
        elif name == 'mod_assign_get_submission_status': data = ({'exception':'moodle_exception','errorcode':'nopermissions'} if restricted_status else {'lastattempt': {'submission': {'status': state['submission'], 'plugins': [{'fileareas':[{'files':state['files']}], 'editorfields':[{'text':state['online_text']}]}]}, 'canedit': not locked, 'locked': locked}, 'warnings': []})
        elif name == 'mod_quiz_get_user_attempts':
            data = {'attempts': ([{'id': 55, 'quiz': 40, 'userid': 999 if wrong_owner else 9991, 'state': state['quiz']}] if state['started'] or finished or wrong_owner else []), 'warnings': []}
        elif name == 'mod_quiz_get_quiz_access_information': data = {'canattempt': True, 'preventaccessreasons': [], 'warnings': []}
        elif name == 'mod_quiz_start_attempt':
            writes.append(name);state['started'] = True
            data = {'attempt': {'id': 55, 'quiz': 40, 'userid': 9991, 'state': state['quiz']}, 'warnings': []}
        elif name == 'mod_quiz_get_attempt_data': data = {'attempt': {'id': 55, 'quiz': 40, 'userid': 9991, 'state': state['quiz']}, 'questions': [QUESTION], 'nextpage': -1, 'warnings': []}
        elif name == 'mod_quiz_get_attempt_summary': data = {'questions': [QUESTION], 'warnings': []}
        elif name == 'mod_quiz_process_attempt':
            writes.append(name)
            if args.get('finishattempt') == ['1']: state['quiz'] = 'finished'
            else:
                assert args['data[0][name]'][0] in {'q900:1_answer','q900:1_choice0'} and args['data[0][value]'] == ['0']
                assert args['data[1][name]'] == ['q900:1_:sequencecheck']
            data = {'state': state['quiz'], 'warnings': []}
        elif name == 'mod_assign_save_submission':
            writes.append(name);state['submission'] = 'draft'
            if 'plugindata[files_filemanager]' in args:
                assert args['plugindata[files_filemanager]'] == ['1234']
                if not drop_files: state['files'] = [{'filename':n,'fileurl':SITE+'/webservice/pluginfile.php/9/mod_assign/submission_files/1/'+n} for n in state['uploaded']]
            if 'plugindata[onlinetext_editor][text]' in args and not drop_text:state['online_text']=args['plugindata[onlinetext_editor][text]'][0]
            data = []
        elif name == 'mod_assign_submit_for_grading':
            writes.append(name);state['submission'] = 'submitted';data = {'status': True, 'warnings': []}
        else: raise AssertionError('Unexpected API ' + name)
        return httpx.Response(200, json=data)

    client = MoodleClient(TOKEN, tmp_path / 'state', http=httpx.Client(transport=httpx.MockTransport(handle)))
    root = tmp_path / 'submissions';root.mkdir()
    return MoodleActions(client, root, now=lambda: NOW), writes, state


def test_deadline_queue_sorts_quizzes_and_assignments_and_skips_completed(tmp_path):
    tool, writes, state = actions(tmp_path)
    result = tool.deadlines(days=7)
    assert [i['module_id'] for i in result['items']] == [4, 3]
    assert all(i['due_at'].endswith('+03:00') for i in result['items'])
    assert writes == []
    state['submission'] = 'submitted';state['started'] = True;state['quiz'] = 'finished'
    assert tool.deadlines(days=7)['items'] == []


def test_generated_file_is_saved_then_submitted_with_verified_server_state(tmp_path):
    tool, writes, state = actions(tmp_path)
    path = tmp_path / 'submissions' / 'answer.txt';path.write_text('Generated answer')
    saved = tool.save_assignment(569, 3, [str(path)])
    assert saved['submission']['status'] == 'draft'
    assert 'mod_assign_submit_for_grading' not in writes
    done = tool.submit_assignment(569, 3)
    assert done['verified_submitted'] is True and done['submission']['status'] == 'submitted'


def test_secrets_outside_generated_file_root_cannot_be_uploaded(tmp_path):
    from university_mcp.client import PortalError
    tool, writes, _ = actions(tmp_path)
    secret = tmp_path / 'auth.json';secret.write_text('secret')
    with pytest.raises(PortalError): tool.save_assignment(569, 3, [str(secret)])
    assert writes == []


@pytest.mark.parametrize('mode', ['submitted', 'locked'])
def test_completed_or_locked_assignment_is_never_overwritten(tmp_path, mode):
    from university_mcp.client import PortalError
    tool, writes, _ = actions(tmp_path, **{mode: True})
    path = tmp_path / 'submissions' / 'answer.txt';path.write_text('Generated answer')
    with pytest.raises(PortalError): tool.save_assignment(569, 3, [str(path)])
    assert writes == []


def test_quiz_start_reuses_attempt_and_answers_save_without_finishing(tmp_path):
    tool, writes, state = actions(tmp_path)
    first = tool.start_quiz(569, 4)
    second = tool.start_quiz(569, 4)
    assert first['attempt']['id'] == second['attempt']['id'] == 55
    assert writes.count('mod_quiz_start_attempt') == 1
    result = tool.save_answers(569, 4, 55, 0, [{'name': 'q900:1_answer', 'value': '0'}])
    assert result['state'] == 'inprogress' and state['quiz'] == 'inprogress'
    done = tool.finish_quiz(569, 4, 55)
    assert done['verified_finished'] is True


def test_quiz_attempt_ownership_and_unknown_fields_block_mutation(tmp_path):
    from university_mcp.client import PortalError
    tool, writes, _ = actions(tmp_path, wrong_owner=True)
    with pytest.raises(PortalError): tool.save_answers(569, 4, 55, 0, [{'name': 'q900:1_answer', 'value': '0'}])
    assert writes == []
    tool, writes, state = actions(tmp_path / 'other')
    tool.start_quiz(569, 4)
    before = list(writes)
    with pytest.raises(PortalError): tool.save_answers(569, 4, 55, 0, [{'name': 'unknown-field', 'value': 'x'}])
    assert writes == before


def test_assignment_save_rejects_unverified_attachment_and_online_text(tmp_path):
    from university_mcp.client import PortalError
    tool, _, _ = actions(tmp_path / 'filecase', drop_files=True)
    path = tmp_path / 'filecase' / 'submissions' / 'answer.txt'; path.write_text('Generated answer')
    with pytest.raises(PortalError): tool.save_assignment(569, 3, [str(path)])
    tool, _, _ = actions(tmp_path / 'textcase', drop_text=True)
    with pytest.raises(PortalError): tool.save_assignment(569, 3, [], 'NEW ANSWER')


def test_existing_draft_filename_is_preserved_when_adding_generated_file(tmp_path):
    tool, _, _ = actions(tmp_path, existing_file=True)
    path = tmp_path / 'submissions' / 'answer.txt';path.write_text('Generated answer')
    result = tool.save_assignment(569, 3, [str(path)])
    assert {f['filename'] for f in result['submission']['files']} == {'old.txt','answer.txt'}


def test_deadline_queue_preserves_other_work_when_one_status_is_inaccessible(tmp_path):
    tool, _, _ = actions(tmp_path, restricted_status=True)
    result = tool.deadlines(days=7)
    assert [i['module_id'] for i in result['items']] == [4]
    assert result['warnings'][0]['module_id'] == 3


def test_quiz_checkbox_can_be_cleared_using_actual_hidden_default(tmp_path, monkeypatch):
    monkeypatch.setitem(QUESTION,'html', '<div class="qtext">Pick options</div><input type="hidden" name="q900:1_:sequencecheck" value="1"><input type="hidden" name="q900:1_choice0" value="0"><input type="checkbox" name="q900:1_choice0" value="1">')
    tool, writes, state = actions(tmp_path)
    tool.start_quiz(569,4)
    result=tool.save_answers(569,4,55,0,[{'name':'q900:1_choice0','value':'0'}])
    assert result['state']=='inprogress'


def test_finishing_quiz_refuses_unanswered_questions(tmp_path, monkeypatch):
    from university_mcp.client import PortalError
    monkeypatch.setitem(QUESTION,'state','todo')
    tool, writes, state = actions(tmp_path)
    tool.start_quiz(569,4)
    count=len(writes)
    with pytest.raises(PortalError):tool.finish_quiz(569,4,55)
    assert len(writes)==count


def test_quiz_option_text_uses_moodle_aria_labelledby(tmp_path, monkeypatch):
    monkeypatch.setitem(QUESTION, 'html', '<div class="r0"><input type="radio" name="q900:1_answer" value="0" id="a" aria-labelledby="a_label"><div id="a_label"><p>Federal and local</p></div></div>')
    tool, _, _ = actions(tmp_path)
    tool.start_quiz(569, 4)
    field = tool.attempt(569, 4, 55)['questions'][0]['fields'][0]
    assert field['label'] == 'Federal and local'


def test_quiz_fields_expose_current_selection_for_verifying_saved_answers(tmp_path, monkeypatch):
    monkeypatch.setitem(QUESTION, 'html', '<input type="radio" name="q900:1_answer" value="0" checked><input type="radio" name="q900:1_answer" value="1"><select name="q900:1_part"><option value="0">Choose</option><option value="2" selected>Yes</option></select>')
    tool, _, _ = actions(tmp_path)
    tool.start_quiz(569, 4)
    fields = tool.attempt(569, 4, 55)['questions'][0]['fields']
    assert [f['checked'] for f in fields[:2]] == [True, False]
    assert [o['selected'] for o in fields[2]['options']] == [False, True]

def test_deadlines_propagate_source_and_status_warnings(tmp_path, monkeypatch):
    tool, _, _ = actions(tmp_path)
    original = tool.client._assignments
    monkeypatch.setattr(tool.client, '_assignments', lambda cid: (original(cid)[0], [{'message':'Assignment list incomplete'}]))
    original_status = tool.submission_status
    monkeypatch.setattr(tool, 'submission_status', lambda cid, mid: {**original_status(cid, mid), 'warnings':[{'message':'Status incomplete'}]})
    result = tool.deadlines()
    assert {w['message'] for w in result['warnings']} >= {'Assignment list incomplete','Status incomplete'}
