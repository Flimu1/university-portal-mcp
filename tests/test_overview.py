"""Own-account data, partial responses and local change-baseline contracts."""
import json
from urllib.parse import parse_qs
import httpx
import pytest
from university_mcp.actions import MoodleActions
from university_mcp.client import MoodleClient,PortalError,AuthenticationError
from test_client import TOKEN
NOW=1800000000

def setup(tmp_path):
 from university_mcp.overview import StudyOverview
 state={'score':None,'message':'<p>Bring notes</p>','restricted':False,'auth_fail':False};calls=[]
 def handler(request):
  a=parse_qs(request.content.decode());fn=a['wsfunction'][0];calls.append((fn,a))
  assert a['wstoken']==[TOKEN] and 'wstoken' not in str(request.url)
  if state['auth_fail']:return httpx.Response(200,json={'exception':'moodle_exception','errorcode':'invalidtoken'})
  if fn=='core_webservice_get_site_info':v={'userid':9991,'fullname':'Example student','functions':[]}
  elif fn=='core_enrol_get_users_courses':v=[{'id':42,'fullname':'Example course'}]
  elif fn=='core_course_get_contents':v=[{'id':1,'modules':[{'id':n,'instance':n+100,'name':f'Activity {n}','modname':'assign' if n==3 else 'resource','uservisible':n!=9} for n in [1,2,3,4,5,9]]}]
  elif fn=='gradereport_overview_get_course_grades':v={'grades':[{'courseid':42,'grade':'-','rawgrade':None}],'warnings':[]}
  elif fn=='gradereport_user_get_grade_items':
   v=({'exception':'moodle_exception','errorcode':'nopermissions'} if state['restricted'] else {'usergrades':[{'userid':9991,'gradeitems':[
    {'id':1,'cmid':3,'itemname':'Report','itemmodule':'assign','graderaw':state['score'],'grademax':10,'gradeishidden':False,'feedback':'<p>Clear <b>answer</b></p>','gradedatesubmitted':NOW-100,'gradedategraded':NOW if state['score'] is not None else None},
    {'id':2,'cmid':4,'itemname':'Hidden','itemmodule':'quiz','graderaw':8,'grademax':10,'gradeishidden':True,'feedback':'Secret feedback'}]}],'warnings':[]})
  elif fn=='core_completion_get_activities_completion_status':v={'statuses':[{'cmid':n,'state':st,'tracking':tr} for n,st,tr in [(1,0,1),(2,1,1),(3,2,2),(4,3,2),(5,0,0),(9,1,1)]],'warnings':[]}
  elif fn=='core_calendar_get_calendar_events':
   assert a['options[timestart]']==[str(NOW)] and a['events[courseids][0]']==['42']
   v={'events':[{'id':n,'name':'Event','courseid':co,'userid':u,'timestart':t,'visible':vis} for n,co,u,t,vis in [(1,42,88,NOW+100,1),(2,42,0,NOW+100,0),(3,0,123,NOW+100,1),(4,777,0,NOW+100,1),(5,42,0,NOW+100*86400,1)]],'warnings':[]}
  elif fn=='mod_forum_get_forums_by_courses':v=[{'id':7,'course':42,'type':'news','name':'News'},{'id':8,'course':42,'type':'general','name':'Chat'}]
  elif fn=='mod_forum_get_forum_discussions':
   assert a['forumid']==['7'];page=int(a.get('page',['0'])[0]);size=int(a.get('perpage',['50'])[0])
   v={'discussions':([{'discussion':91+i,'name':state.get('title',f'News {i}'),'message':state['message'],'timemodified':NOW-50,'created':NOW-100} for i in range(size)] if page==0 else [{'discussion':999,'name':'Page2','message':'More','timemodified':NOW-200}]),'warnings':[]}
  elif fn=='mod_assign_get_assignments':v={'courses':[{'id':42,'assignments':[{'id':103,'cmid':3,'name':'Report','duedate':NOW+500,'cutoffdate':0,'introfiles':[],'introattachments':[]}]}],'warnings':[]}
  elif fn=='mod_quiz_get_quizzes_by_courses':v={'quizzes':[],'warnings':[]}
  elif fn=='mod_assign_get_submission_status':v={'lastattempt':{'submission':{'status':'submitted','plugins':[]},'canedit':False},'warnings':[]}
  else:raise AssertionError('Unexpected '+fn)
  return httpx.Response(200,json=v)
 c=MoodleClient(TOKEN,tmp_path/'state',http=httpx.Client(transport=httpx.MockTransport(handler)))
 return StudyOverview(c,MoodleActions(c,tmp_path/'submissions',now=lambda:NOW),now=lambda:NOW),state,calls

