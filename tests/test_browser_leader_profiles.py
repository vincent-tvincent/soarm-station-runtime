import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
import errno
import importlib.util
import json
import os
from pathlib import Path
import stat

from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
import pytest

from soarm_station.browser_leader_profiles import (BrowserLeaderProfiles,ProfileError,
    decode_json,mount_browser_leader_routes,normalize_profile)
from soarm_station.web_access import dashboard_access_middleware


ARMS=[{'id':'arm1','joints':[{'name':'pan'},{'name':'lift'}],'gripper':{'name':'gripper'}}]


def calibration(names=('pan','lift','gripper')):
    return {name:{'id':index+1,'drive_mode':0,'homing_offset':(-2047,0,2047)[index],
                  'range_min':0,'range_max':4095} for index,name in enumerate(names)}


def imported(identifier='leader'):
    return {'id':identifier,'label':'Desk leader','arm_id':'arm1','calibration':calibration()}


def canonical(identifier='leader'):
    return {'id':identifier,'label':'Mapped leader','motor_model':'sts3215',
            'joint_map':{'leader_pan':'pan','leader_lift':'lift'},'gripper_source':'jaw',
            'calibration':calibration(('leader_pan','leader_lift','jaw'))}


def test_standard_import_uses_explicit_arm_and_preserves_every_calibration_value(tmp_path):
    store=BrowserLeaderProfiles(tmp_path,ARMS);body=imported();before=copy.deepcopy(body)
    assert store.list_profiles()==[] and not (tmp_path/'local').exists()
    profile=store.import_profile(body)
    assert profile=={'id':'leader','label':'Desk leader','motor_model':'sts3215',
        'joint_map':{'pan':'pan','lift':'lift'},'gripper_source':'gripper','calibration':body['calibration']}
    assert body==before and store.list_profiles()==[profile]
    path=tmp_path/'local/browser-leader-profiles/leader.json'
    assert json.loads(path.read_text())==profile
    assert stat.S_IMODE(path.stat().st_mode)==0o600
    assert stat.S_IMODE(path.parent.stat().st_mode)==0o700
    assert not any(key in profile for key in ('arm_id','port','serial','credentials'))


def test_canonical_mappings_and_only_explicit_usb_hints_survive(tmp_path):
    store=BrowserLeaderProfiles(tmp_path,ARMS);profile=canonical()
    result=store.import_profile(profile)
    assert result==profile and 'match' not in result
    hinted=canonical('usb');hinted['match']={'vid':0x1a86,'pid':0x55d3}
    assert store.import_profile(hinted)==hinted


def test_identical_import_is_idempotent_and_conflict_never_replaces_working_file(tmp_path):
    store=BrowserLeaderProfiles(tmp_path,ARMS);profile=store.import_profile(imported())
    path=tmp_path/'local/browser-leader-profiles/leader.json';before=(path.read_bytes(),path.stat().st_mtime_ns)
    assert store.import_profile(profile)==profile
    assert (path.read_bytes(),path.stat().st_mtime_ns)==before
    changed=imported();changed['calibration']['pan']['homing_offset']+=1
    with pytest.raises(ProfileError) as error:store.import_profile(changed)
    assert error.value.status==409 and 'choose another' in str(error.value)
    assert (path.read_bytes(),path.stat().st_mtime_ns)==before


@pytest.mark.parametrize('mutate',[
    lambda p:p.update(id='../outside'),lambda p:p.update(id='x'*65),lambda p:p.update(label='   '),
    lambda p:p.update(motor_model='unsupported'),lambda p:p.update(arm_id='missing'),
    lambda p:p.update(port='/dev/private'),lambda p:p.update(calibration='/private/calibration.json'),
    lambda p:p.update(match={'serial':'private-device'}),lambda p:p.update(match={'vid':True}),
    lambda p:p.update(match={'pid':0x55d3}),
    lambda p:p.update(match={'vid':65536}),lambda p:p['calibration']['pan'].update(id=0),
    lambda p:p['calibration']['pan'].update(id=254),lambda p:p['calibration']['pan'].update(id=2),
    lambda p:p['calibration']['pan'].update(drive_mode=1),
    lambda p:p['calibration']['pan'].update(homing_offset=-2048),
    lambda p:p['calibration']['pan'].update(homing_offset=2048),
    lambda p:p['calibration']['pan'].update(range_min=-1),
    lambda p:p['calibration']['pan'].update(range_max=4096),
    lambda p:p['calibration']['pan'].update(range_min=4095),
    lambda p:p['calibration']['pan'].update(range_max=True),
    lambda p:p['calibration']['pan'].update(token='private'),
    lambda p:p['calibration'].pop('gripper'),
])
def test_invalid_import_never_creates_private_storage(tmp_path,mutate):
    store=BrowserLeaderProfiles(tmp_path,ARMS);profile=imported();mutate(profile)
    with pytest.raises(ProfileError) as error:store.import_profile(profile)
    assert error.value.status==400
    assert not (tmp_path/'local').exists()


