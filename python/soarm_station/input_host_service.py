"""Install an advertising-only Mac companion with persistent configuration.

The installed source is an immutable snapshot, independent of the development
checkout. Calibration bytes are copied, never generated or recalibrated.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time

from .leader_input import load_config

LABEL = 'org.soarm.station.leader-input'


def install(source, destination, config, python, url, ca=None, launch_agents=None):
    source, destination = Path(source).resolve(), Path(destination).expanduser().resolve()
    python = Path(python).expanduser().absolute()  # Keep venv executable, not its symlink target.
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError('An executable LeRobot Python environment is required')
    if not url.startswith('https://'):
        raise ValueError('Station URL must use HTTPS')
    profile = load_config(config)
    marker = destination/'installation.json'
    if destination.exists() and any(destination.iterdir()) and not marker.exists():
        raise ValueError('Destination is not a Station input-host installation')
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Profile content is explicit input on each install. Preserve edits by passing
    # the installed profile when updating. All calibration paths become local.
    calibration_dir = destination/'calibration'
    calibration_dir.mkdir(exist_ok=True, mode=0o700)
    for item in profile['leaders']:
        data = Path(item['calibration']).read_bytes()
        name = hashlib.sha256(data).hexdigest()+'.json'
        target = calibration_dir/name
        if not target.exists():
            target.write_bytes(data); target.chmod(0o600)
        item['calibration'] = 'calibration/'+name
    profile_path = destination/'input-host.json'
    pending = profile_path.with_suffix('.tmp')
    pending.write_text(json.dumps(profile, indent=2)+'\n'); pending.chmod(0o600)
    os.replace(pending, profile_path)
    package = source/'python'/'soarm_station'
    files = sorted(package.glob('*.py'))
    if not files:raise ValueError('Station source package is missing')
    digest = hashlib.sha256()
    for path in files:digest.update(path.name.encode()+b'\0'+path.read_bytes())
    revision = digest.hexdigest()[:16]
    code = destination/'releases'/revision/'soarm_station'
    code.mkdir(parents=True, exist_ok=True)
    for path in files:shutil.copy2(path, code/path.name)
    args = [str(python), '-m', 'soarm_station.leader_input', '--config', str(profile_path),
            '--url', url, '--log-file', str(destination/'bridge.log')]
    if ca:
        data = Path(ca).read_bytes()
        ca_path = destination/'station-ca.pem'
        ca_path.write_bytes(data)
        args += ['--ca', str(ca_path)]
    agents = Path(launch_agents or Path.home()/'Library'/'LaunchAgents')
    agents.mkdir(parents=True, exist_ok=True)
    plist_path = agents/(LABEL+'.plist')
    job = {'Label': LABEL, 'ProgramArguments': args, 'RunAtLoad': True,
           'KeepAlive': True, 'ThrottleInterval': 15, 'ProcessType': 'Background',
           'WorkingDirectory': str(destination),
           'EnvironmentVariables': {'PYTHONPATH': str(code.parent), 'PYTHONUNBUFFERED': '1'},
           'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}
    plist_path.write_bytes(plistlib.dumps(job)); plist_path.chmod(0o600)
    marker.write_text(json.dumps({'label': LABEL, 'source_revision': revision,
                                 'python': str(python)}, indent=2)+'\n')
    return plist_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--url', required=True)
    parser.add_argument('--ca', type=Path)
    parser.add_argument('--destination', type=Path,
                        default=Path.home()/'Library'/'Application Support'/'SO-Arm Station'/'leader-input')
    parser.add_argument('--no-start', action='store_true', help='Write files without starting/reloading launchd')
    args = parser.parse_args()
    if sys.platform != 'darwin':parser.error('This installer is for macOS launchd')
    root = Path(__file__).resolve().parents[2]
    plist = install(root, args.destination, args.config, args.python, args.url, args.ca)
    if not args.no_start:
        domain = 'gui/'+str(os.getuid())
        subprocess.run(['launchctl', 'bootout', domain+'/'+LABEL], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
        # bootout may return while launchd is still removing the old job. Wait
        # for teardown instead of treating its transient EIO as a broken plist.
        deadline=time.monotonic()+15
        while subprocess.run(['launchctl','print',domain+'/'+LABEL],stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL).returncode==0:
            if time.monotonic()>deadline:raise RuntimeError('Previous companion has not unloaded')
            time.sleep(.2)
        while True:
            result=subprocess.run(['launchctl','bootstrap',domain,str(plist)],capture_output=True,text=True)
            if result.returncode==0:break
            if time.monotonic()>deadline:raise RuntimeError(result.stderr.strip() or 'Could not start companion')
            time.sleep(.3)
    print('Installed:', plist)
    print('Profile:', args.destination/'input-host.json')
    print('Automatic discovery only; Follow leader still requires an operator command.')


if __name__ == '__main__':main()