def test_grades_preserve_unknown_zero_hidden_and_plain_feedback(tmp_path):
 s,st,_=setup(tmp_path);items=s.course_grades(42)['items']
 assert items[0]['score'] is None and not items[0]['grade_available'] and items[0]['feedback']=='Clear answer'
 assert items[1]['score'] is None and items[1]['feedback']=='' and items[1]['hidden']
 st['score']=0;assert s.course_grades(42)['items'][0]['grade_available'] is True
 assert s.course_grades(42)['items'][0]['score']==0 and s.grades()['courses'][0]['score'] is None

def test_unenrolled_course_is_rejected_before_grade_request(tmp_path):
 s,_,calls=setup(tmp_path)
 with pytest.raises(PortalError):s.course_grades(777)
 assert not any(fn=='gradereport_user_get_grade_items' for fn,_ in calls)

def test_progress_separates_failed_complete_untracked_and_hidden(tmp_path):
 s,_,_=setup(tmp_path);v=s.progress(42)
 assert (v['tracked'],v['completed'],v['failed'],v['percent'])==(4,3,1,75)
 assert {i['module_id'] for i in v['activities']}=={1,2,3,4}

def test_calendar_range_visibility_and_account_boundaries(tmp_path):
 s,_,_=setup(tmp_path);v=s.calendar(30)
 assert [e['id'] for e in v['events']]==[1] and v['events'][0]['starts_at'].endswith('+03:00')

def test_announcements_paginate_news_only_and_strip_html(tmp_path):
 s,_,calls=setup(tmp_path);v=s.announcements(30,42)
 assert len(v['items'])==51 and any(n['id']==999 for n in v['items']) and v['items'][0]['text']=='Bring notes'
 assert [a['page'] for fn,a in calls if fn=='mod_forum_get_forum_discussions']==[['0'],['1']]

def test_overview_baseline_zero_grade_updated_news_and_no_portal_writes(tmp_path):
 s,st,calls=setup(tmp_path);first=s.overview(30,True)
 assert first['changes']==[] and first['baseline_initialized']
 assert first['recent_work'][0]['submission_status']=='submitted' and first['recent_work'][0]['assessment_state']=='awaiting_grade'
 st['score']=0;st['message']='<p>New date</p>'
 changed=s.overview(30,False)
 assert {x['kind'] for x in changed['changes']}=={'grade_published','announcement_updated'}
 assert s.overview(30,False)['changes']==changed['changes']
 s.overview(30,True);assert s.overview(30,False)['changes']==[]
 assert not any(any(w in fn for w in ['save','submit','process','update','mark','start','send']) for fn,_ in calls)

def test_partial_grade_failure_preserves_baseline_without_false_alert(tmp_path):
 s,st,_=setup(tmp_path);st['score']=5;s.overview(30,True);st['restricted']=True
 partial=s.overview(30,True);assert partial['warnings'] and partial['changes']==[]
 st['restricted']=False;assert s.overview(30,False)['changes']==[]

def test_auth_failure_does_not_overwrite_baseline(tmp_path):
 s,st,_=setup(tmp_path);s.overview(30,True)
 before={p:p.read_bytes() for p in (tmp_path/'state').rglob('study-*.json')};assert before
 st['auth_fail']=True
 with pytest.raises(AuthenticationError):s.overview(30,True)
 assert all(p.read_bytes()==content for p,content in before.items())

def test_initial_partial_source_recovery_is_not_a_new_grade(tmp_path):
 s,st,_=setup(tmp_path);st['score']=5;st['restricted']=True
 s.overview(30,True);st['restricted']=False
 assert s.overview(30,False)['changes']==[]
 s.overview(30,True);st['score']=6
 assert {c['kind'] for c in s.overview(30,False)['changes']}=={'grade_updated'}

def test_different_windows_have_independent_first_baselines(tmp_path):
 s,_,_=setup(tmp_path)
 s.overview(1,True)
 assert s.overview(30,False)['baseline_initialized'] is False
 assert s.overview(30,False)['changes']==[]
 s.overview(30,True)
 assert s.overview(1,False)['changes']==[]


def test_snapshot_uses_utf8_on_legacy_system_encoding(tmp_path,monkeypatch):
 import os
 s,st,_=setup(tmp_path);st['title']='Новое занятие'
 original=os.fdopen
 def legacy_default(*args,**kwargs):
  kwargs.setdefault('encoding','cp1252')
  return original(*args,**kwargs)
 monkeypatch.setattr(os,'fdopen',legacy_default)
 s.overview(30,True)
 saved=next((tmp_path/'state').rglob('study-*.json'))
 assert 'Новое занятие' in saved.read_text(encoding='utf-8')
 assert s.overview(30,False)['changes']==[]
