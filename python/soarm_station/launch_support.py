"""Repository/environment discovery for direct ROS launch; no hardware access."""
import os
import re
import shlex
from pathlib import Path
from .web_access import validate_access_config


def is_project_root(path):
    root=Path(path).expanduser()
    return (root/'python/soarm_station/config.py').is_file() and (root/'ros2_ws').is_dir()


def discover_root(origin,environment=None):
    """Prefer the selected launch file's checkout over a stale shell variable."""
    for candidate in Path(origin).resolve().parents:
        if is_project_root(candidate):return candidate
    env=os.environ if environment is None else environment
    if env.get('SOARM_STATION_ROOT') and is_project_root(env['SOARM_STATION_ROOT']):return Path(env['SOARM_STATION_ROOT']).expanduser().resolve()
    return None


def resolve_root(value,origin,environment=None):
    root=Path(value).expanduser().resolve() if value else discover_root(origin,environment)
    if root is None or not is_project_root(root):
        raise ValueError('Cannot find the full SO-Arm Station checkout. Pass root:=/absolute/path/to/soarm-station-runtime.')
    return root


def read_secret_environment(path,names):
    """Read NAME=value data without evaluating shell commands or substitutions."""
    result={}
    if not Path(path).is_file():return result
    for number,line in enumerate(Path(path).read_text().splitlines(),1):
        text=line.strip()
        if not text or text.startswith('#'):continue
        if text.startswith('export '):text=text[7:].lstrip()
        name,separator,value=text.partition('=')
        name=name.strip()
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',name):
            raise ValueError(f'Invalid environment assignment on line {number} of {path}')
        if name not in names:continue
        value=value.strip()
        if value.startswith(('"',"'")):
            try:parts=shlex.split(value,comments=True,posix=True)
            except ValueError:raise ValueError(f'Invalid quoted value for {name} in {path}') from None
            if len(parts)!=1:raise ValueError(f'Expected one value for {name} in {path}')
            value=parts[0]
        result[name]=value
    return result


def child_environment(root,config,environment=None,secrets_file=None,keystore=None):
    """Return explicit child overrides. Secret values are never logged."""
    root=Path(root).resolve();base=dict(os.environ if environment is None else environment)
    ros_python=root/'.venv/bin/python'
    native_python=Path(base.get('SOARM_STATION_RUNTIME_PYTHON',str(root/'.runtime-venv/bin/python'))).expanduser().absolute()
    for executable in (ros_python,native_python):
        if not executable.is_file() or not os.access(executable,os.X_OK):
            raise ValueError(f'Missing Python environment: {executable}. Run scripts/bootstrap first.')
    security=Path(keystore or base.get('ROS_SECURITY_KEYSTORE') or root/'local/security').expanduser().resolve()
    needed=('cert.pem','key.pem','identity_ca.cert.pem','permissions_ca.cert.pem','permissions.p7s','governance.p7s')
    if any(not (security/'enclaves'/name).is_file() for name in needed):
        raise ValueError(f'DDS credentials are incomplete at {security}. Run scripts/init-security first.')
    file=Path(secrets_file or root/'local/secrets.env').expanduser()
    if not file.is_absolute():file=root/file
    web=config.get('web',{});collector=config.get('collector',{})
    names={web.get('token_env','SOARM_STATION_TOKEN'),collector.get('token_env','SOARM_STATION_COLLECTOR_TOKEN')}
    private=read_secret_environment(file,names)
    overrides={name:private[name] for name in names if not base.get(name) and name in private}
    if web:
        token_name=web.get('token_env','SOARM_STATION_TOKEN');token=base.get(token_name) or overrides.get(token_name,'')
        validate_access_config(web,token)
        parent=Path(config['_path']).parent
        for field in ('cert','key'):
            value=web.get(field)
            if not value or not (parent/Path(value).expanduser()).is_file():
                raise ValueError(f'Dashboard TLS {field} is missing. Run scripts/init-local or correct the selected YAML.')
    overrides.update(SOARM_STATION_ROOT=str(root),SOARM_STATION_CONFIG=config['_path'],SOARM_STATION_RUNTIME_PYTHON=str(native_python),
        PYTHONPATH=(base['PYTHONPATH']+os.pathsep if base.get('PYTHONPATH') else '')+str(root/'python'),
        PATH=str(root/'.venv/bin')+os.pathsep+base.get('PATH','/usr/bin:/bin'),
        TZ='UTC',PYTHONUNBUFFERED='1',ROS_DOMAIN_ID=base.get('ROS_DOMAIN_ID','71'),
        ROS_AUTOMATIC_DISCOVERY_RANGE=base.get('ROS_AUTOMATIC_DISCOVERY_RANGE','LOCALHOST'),
        ROS_SECURITY_ENABLE='true',ROS_SECURITY_STRATEGY='Enforce',ROS_SECURITY_KEYSTORE=str(security))
    return overrides
