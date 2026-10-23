"""Installation side effects: separate private storage and lossless config updates."""
import json
import stat
import sys
import tomllib
from pathlib import Path
import pytest


def test_config_preserves_unrelated_settings_and_is_repeatable(tmp_path):
    from university_mcp.onboarding import configure_codex
    home=tmp_path/'Codex Settings';home.mkdir()
    path=home/'config.toml'
    original='# personal comment\nmodel = "my-model"\n[mcp_servers.other]\ncommand="node"\nargs=["--experimental", "server.js"]\n'
    path.write_text(original)
    python=tmp_path/'App Folder'/'python';data=tmp_path/'Private Data'
    configure_codex(python,data,home)
    text=path.read_text();doc=tomllib.loads(text)
    assert '# personal comment' in text and doc['model']=='my-model'
    assert doc['mcp_servers']['other']['args']==['--experimental','server.js']
    entry=doc['mcp_servers']['university_portal']
    assert entry['command']==str(python.resolve()) and entry['args']==['-m','university_mcp']
    assert entry['tool_timeout_sec']==180
    assert entry['env']['UNIVERSITY_MCP_AUTH_FILE']==str((data/'auth.json').resolve())
    assert entry['env']['UNIVERSITY_IT_AUTH_FILE']==str((data/'it-auth.json').resolve())
    assert 'token' not in json.dumps(entry)
    assert (home/'config.toml.university-backup').read_text()==original
    configure_codex(python,data,home)
    assert path.read_text()==text
    assert (home/'config.toml.university-backup').read_text()==original
    if sys.platform!='win32':
        assert stat.S_IMODE(data.stat().st_mode)==0o700
        assert stat.S_IMODE(path.stat().st_mode)==0o600


def test_conflicting_installation_requires_explicit_replace(tmp_path):
    from university_mcp.onboarding import configure_codex,SetupError
    home=tmp_path/'codex';home.mkdir();p=home/'config.toml'
    p.write_text('[mcp_servers.university_portal]\ncommand="foreign-command"\n')
    before=p.read_bytes()
    with pytest.raises(SetupError,match='replace'):configure_codex(Path(sys.executable),tmp_path/'data',home)
    assert p.read_bytes()==before
    configure_codex(Path(sys.executable),tmp_path/'data',home,replace=True)
    assert tomllib.loads(p.read_text())['mcp_servers']['university_portal']['command']==str(Path(sys.executable).absolute())


def test_malformed_config_is_not_overwritten(tmp_path):
    from university_mcp.onboarding import configure_codex,SetupError
    p=tmp_path/'config.toml';p.write_text('invalid = [')
    with pytest.raises(SetupError):configure_codex(Path(sys.executable),tmp_path/'data',tmp_path)
    assert p.read_text()=='invalid = ['


@pytest.mark.skipif(sys.platform=='win32',reason='POSIX symlink permission check')
def test_config_symlink_is_not_followed(tmp_path):
    from university_mcp.onboarding import configure_codex,SetupError
    target=tmp_path/'target';target.write_text('model="preserve"')
    home=tmp_path/'codex';home.mkdir();(home/'config.toml').symlink_to(target)
    with pytest.raises(SetupError):configure_codex(Path(sys.executable),tmp_path/'data',home)
    assert target.read_text()=='model="preserve"'


@pytest.mark.parametrize('platform,environ,suffix',[
    ('darwin',{},'Library/Application Support/UniversityPortalMCP'),
    ('linux',{},'.local/share/university-portal-mcp'),
    ('linux',{'XDG_DATA_HOME':'custom-data'},'custom-data/university-portal-mcp'),
    ('win32',{'LOCALAPPDATA':'WindowsData'},'WindowsData/UniversityPortalMCP')])
def test_storage_paths_follow_each_platform(tmp_path,monkeypatch,platform,environ,suffix):
    from university_mcp.onboarding import default_data_dir
    monkeypatch.setattr(sys,'platform',platform)
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    for k in ('XDG_DATA_HOME','LOCALAPPDATA'):monkeypatch.delenv(k,raising=False)
    for k,v in environ.items():monkeypatch.setenv(k,str(tmp_path/v))
    assert default_data_dir()==tmp_path/suffix


def test_doctor_reports_missing_login_without_claiming_connection(tmp_path):
    from university_mcp.onboarding import configure_codex,doctor
    data=tmp_path/'data';home=tmp_path/'codex'
    configure_codex(Path(sys.executable),data,home)
    result=doctor(data,home)
    assert result['configured'] and result['moodle']['status']=='needs_login'
    assert result['it']['status']=='needs_login' and result['ready'] is False

def test_doctor_json_prints_safely_to_non_utf8_terminal(tmp_path,monkeypatch):
    import io
    from university_mcp import onboarding
    monkeypatch.setattr(onboarding,'doctor',lambda *args:{'configured':True,'ready':True,'moodle':{'status':'connected','name':'Студент'}})
    output=io.BytesIO();terminal=io.TextIOWrapper(output,encoding='cp1252',write_through=True)
    monkeypatch.setattr(sys,'stdout',terminal)
    assert onboarding.main(['--doctor','--live'])==0
    value=json.loads(output.getvalue().decode('cp1252'))
    assert value['moodle']['name']=='Студент'
