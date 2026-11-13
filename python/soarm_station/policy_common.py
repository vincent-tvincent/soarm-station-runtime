"""Small shared policy-management primitives; no ROS/model/device imports."""
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path


class PolicyError(ValueError):
    def __init__(self,code,message,status=400,details=None):
        super().__init__(message);self.code=code;self.message=message;self.status=status;self.details=details
    def response(self):
        result={'schema_version':1,'code':self.code,'message':self.message}
        if self.details is not None:result['details']=self.details
        return result


def revision(data):
    if not isinstance(data,bytes):data=json.dumps(data,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    return 'sha256:'+hashlib.sha256(data).hexdigest()


def identifier(value,label='ID'):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',value):
        raise PolicyError('INVALID_ID',label+' must start with a lowercase letter and contain at most64 letters/digits/_/-')
    return value


def relative_path(value,allow_dot=False):
    if not isinstance(value,str) or not value or '\\' in value or '\x00' in value or len(value)>1024:
        raise PolicyError('INVALID_PATH','A bounded POSIX relative path is required')
    path=Path(value)
    if not path.parts:
        if value=='.' and allow_dot:return '.'
        raise PolicyError('INVALID_PATH','A file/model relative path is required')
    if path.is_absolute() or any(part in ('..','') for part in path.parts) or ':' in path.parts[0]:
        raise PolicyError('INVALID_PATH','Path must stay relative to its configured source')
    return path.as_posix()


def atomic_write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    handle,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(handle,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
        directory=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)