@pytest.mark.parametrize('mutate',[
    lambda p:p['joint_map'].update(leader_lift='pan'),
    lambda p:p.update(gripper_source='leader_pan'),
    lambda p:p['joint_map'].update(unknown='pan'),
    lambda p:p['joint_map'].update(leader_pan='../pan'),
    lambda p:p.update(joint_map={}),
])
def test_invalid_canonical_mapping_is_rejected(tmp_path,mutate):
    profile=canonical();mutate(profile)
    with pytest.raises(ProfileError):BrowserLeaderProfiles(tmp_path,ARMS).import_profile(profile)


def test_standard_import_never_guesses_an_arm_or_joint_mapping(tmp_path):
    store=BrowserLeaderProfiles(tmp_path,ARMS);profile=imported();profile.pop('arm_id')
    with pytest.raises(ProfileError,match='arm_id is required'):store.import_profile(profile)
    profile=imported();profile['calibration']=calibration(('other','lift','gripper'))
    with pytest.raises(ProfileError,match='exactly'):store.import_profile(profile)


def test_missing_root_has_no_cwd_or_public_directory_fallback(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    for root in (None,'','relative-root'):
        store=BrowserLeaderProfiles(root,ARMS)
        with pytest.raises(ProfileError) as error:store.list_profiles()
        assert error.value.status==503
        with pytest.raises(ProfileError):store.import_profile(imported())
    assert list(tmp_path.iterdir())==[]


def test_profile_capacity_is_bounded_but_idempotent_imports_still_succeed(tmp_path,monkeypatch):
    monkeypatch.setattr('soarm_station.browser_leader_profiles.MAX_PROFILES',2)
    store=BrowserLeaderProfiles(tmp_path,ARMS)
    one=store.import_profile(imported('one'));store.import_profile(imported('two'))
    with pytest.raises(ProfileError) as error:store.import_profile(imported('three'))
    assert error.value.status==507
    assert store.import_profile(one)==one and len(store.list_profiles())==2


def test_publish_failure_leaves_no_selectable_partial_file(tmp_path,monkeypatch):
    store=BrowserLeaderProfiles(tmp_path,ARMS)
    def failed(*args,**kwargs):raise OSError('simulated atomic publication failure')
    monkeypatch.setattr('soarm_station.browser_leader_profiles.os.link',failed)
    with pytest.raises(OSError):store.import_profile(imported())
    folder=tmp_path/'local/browser-leader-profiles'
    assert list(folder.glob('*.json'))==[] and list(folder.glob('.import-*'))==[]
    assert store.list_profiles()==[]


@pytest.mark.parametrize('code',[errno.EOPNOTSUPP,errno.EPERM])
def test_filesystem_without_hardlinks_still_publishes_atomically_and_preserves_conflicts(tmp_path,monkeypatch,code):
    def unsupported(*args,**kwargs):raise OSError(code,'Hard links are unsupported')
    monkeypatch.setattr('soarm_station.browser_leader_profiles.os.link',unsupported)
    store=BrowserLeaderProfiles(tmp_path,ARMS);profile=store.import_profile(imported())
    path=tmp_path/'local/browser-leader-profiles/leader.json';before=path.read_bytes()
    assert store.list_profiles()==[profile] and store.import_profile(profile)==profile
    changed=imported();changed['calibration']['pan']['homing_offset']=123
    with pytest.raises(ProfileError) as error:store.import_profile(changed)
    assert error.value.status==409 and path.read_bytes()==before
    assert list(path.parent.glob('.import-*'))==[]


def test_concurrent_import_cannot_clobber_another_profiles_calibration(tmp_path):
    def importing(offset):
        profile=imported();profile['calibration']['pan']['homing_offset']=offset
        try:return BrowserLeaderProfiles(tmp_path,ARMS).import_profile(profile)
        except ProfileError as error:return error.status
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(importing,(-10,10)))
    assert sum(isinstance(result,dict) for result in results)==1 and 409 in results
    saved=BrowserLeaderProfiles(tmp_path,ARMS).list_profiles()
    assert len(saved)==1 and saved[0] in results


def test_symlink_storage_is_rejected_without_touching_its_destination(tmp_path):
    public=tmp_path/'public';public.mkdir();(tmp_path/'local').symlink_to(public,target_is_directory=True)
    store=BrowserLeaderProfiles(tmp_path,ARMS)
    with pytest.raises(ProfileError):store.import_profile(imported())
    assert list(public.iterdir())==[]


