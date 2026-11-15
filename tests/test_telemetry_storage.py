from pathlib import Path

import pytest
import yaml

from soarm_station.config import load,validate_telemetry_storage


def profile(tmp_path,monitoring):
    path=tmp_path/'profiles'/'robot.yaml';path.parent.mkdir()
    path.write_text(yaml.safe_dump({'schema_version':1,'arms':[],'cameras':[],
        'database':'../ssd/history/telemetry.sqlite3','monitoring':monitoring}))
    return path


def test_required_mount_resolves_against_profile_without_checking_live_mount(tmp_path,monkeypatch):
    def no_mount_query(path):raise AssertionError('Global config loading must not depend on the telemetry disk')
    monkeypatch.setattr('soarm_station.config.os.path.ismount',no_mount_query)
    config=load(profile(tmp_path,{'storage_mount':'../ssd'}))
    assert config['monitoring']['storage_mount']==str(tmp_path/'ssd')
    assert config['database']==str(tmp_path/'ssd/history/telemetry.sqlite3')
    assert not (tmp_path/'ssd').exists()


@pytest.mark.parametrize('value',['',False,10,[]])
def test_required_mount_rejects_invalid_configuration_types(tmp_path,value):
    with pytest.raises(ValueError,match='storage_mount'):load(profile(tmp_path,{'storage_mount':value}))


@pytest.mark.parametrize('monitoring',[{}, {'storage_mount':None}])
def test_optional_mount_preserves_legacy_storage_behavior(tmp_path,monkeypatch,monitoring):
    config=load(profile(tmp_path,monitoring))
    monkeypatch.setattr('soarm_station.config.os.path.ismount',lambda path:pytest.fail('No mount guard configured'))
    validate_telemetry_storage(config)
    assert not Path(config['database']).parent.exists()


def test_missing_mount_rejects_before_creating_database_directories(tmp_path,monkeypatch):
    config=load(profile(tmp_path,{'storage_mount':'../ssd'}));seen=[]
    monkeypatch.setattr('soarm_station.config.os.path.ismount',lambda path:seen.append(path) or False)
    with pytest.raises(OSError,match='storage mount is not mounted'):validate_telemetry_storage(config)
    assert seen==[tmp_path/'ssd']
    assert not (tmp_path/'ssd').exists()


def test_mounted_directory_allows_only_contained_database_paths(tmp_path,monkeypatch):
    mount=tmp_path/'ssd';mount.mkdir()
    monkeypatch.setattr('soarm_station.config.os.path.ismount',lambda path:path==mount)
    config={'database':str(mount/'history/data.sqlite3'),'monitoring':{'storage_mount':str(mount)}}
    validate_telemetry_storage(config)
    assert not (mount/'history').exists()
    for database in (mount,tmp_path/'elsewhere.db',tmp_path/'ssd-other/data.sqlite3'):
        with pytest.raises(ValueError,match='must be under'):
            validate_telemetry_storage({**config,'database':str(database)})


def test_symlink_escape_cannot_place_the_database_back_on_another_filesystem(tmp_path,monkeypatch):
    mount=tmp_path/'ssd';mount.mkdir();outside=tmp_path/'sd';outside.mkdir()
    (mount/'redirect').symlink_to(outside,target_is_directory=True)
    monkeypatch.setattr('soarm_station.config.os.path.ismount',lambda path:True)
    with pytest.raises(ValueError,match='must be under'):
        validate_telemetry_storage({'database':str(mount/'redirect/data.sqlite3'),
            'monitoring':{'storage_mount':str(mount)}})
    assert not (outside/'data.sqlite3').exists()
