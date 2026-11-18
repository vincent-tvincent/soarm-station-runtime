"""Small browser-readable leader calibration profiles; no device access."""
import asyncio
from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading


MAX_PROFILE_BYTES=16*1024
MAX_PROFILES=64
MAX_BODY_JOINTS=32
IDENTIFIER=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}')
JOINT_NAME=re.compile(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}')
CALIBRATION_FIELDS={'id','drive_mode','homing_offset','range_min','range_max'}


class ProfileError(ValueError):
    def __init__(self,message,code='INVALID_LEADER_PROFILE',status=400):
        super().__init__(message);self.code=code;self.status=status


def _require(condition,message):
    if not condition:raise ProfileError(message)


def _json_pairs(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ProfileError('Duplicate JSON field: '+key[:64])
        value[key]=item
    return value


def decode_json(value):
    try:return json.loads(value,object_pairs_hook=_json_pairs)
    except ProfileError:raise
    except (ValueError,UnicodeDecodeError,RecursionError) as error:
        raise ProfileError('Profile must be valid JSON') from error


def normalize_profile(value,arms):
    """Return only the canonical browser schema, preserving calibration values."""
    _require(isinstance(value,dict),'Profile must be a JSON object')
    allowed={'id','label','arm_id','motor_model','joint_map','gripper_source','calibration','match'}
    _require(not set(value)-allowed,'Unsupported profile fields; paths, serial numbers and credentials are not accepted')
    identifier=value.get('id');label=value.get('label')
    _require(isinstance(identifier,str) and IDENTIFIER.fullmatch(identifier) is not None,
             'Profile id must be 1–64 letters, digits, dots, underscores or hyphens, starting with a letter or digit')
    _require(isinstance(label,str) and 1<=len(label.strip())<=128,'Profile label must contain 1–128 visible characters')
    _require(value.get('motor_model','sts3215')=='sts3215','Only motor_model sts3215 is supported')
    calibration=value.get('calibration')
    _require(isinstance(calibration,dict) and 2<=len(calibration)<=MAX_BODY_JOINTS+1,
             'Calibration must contain body motors and a separate gripper (2–33 motors)')
    normalized={};motor_ids=set()
    for name,item in calibration.items():
        _require(isinstance(name,str) and JOINT_NAME.fullmatch(name) is not None,'Calibration motor names must be valid names of at most 64 characters')
        _require(isinstance(item,dict) and set(item)==CALIBRATION_FIELDS,
                 f'Calibration for {name} requires exactly id, drive_mode, homing_offset, range_min and range_max')
        _require(all(type(item[key]) is int for key in CALIBRATION_FIELDS),f'Calibration values for {name} must be integers')
        _require(1<=item['id']<=253,f'Motor id for {name} must be between 1 and 253')
        _require(item['id'] not in motor_ids,'Calibration motor IDs must be unique')
        _require(item['drive_mode']==0,f'Drive mode for {name} must be 0 for the supported Feetech calibration')
        _require(-2047<=item['homing_offset']<=2047,f'Homing offset for {name} must fit signed 11-bit magnitude (−2047…2047)')
        _require(0<=item['range_min']<item['range_max']<=4095,f'Calibration range for {name} must satisfy 0 ≤ minimum < maximum ≤ 4095')
        motor_ids.add(item['id']);normalized[name]={key:item[key] for key in sorted(CALIBRATION_FIELDS)}
    arm=None
    if 'arm_id' in value:
        _require(isinstance(value['arm_id'],str) and value['arm_id'] in arms,'Select an explicitly configured arm_id')
        arm=arms[value['arm_id']]
    if 'joint_map' in value or 'gripper_source' in value:
        mapping=value.get('joint_map');gripper=value.get('gripper_source')
    else:
        _require(arm is not None,'arm_id is required when importing a standard LeRobot calibration')
        mapping={joint['name']:joint['name'] for joint in arm['joints']}
        gripper=arm.get('gripper',{}).get('name','gripper')
    _require(isinstance(mapping,dict) and 1<=len(mapping)<=MAX_BODY_JOINTS,'joint_map must map 1–32 leader joints to follower joints')
    _require(all(isinstance(name,str) and JOINT_NAME.fullmatch(name) is not None for name in (*mapping.keys(),*mapping.values())),
             'Joint mapping names must be valid names of at most 64 characters')
    _require(len(set(mapping.values()))==len(mapping),'joint_map must have unique follower joint names')
    _require(isinstance(gripper,str) and JOINT_NAME.fullmatch(gripper) is not None,'gripper_source must name the calibrated leader gripper')
    _require(gripper not in mapping,'The gripper must be separate from body joint_map entries')
    _require(set(normalized)==set(mapping)|{gripper},'Calibration must cover exactly the mapped body motors and gripper_source')
    if arm is not None:
        _require(set(mapping.values())=={joint['name'] for joint in arm['joints']},'Mapped follower joints must exactly match the selected arm')
    profile={'id':identifier,'label':label.strip(),'motor_model':'sts3215','joint_map':dict(mapping),
             'gripper_source':gripper,'calibration':normalized}
    if 'match' in value:
        match=value['match']
        _require(isinstance(match,dict) and not set(match)-{'vid','pid'},'match accepts only optional USB vid/pid hints')
        _require(all(type(number) is int and 0<=number<=65535 for number in match.values()),'USB vid/pid hints must be integers between 0 and 65535')
        _require('pid' not in match or 'vid' in match,'A USB pid hint also requires its vid hint')
        if match:profile['match']=dict(match)
    encoded=json.dumps(profile,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')
    if len(encoded)>MAX_PROFILE_BYTES:raise ProfileError('Leader profile exceeds 16 KiB','PROFILE_TOO_LARGE',413)
    return profile


class BrowserLeaderProfiles:
    def __init__(self,root,arms):
        self.root=Path(root).resolve() if root and Path(root).is_absolute() else None
        self.arms={arm['id']:{'joints':[{'name':joint['name']} for joint in arm['joints']],
                             'gripper':{'name':arm.get('gripper',{}).get('name','gripper')}} for arm in arms}
        self.lock=threading.RLock()

    def _folder(self,create=False):
        if self.root is None or not self.root.is_dir():
            raise ProfileError('Station profile storage root is not configured','PROFILE_STORAGE_UNAVAILABLE',503)
        local=self.root/'local';folder=local/'browser-leader-profiles'
        for path in (local,folder):
            if path.is_symlink():raise ProfileError('Profile storage must use its configured private directory','PROFILE_STORAGE_UNAVAILABLE',503)
            if create:path.mkdir(mode=0o700,exist_ok=True)
            elif not path.exists():return None
            if not path.is_dir():raise ProfileError('Profile storage directory is unavailable','PROFILE_STORAGE_UNAVAILABLE',503)
        return folder

    def _files(self,folder):
        files=[]
        with os.scandir(folder) as entries:
            for index,entry in enumerate(entries):
                if index>=MAX_PROFILES*2:raise ProfileError('Profile storage contains too many entries','PROFILE_STORAGE_FULL',507)
                if entry.name.endswith('.json'):
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        raise ProfileError('Stored profile entry must be a regular file','PROFILE_STORAGE_INVALID',503)
                    files.append(Path(entry.path))
                    if len(files)>MAX_PROFILES:raise ProfileError('Profile storage exceeds its 64-profile limit','PROFILE_STORAGE_FULL',507)
        return files

    def _read(self,path):
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):raise ProfileError('Stored profile is not a regular file','PROFILE_STORAGE_INVALID',503)
            raw=stream.read(MAX_PROFILE_BYTES+1)
        if len(raw)>MAX_PROFILE_BYTES:raise ProfileError('Stored profile exceeds 16 KiB','PROFILE_STORAGE_INVALID',503)
        try:
            document=decode_json(raw)
            _require(isinstance(document,dict) and 'arm_id' not in document and 'joint_map' in document and 'gripper_source' in document,
                     'Stored profiles must use the canonical browser schema')
            profile=normalize_profile(document,self.arms)
            _require(profile['id']==path.stem,'Stored profile ID does not match its filename')
            return profile
        except ProfileError as error:
            raise ProfileError('Stored leader profile is invalid: '+str(error),'PROFILE_STORAGE_INVALID',503) from error

    @contextmanager
    def _writer(self,folder):
        fd=os.open(folder/'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)

    def list_profiles(self):
        with self.lock:
            folder=self._folder()
            if folder is None:return []
            return sorted((self._read(path) for path in self._files(folder)),key=lambda row:(row['label'].casefold(),row['id']))

    def import_profile(self,document):
        profile=normalize_profile(document,self.arms)
        encoded=(json.dumps(profile,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode('utf-8')
        if len(encoded)>MAX_PROFILE_BYTES:raise ProfileError('Leader profile exceeds 16 KiB','PROFILE_TOO_LARGE',413)
        with self.lock:
            folder=self._folder(create=True)
            with self._writer(folder):
                target=folder/(profile['id']+'.json')
                if target.exists() or target.is_symlink():
                    if self._read(target)!=profile:
                        raise ProfileError('This profile ID already has different calibration or settings; choose another ID','PROFILE_ID_CONFLICT',409)
                    return profile
                if len(self._files(folder))>=MAX_PROFILES:
                    raise ProfileError('Profile storage is full (64 profiles)','PROFILE_STORAGE_FULL',507)
                descriptor,temporary=tempfile.mkstemp(prefix='.import-',suffix='.tmp',dir=folder)
                try:
                    with os.fdopen(descriptor,'wb') as stream:
                        stream.write(encoded);stream.flush();os.fsync(stream.fileno())
                    try:
                        os.link(temporary,target)
                    except OSError as error:
                        if error.errno not in (errno.EPERM,errno.EOPNOTSUPP,errno.ENOTSUP,errno.ENOSYS):raise
                        # exFAT cannot create hard links. All application writers
                        # hold this directory's interprocess lock; check again
                        # and atomically publish within the same directory.
                        if target.exists() or target.is_symlink():
                            raise FileExistsError(errno.EEXIST,'Profile already exists') from error
                        os.replace(temporary,target)
                    directory_fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY)
                    try:os.fsync(directory_fd)
                    finally:os.close(directory_fd)
                except FileExistsError:
                    if self._read(target)!=profile:
                        raise ProfileError('This profile ID was imported concurrently; choose another ID','PROFILE_ID_CONFLICT',409)
                finally:Path(temporary).unlink(missing_ok=True)
                return profile


def mount_browser_leader_routes(app,config,*,root):
    """Mount under the caller's existing authentication/origin middleware."""
    from aiohttp import web
    store=BrowserLeaderProfiles(root,config.get('arms',[]))
    def error_response(error):
        if isinstance(error,ProfileError):
            return web.json_response({'error':str(error)[:512],'code':error.code},status=error.status,headers={'Cache-Control':'no-store'})
        detail=getattr(error,'strerror',None)
        message='Leader profile storage is unavailable'+(': '+detail[:192] if detail else '')
        return web.json_response({'error':message,'code':'PROFILE_STORAGE_UNAVAILABLE'},status=503,headers={'Cache-Control':'no-store'})
    async def listing(request):
        try:return web.json_response({'schema_version':1,'profiles':await asyncio.to_thread(store.list_profiles)},headers={'Cache-Control':'no-store'})
        except (ProfileError,OSError) as error:return error_response(error)
    async def importing(request):
        try:
            if request.content_type!='application/json':raise ProfileError('Use application/json for a leader profile')
            if request.content_length is not None and request.content_length>MAX_PROFILE_BYTES:
                raise ProfileError('Leader profile exceeds 16 KiB','PROFILE_TOO_LARGE',413)
            raw=await request.read()
            if len(raw)>MAX_PROFILE_BYTES:raise ProfileError('Leader profile exceeds 16 KiB','PROFILE_TOO_LARGE',413)
            profile=await asyncio.to_thread(store.import_profile,decode_json(raw))
            return web.json_response({'profile':profile},headers={'Cache-Control':'no-store'})
        except (ProfileError,OSError) as error:return error_response(error)
    app.router.add_get('/api/leader-profiles',listing)
    app.router.add_post('/api/leader-profiles',importing)
    return store
