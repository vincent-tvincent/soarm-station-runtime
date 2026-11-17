import importlib.machinery
import importlib.util
import os
from pathlib import Path

import pytest


def module():
    path=Path(__file__).resolve().parents[1]/'scripts/protect-telemetry-mount'
    loader=importlib.machinery.SourceFileLoader('protect_telemetry_mount',str(path))
    spec=importlib.util.spec_from_loader(loader.name,loader)
    value=importlib.util.module_from_spec(spec);loader.exec_module(value)
    return value


def setup(tmp_path,monkeypatch):
    helper=module();parent=tmp_path/'mnt';parent.mkdir();mount=parent/'ssd';mount.mkdir()
    calls=[];protected=[];view=[]
    monkeypatch.setattr(helper.os,'readlink',lambda path:'private' if path=='/proc/self/ns/mnt' else 'parent')
    monkeypatch.setattr(helper.os.path,'ismount',lambda path:Path(path)==mount)
    def run(arguments):
        calls.append(arguments)
        if '--bind' in arguments:
            view.append(Path(arguments[-1]));(view[-1]/mount.name).mkdir()
        if arguments[0]=='umount':
            (Path(arguments[-1])/mount.name).rmdir()
    monkeypatch.setattr(helper,'protect_directory',lambda fd:protected.append(helper.os.fstat(fd).st_ino))
    return helper,mount,calls,protected,view,run


def test_administrator_helper_changes_only_nonrecursive_underlay_view(tmp_path,monkeypatch):
    helper,mount,calls,protected,view,run=setup(tmp_path,monkeypatch)
    before=helper.identity(mount)
    result=helper.protect_in_namespace(mount,run=run,temp_parent=str(tmp_path))
    assert result['mounted_filesystem_unchanged'] and result['underlying_mode']=='0700'
    assert helper.identity(mount)==before and protected and protected[0]!=mount.stat().st_ino
    assert calls[0]==['mount','--make-rprivate','/']
    assert calls[1][:3]==['mount','--bind','--'] and '--rbind' not in calls[1]
    assert calls[-1]==['umount','--',str(view[0])]
    assert not view[0].parent.exists()


def test_administrator_helper_refuses_shared_namespace_before_any_mount(tmp_path,monkeypatch):
    helper,mount,calls,protected,view,run=setup(tmp_path,monkeypatch)
    monkeypatch.setattr(helper.os,'readlink',lambda path:'same')
    with pytest.raises(RuntimeError,match='private mount namespace'):
        helper.protect_in_namespace(mount,run=run,temp_parent=str(tmp_path))
    assert not calls and not protected


def test_failed_permission_change_still_unmounts_only_the_private_view(tmp_path,monkeypatch):
    helper,mount,calls,protected,view,run=setup(tmp_path,monkeypatch)
    before=helper.identity(mount)
    def fail(fd):raise OSError('simulated permission failure')
    monkeypatch.setattr(helper,'protect_directory',fail)
    with pytest.raises(OSError,match='simulated'):
        helper.protect_in_namespace(mount,run=run,temp_parent=str(tmp_path))
    assert calls[-1]==['umount','--',str(view[0])] and helper.identity(mount)==before
    assert not view[0].parent.exists()


def test_failed_unmount_never_recursively_removes_the_bound_tree(tmp_path,monkeypatch):
    helper,mount,calls,protected,view,run=setup(tmp_path,monkeypatch)
    def fail_unmount(arguments):
        if arguments[0]=='umount':raise OSError('simulated busy view')
        return run(arguments)
    with pytest.raises(OSError,match='busy view'):
        helper.protect_in_namespace(mount,run=fail_unmount,temp_parent=str(tmp_path))
    assert (view[0]/mount.name).is_dir() and mount.is_dir()
