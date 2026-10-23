"""Install one private local account connection without rewriting other Codex settings."""
import argparse
import json
import logging
import os
from pathlib import Path
import sys
import tempfile

import tomlkit

from .private import private_directory,load_private_json


class SetupError(Exception):
    pass


def default_data_dir():
    home=Path.home()
    if sys.platform=='win32':return Path(os.environ.get('LOCALAPPDATA',home/'AppData'/'Local'))/'UniversityPortalMCP'
    if sys.platform=='darwin':return home/'Library'/'Application Support'/'UniversityPortalMCP'
    return Path(os.environ.get('XDG_DATA_HOME',home/'.local'/'share'))/'university-portal-mcp'


def codex_directory():
    return Path(os.environ.get('CODEX_HOME',Path.home()/'.codex')).expanduser().absolute()


def _entry(python,data):
    return {'command':str(Path(python).expanduser().absolute()),'args':['-m','university_mcp'],
        'tool_timeout_sec':180,'env':{
        'UNIVERSITY_MCP_AUTH_FILE':str(data/'auth.json'),
        'UNIVERSITY_IT_AUTH_FILE':str(data/'it-auth.json'),
        'UNIVERSITY_MCP_STATE_DIR':str(data/'state'),
        'UNIVERSITY_MCP_DOWNLOAD_DIR':str(data/'materials'),
        'UNIVERSITY_MCP_SUBMISSION_DIR':str(data/'submissions')}}


def configure_codex(python,data_dir,codex_home,replace=False):
    data=private_directory(data_dir).resolve()
    home=Path(codex_home).expanduser().absolute();home.mkdir(parents=True,exist_ok=True)
    path=home/'config.toml'
    if path.is_symlink():raise SetupError('Codex config is a symlink; edit its source explicitly instead.')
    before=path.read_bytes() if path.exists() else b''
    try:
        doc=tomlkit.parse(before.decode('utf-8'))
        servers=doc.setdefault('mcp_servers',tomlkit.table())
        old=servers.get('university_portal')
        expected=_entry(python,data)
        if old is not None and (old.get('command')!=expected['command'] or old.get('env',{}).get('UNIVERSITY_MCP_AUTH_FILE')!=str(data/'auth.json')) and not replace:
            raise SetupError('Another university_portal installation exists. Keep it or rerun with --replace explicitly.')
        if old is not None and old.unwrap()==expected:return expected
        servers['university_portal']=expected
        content=tomlkit.dumps(doc).encode('utf-8')
    except SetupError:raise
    except Exception:
        raise SetupError('Codex config is invalid or unsupported; it was not overwritten.') from None
    backup=home/'config.toml.university-backup'
    if before and not backup.exists():
        try:
            fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as handle:handle.write(before)
        except FileExistsError:pass
    fd,tmp=tempfile.mkstemp(prefix='.university-config-',dir=home)
    try:
        with os.fdopen(fd,'wb') as handle:handle.write(content)
        if path.is_symlink() or (path.read_bytes() if path.exists() else b'')!=before:
            raise SetupError('Codex config changed during setup; retry without losing those changes.')
        os.replace(tmp,path)
    finally:
        if Path(tmp).exists():Path(tmp).unlink()
    return expected


def doctor(data_dir,codex_home,live=False):
    data=Path(data_dir).expanduser().resolve();configured=False
    try:
        doc=tomlkit.parse((Path(codex_home)/'config.toml').read_text(encoding='utf-8'))
        e=doc.get('mcp_servers',{}).get('university_portal',{})
        configured=(e.get('env',{}).get('UNIVERSITY_MCP_AUTH_FILE')==str(data/'auth.json') and Path(e.get('command','')).is_file())
    except (OSError,ValueError,tomlkit.exceptions.TOMLKitError):pass
    result={'configured':configured,'moodle':{'status':'needs_login'},'it':{'status':'needs_login'},'ready':False}
    try:
        auth=load_private_json(data/'auth.json')
        from .models import SITE
        if auth.get('site')!=SITE or not auth.get('token'):raise ValueError('invalid auth')
        result['moodle']={'status':'saved_not_verified'}
        if live:
            from .client import MoodleClient
            api=MoodleClient(auth['token'],data/'doctor-state')
            try:
                status=api.status();courses=api.courses(refresh=True)
                result['moodle']={'status':'connected','user':status['user'],'courses':courses['total']}
            finally:api.http.close()
    except FileNotFoundError:pass
    except Exception:result['moodle']={'status':'needs_login','message':'Login is unavailable or expired; open the local setup again.'}
    if (data/'it-auth.json').exists():
        try:
            from .it_portal import ITPortalClient
            it=ITPortalClient(data/'it-auth.json')
            try:
                result['it']={'status':'saved_not_verified'}
                if live:
                    report=it.report('grades')
                    result['it']={'status':'connected','selection':report['selection']}
            finally:it.close()
        except Exception:result['it']={'status':'needs_login','message':'Open optional IT login again.'}
    result['ready']=configured and result['moodle']['status']=='connected'
    return result


def main(argv=None):
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    parser=argparse.ArgumentParser(description='Private university connection setup')
    parser.add_argument('--data-dir',type=Path,default=default_data_dir())
    parser.add_argument('--codex-home',type=Path,default=codex_directory())
    parser.add_argument('--replace',action='store_true')
    parser.add_argument('--configure-only',action='store_true')
    parser.add_argument('--doctor',action='store_true')
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.doctor:
            result=doctor(args.data_dir,args.codex_home,args.live)
            print(json.dumps(result,ensure_ascii=True,indent=2))
            return 0 if result['ready'] or (not args.live and result['configured']) else 1
        configure_codex(Path(sys.executable),args.data_dir,args.codex_home,args.replace)
        print('1/3 Python environment ready.\n2/3 Codex connection configured.',flush=True)
        if args.configure_only:
            print('3/3 Run this command again without --configure-only to sign in privately. No login is claimed.')
            return 0
        from .setup_web import run_setup
        return 0 if run_setup(args.data_dir,open_browser=not args.no_browser) else 1
    except (SetupError,OSError,ValueError) as exc:
        print('Setup stopped: '+str(exc),file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
