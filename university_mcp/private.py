"""Private local state without symlink following or partial credential writes."""
import json
import os
from pathlib import Path
import stat
import tempfile


def private_directory(path):
    path=Path(path).expanduser().absolute()
    if path.is_symlink():
        raise ValueError('Private storage must not be a symlink.')
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    info=path.stat()
    if os.name!='nt' and info.st_uid!=os.getuid():
        raise ValueError('Private storage must belong to the current user.')
    if os.name!='nt':path.chmod(0o700)
    return path


def load_private_json(path):
    path=Path(path);info=path.lstat()
    if not stat.S_ISREG(info.st_mode):raise ValueError('Private file must be a regular file.')
    if os.name!='nt' and (info.st_mode&0o077 or info.st_uid!=os.getuid()):
        raise ValueError('Private file permissions must be 0600 and owned by the current user.')
    if info.st_size>65536:raise ValueError('Private file exceeds supported size.')
    value=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict):raise ValueError('Private file must contain an object.')
    return value


def save_private_json(path,value):
    path=Path(path).expanduser().absolute()
    private_directory(path.parent)
    if path.is_symlink():raise ValueError('Private file must not be a symlink.')
    fd,tmp=tempfile.mkstemp(prefix='.private-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as handle:json.dump(value,handle,ensure_ascii=False)
        if os.name!='nt':os.chmod(tmp,0o600)
        os.replace(tmp,path)
    finally:
        if Path(tmp).exists():Path(tmp).unlink()