def test_symlink_profile_cannot_expose_another_private_file(tmp_path):
    store=BrowserLeaderProfiles(tmp_path,ARMS);store.import_profile(imported())
    private=tmp_path/'private.json';private.write_text('{"token":"do-not-expose"}')
    (tmp_path/'local/browser-leader-profiles/unsafe.json').symlink_to(private)
    with pytest.raises(ProfileError) as error:store.list_profiles()
    assert error.value.status==503 and 'do-not-expose' not in str(error.value)
    assert private.read_text()=='{"token":"do-not-expose"}'


def test_duplicate_json_keys_are_not_silently_overwritten():
    with pytest.raises(ProfileError,match='Duplicate JSON field'):decode_json('{"id":"one","id":"two"}')


def test_api_authentication_import_idempotence_conflict_and_readable_validation(tmp_path):
    async def check():
        token='test-profile-token-'+'x'*24
        app=web.Application(middlewares=[dashboard_access_middleware({'trusted_network_access':False},token)])
        mount_browser_leader_routes(app,{'arms':ARMS},root=tmp_path)
        async with TestClient(TestServer(app)) as client:
            assert (await client.get('/api/leader-profiles')).status==401
            assert (await client.post('/api/leader-profiles',json=imported())).status==401
            assert not (tmp_path/'local').exists()
            auth={'Authorization':'Bearer '+token}
            response=await client.get('/api/leader-profiles',headers=auth)
            assert await response.json()=={'schema_version':1,'profiles':[]}
            assert response.headers['Cache-Control']=='no-store'
            for _ in range(2):
                response=await client.post('/api/leader-profiles',json=imported(),headers=auth)
                assert response.status==200
                profile=(await response.json())['profile']
                assert profile['joint_map']=={'pan':'pan','lift':'lift'}
            assert (await (await client.get('/api/leader-profiles',headers=auth)).json())['profiles']==[profile]
            changed=imported();changed['label']='Another calibration'
            response=await client.post('/api/leader-profiles',json=changed,headers=auth)
            assert response.status==409 and (await response.json())['code']=='PROFILE_ID_CONFLICT'
            response=await client.post('/api/leader-profiles',json={**imported(),'serial':'private'},headers=auth)
            assert response.status==400 and (await response.json())['error']
            response=await client.post('/api/leader-profiles',json=imported(),headers={**auth,'Origin':'https://other.invalid'})
            assert response.status==403
    asyncio.run(check())


def test_api_large_or_invalid_json_and_unconfigured_store_are_explicit(tmp_path):
    async def check():
        app=web.Application(client_max_size=262144)
        mount_browser_leader_routes(app,{'arms':ARMS},root=tmp_path)
        async with TestClient(TestServer(app)) as client:
            response=await client.post('/api/leader-profiles',data=b'x'*16385,headers={'Content-Type':'application/json'})
            assert response.status==413 and (await response.json())['code']=='PROFILE_TOO_LARGE'
            for raw in ('not-json','{"id":"one","id":"two"}'):
                response=await client.post('/api/leader-profiles',data=raw,headers={'Content-Type':'application/json'})
                assert response.status==400 and (await response.json())['error']
        app=web.Application();mount_browser_leader_routes(app,{'arms':ARMS},root=None)
        async with TestClient(TestServer(app)) as client:
            response=await client.get('/api/leader-profiles')
            assert response.status==503 and (await response.json())['code']=='PROFILE_STORAGE_UNAVAILABLE'
    asyncio.run(check())


def test_preview_profile_api_uses_disposable_store_without_real_local_directory(tmp_path,monkeypatch):
    import soarm_station.browser_leader_profiles as profiles
    repository=Path(__file__).resolve().parents[1]
    spec=importlib.util.spec_from_file_location('preview_browser_profiles',repository/'scripts/preview-dashboard.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    preview=module.Preview(repository/'tests/dashboard-fixture.json',repository/'web',tmp_path,'station')
    fake_checkout=tmp_path/'checkout';fake_checkout.mkdir()
    monkeypatch.setattr(module,'ROOT',fake_checkout)
    stores=[];original=profiles.mount_browser_leader_routes
    def mount(*args,**kwargs):
        store=original(*args,**kwargs);stores.append(store);return store
    monkeypatch.setattr(profiles,'mount_browser_leader_routes',mount)
    async def check():
        app=module.build_app(preview)
        async with TestClient(TestServer(app)) as client:
            response=await client.post('/api/leader-profiles',json=canonical())
            assert response.status==200
            assert (await (await client.get('/api/leader-profiles')).json())['profiles']==[canonical()]
            assert (stores[0].root/'local/browser-leader-profiles/leader.json').is_file()
            assert not (fake_checkout/'local').exists()
        assert not stores[0].root.exists()
    asyncio.run(check())
