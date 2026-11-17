"""Policy preparation tests using fake feedback/resources and no native SDK."""
import copy
import json
import threading
import time
import tempfile
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from soarm_station.model_runtime import ModelSupervisor
from soarm_station.policy_common import PolicyError
from soarm_station.policy_runtime import PolicyRuntime, execution_key


class ForbiddenBackend:
    def __init__(self):
        self.calls = []

    def send(self, *args, **kwargs):
        self.calls.append("send")
        raise AssertionError("Preparation must never send a motor command")

    def hold(self, *args, **kwargs):
        self.calls.append("hold")
        raise AssertionError("Preparation must never issue a physical hold")


class FakeSession:
    def __init__(self):
        self.paused = True
        self.terminally_failed = False
        self.preparation_details = {"native_acknowledged": True, "state": "paused"}
        self.closes = 0
        self.resets = 0
        self.polls = 0

    def pause(self):
        self.paused = True

    def close(self):
        self.closes += 1

    def reset_task(self, *args):
        self.resets += 1
        raise AssertionError("Preparation cannot start a native task")

    def poll(self, *args):
        self.polls += 1
        raise AssertionError("Preparation cannot request model actions")


class FakeArm:
    def __init__(self, system):
        self.id = "arm-one"
        self.lock = threading.RLock()
        self.op = None
        self.hold_pending = False
        self.policy_preparation = None
        self.permission = False
        self.principal = ""
        self.q = {"joint": .1}
        self.gripper = 50.
        self.model = None
        self.model_state = "IDLE"
        self.model_reason = ""
        self.generation = 7
        self.system = system
        self.config = system["arms"][0]
        self.backend = ForbiddenBackend()
        self.permission_calls = []

    def set_permission(self, *args, **kwargs):
        self.permission_calls.append((args, kwargs))
        raise AssertionError("Preparation cannot grant robot permission")

    def _hold(self, *args, **kwargs):
        self.backend.calls.append("owner_hold")
        raise AssertionError("Preparation cannot interrupt the arm owner")


class Harness:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"profiles": {}, "arms": [{"id": "arm-one", "joints": [{"name": "joint"}], "gripper": {"name": "gripper"}, "calibration_revision": "calibration-one"}], "cameras": [{"id": "global"}], "policy_management": {"cache_db": str(Path(self.temp.name) / "policy.sqlite3"), "connections": {"private": {"zenoh_mode": "client", "zenoh_config_path": "/configured/startup.json5"}}}}
        self.arm = FakeArm(self.config)
        self.camera = SimpleNamespace(snapshot=lambda: {"age_ms": 0, "error": None})
        self.owner = SimpleNamespace(config=self.config, arms={self.arm.id: self.arm}, cameras={"global": self.camera}, boot="boot-one")
        self.loads = []
        self.sessions = []
        self.inspections = []
        self.inspector_result = {"admitted": False, "descriptor": {"ready": True, "artifact_identity": "sha256:artifact-one", "instance_id": "instance-one", "category": "smolvla", "capabilities": {"features": [], "action_interval": .1, "modes": ["chunk"]}}}
        self.bridge = PolicyRuntime(self.owner, loader=self.load, inspector=self.inspect)

    def load(self, profile):
        self.loads.append(copy.deepcopy(profile))
        session = FakeSession()
        self.sessions.append(session)
        return session

    def inspect(self, execution, settings, connection):
        self.inspections.append(copy.deepcopy((execution, settings, connection)))
        return copy.deepcopy(self.inspector_result)

    def document(self, *, remote=False):
        features = [{"name": "joint-position", "joint": "joint", "unit": "radian"}, {"name": "gripper-position", "joint": "gripper", "unit": "percent"}]
        execution = {"kind": "remote", "endpoint": "tcp/private-host:7447", "deployment": "trained-deployment", "expected_artifact": "sha256:artifact-one", "connection_profile": "private"} if remote else {"kind": "local", "device": "cpu"}
        result = {"schema_version": 1, "id": "pick-one", "name": "Pick one", "execution": execution, "bindings": {"arm": self.arm.id, "cameras": {}}, "contract": {"state_features": copy.deepcopy(features), "action_features": copy.deepcopy(features), "action_fps": 10., "language_conditioned": False, "calibration_revision": "calibration-one"}}
        if not remote:
            result["model"] = {"source_id": "models", "relative_path": "trained", "artifact_revision": "sha256:artifact-one"}
        return result

    def profile(self):
        contract = self.document()["contract"]
        return {"arm_id": self.arm.id, "backend": "local", "device": "cpu", "state_features": contract["state_features"], "action_features": contract["action_features"], "action_fps": 10., "language_conditioned": False, "calibration_revision": "calibration-one", "cameras": {}, "startup_timeout_s": .25, "_artifact": {"root": "/unused/model", "checkpoint_path": "/unused/model/pretrained_model", "artifact_revision": "sha256:artifact-one", "files": [{"path": "model.safetensors", "size": 12, "sha256": "a" * 64}], "category": "smolvla"}}

    def wait(self, job):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.bridge.job(job["id"])
            if result["state"] in ("complete", "failed"):
                return result
            time.sleep(.005)
        pytest.fail("Policy preparation did not finish")

    def prepare(self, document=None, revision="sha256:profile-one", profile=None, mode="idle"):
        return self.bridge.prepare(document or self.document(), revision, self.profile() if profile is None else profile, mode)

    def ready(self, **kwargs):
        result = self.wait(self.prepare(**kwargs))
        assert result["state"] == "complete", result
        return result["result"]["applied"]

    def assert_no_motion(self):
        assert self.arm.backend.calls == []
        assert self.arm.permission_calls == []
        assert self.arm.permission is False
        assert self.arm.principal == ""
        assert all(session.resets == session.polls == 0 for session in self.sessions)

    def close(self):
        self.bridge.close()
        self.bridge.executor.shutdown(wait=True, cancel_futures=True)
        self.temp.cleanup()


@pytest.fixture
def managed():
    harness = Harness()
    yield harness
    harness.close()


def test_prepare_installs_paused_session_without_permission_or_dispatch(managed):
    applied = managed.ready()
    arm = managed.arm
    assert arm.model.session is managed.sessions[0]
    assert arm.model.session.paused is True
    registered = managed.config["profiles"][applied["runtime_profile_id"]]
    assert arm.model.profile_key == execution_key(registered)
    assert "files" not in registered["_artifact"]
    pin = Path(registered["_artifact"]["inventory_path"])
    assert pin.is_relative_to(Path(managed.temp.name).resolve())
    assert json.loads(pin.read_text())["files"] == managed.profile()["_artifact"]["files"]
    assert arm.model.needs_reset is True
    assert arm.op is None
    assert arm.policy_preparation is None
    assert managed.bridge.snapshot()["pending"] is None
    assert applied["state"] == "ready"
    assert applied["artifact_revision"] == "sha256:artifact-one"
    assert applied["runtime_profile_id"] in managed.config["profiles"]
    managed.assert_no_motion()


def test_identical_execution_reuses_prepared_session_without_reloading(managed):
    first = managed.ready()
    session = managed.arm.model.session
    second = managed.ready(revision="sha256:profile-two")
    assert managed.arm.model.session is session
    assert len(managed.loads) == 1
    assert session.closes == 0
    assert first["runtime_profile_id"] != second["runtime_profile_id"]
    managed.assert_no_motion()


def test_cold_switch_releases_idle_session_before_loading_replacement(managed):
    first = managed.ready()
    old = managed.arm.model.session
    profile = managed.profile()
    profile["device"] = "other-test-device"
    def load(profile):
        assert old.closes == 1
        return Harness.load(managed, profile)
    managed.bridge.loader = load
    second = managed.ready(profile=profile, revision="sha256:profile-two")
    assert old.closes == 1
    assert managed.arm.model.session is managed.sessions[-1]
    assert first["runtime_profile_id"] in managed.config["profiles"]
    assert second["runtime_profile_id"] in managed.config["profiles"]
    managed.assert_no_motion()


def test_terminally_failed_session_is_replaced_even_for_same_profile(managed):
    managed.ready()
    old = managed.arm.model.session
    old.terminally_failed = True
    managed.ready()
    assert len(managed.loads) == 2
    assert old.closes == 1
    assert managed.arm.model.session is not old
    managed.assert_no_motion()


@pytest.mark.parametrize("operation,hold", [({"operation_type": "MODEL", "operation_id": "active-model"}, False), ({"operation_type": "DIRECT", "operation_id": "active-direct"}, False), (None, True)])
def test_idle_prepare_rejects_active_operation_or_unconfirmed_hold(managed, operation, hold):
    managed.arm.op = operation
    managed.arm.hold_pending = hold
    with pytest.raises(PolicyError) as failure:
        managed.prepare()
    assert failure.value.code == "ARM_BUSY"
    assert managed.bridge.pending == {}
    assert managed.arm.policy_preparation is None
    assert managed.loads == []
    assert managed.arm.op == operation
    managed.assert_no_motion()


def test_after_current_waits_for_both_operation_and_hold_completion(managed):
    managed.arm.op = {"operation_type": "MODEL", "operation_id": "running", "profile_id": "legacy"}
    called = threading.Event()
    def loader(profile):
        called.set()
        return Harness.load(managed, profile)
    managed.bridge.loader = loader
    managed.bridge.start()
    job = managed.prepare(mode="after_current")
    assert job["state"] == "queued"
    assert managed.bridge.snapshot()["pending"]["state"] == "after_current"
    assert managed.arm.policy_preparation is None
    assert not called.wait(.15)
    with managed.arm.lock:
        managed.arm.op = None
        managed.arm.hold_pending = True
    assert not called.wait(.15)
    with managed.arm.lock:
        managed.arm.hold_pending = False
    assert called.wait(2)
    assert managed.wait(job)["state"] == "complete"
    assert managed.arm.policy_preparation is None
    assert managed.bridge.snapshot()["pending"] is None
    managed.assert_no_motion()


def test_pending_preparation_prevents_duplicate_jobs_and_core_start(managed):
    entered, released = threading.Event(), threading.Event()
    def loader(profile):
        entered.set()
        assert released.wait(2)
        return Harness.load(managed, profile)
    managed.bridge.loader = loader
    job = managed.prepare()
    try:
        assert entered.wait(2)
        assert managed.arm.policy_preparation == job["id"]
        with pytest.raises(PolicyError) as failure:
            managed.prepare()
        assert failure.value.code == "PREPARATION_BUSY"
        # Exercise only the real owner's early admission guard, without
        # constructing a controller or opening its hardware backend.
        from soarm_station.control import ArmController
        owner = object.__new__(ArmController)
        owner.lock = threading.RLock()
        owner.op = None
        owner.hold_pending = False
        owner.torque_request = None
        owner.policy_preparation = job["id"]
        owner.permission = True
        owner.principal = "operator"
        owner.boot = "boot-test"
        owner.session = "controller-test"
        with pytest.raises(ValueError, match="(?i)prepar|busy"):
            owner.begin({"operation_type": "MODEL", "mode": "INLINE", "principal": "operator", "runtime_boot_id": owner.boot, "controller_session": owner.session})
    finally:
        released.set()
    assert managed.wait(job)["state"] == "complete"
    managed.assert_no_motion()


def test_loader_failure_clears_busy_and_allows_explicit_retry(managed):
    def fail(profile):
        raise RuntimeError("native checkpoint failed to load")
    managed.bridge.loader = fail
    result = managed.wait(managed.prepare())
    assert result["state"] == "failed"
    assert "failed to load" in result["errors"][0]["message"]
    assert managed.arm.policy_preparation is None
    assert managed.bridge.pending == {}
    assert managed.arm.model.session is None
    assert managed.bridge.snapshot()["applied"] is None
    managed.bridge.loader = managed.load
    managed.ready()
    managed.assert_no_motion()


def test_failed_install_closes_unattached_native_session_and_clears_busy(managed, monkeypatch):
    def reject_install(supervisor, profile, session):
        raise RuntimeError("native installation rejected")
    monkeypatch.setattr(ModelSupervisor, "install_prepared", reject_install)
    failed = managed.wait(managed.prepare())
    assert failed["state"] == "failed"
    assert len(managed.sessions) == 1
    assert managed.sessions[0].closes == 1
    assert managed.arm.model.session is None
    assert managed.arm.policy_preparation is None
    assert managed.bridge.pending == {}
    assert managed.bridge.applied == {}
    managed.assert_no_motion()


def test_runtime_closing_during_loader_closes_unattached_result(managed):
    entered, released = threading.Event(), threading.Event()
    def loader(profile):
        entered.set()
        assert released.wait(2)
        return Harness.load(managed, profile)
    managed.bridge.loader = loader
    job = managed.prepare()
    try:
        assert entered.wait(2)
        managed.bridge.close()
    finally:
        released.set()
    failed = managed.wait(job)
    assert failed["state"] == "failed"
    assert failed["errors"][0]["code"] == "RUNTIME_STOPPED"
    assert managed.sessions[0].closes == 1
    assert managed.arm.model.session is None
    assert managed.arm.policy_preparation is None
    managed.assert_no_motion()


def test_prepare_preserves_an_existing_idle_permission_without_regrant(managed):
    managed.arm.permission = True
    managed.arm.principal = "already-authorized-operator"
    managed.ready()
    assert managed.arm.permission is True
    assert managed.arm.principal == "already-authorized-operator"
    assert managed.arm.permission_calls == []
    assert managed.arm.backend.calls == []
    assert managed.arm.op is None


def test_failed_cold_switch_marks_released_previous_application_failed(managed):
    old_applied = managed.ready()
    old = managed.arm.model.session
    changed = managed.profile()
    changed["device"] = "different-test-device"
    def fail(profile):
        raise RuntimeError("replacement loader failed")
    managed.bridge.loader = fail
    result = managed.wait(managed.prepare(profile=changed, revision="sha256:profile-two"))
    assert result["state"] == "failed"
    assert old.closes == 1
    assert managed.arm.model.session is None
    applied = managed.bridge.snapshot()["applied"]
    assert applied["runtime_profile_id"] == old_applied["runtime_profile_id"]
    assert applied["state"] == "failed"
    assert managed.arm.policy_preparation is None
    managed.assert_no_motion()


def test_completed_old_native_failure_does_not_poison_new_preparation(managed):
    managed.arm.model = ModelSupervisor(managed.arm)
    previous = Future()
    previous.set_exception(RuntimeError("retired prediction failed"))
    managed.arm.model.job = previous
    managed.ready()
    assert managed.arm.model.job is None
    managed.assert_no_motion()


def test_model_operation_race_is_rejected_before_unloading_idle_weights(managed):
    managed.ready()
    old = managed.arm.model.session
    original_check = managed.bridge._check_inputs
    operation = {"operation_type": "MODEL", "operation_id": "race-winner", "profile_id": "legacy"}
    def active_race(arm, profile):
        original_check(arm, profile)
        with arm.lock:
            arm.op = operation
    managed.bridge._check_inputs = active_race
    changed = managed.profile()
    changed["device"] = "different-test-device"
    failed = managed.wait(managed.prepare(profile=changed))
    assert failed["state"] == "failed"
    assert failed["errors"][0]["code"] == "ARM_BUSY"
    assert managed.arm.model.session is old
    assert old.closes == 0
    assert len(managed.loads) == 1
    assert managed.arm.op == operation
    assert managed.arm.policy_preparation is None
    managed.assert_no_motion()


@pytest.mark.parametrize("feedback,camera", [(False, False), (True, True)])
def test_missing_feedback_or_stale_camera_does_not_load_or_prepare(managed, feedback, camera):
    profile = managed.profile()
    if not feedback:
        managed.arm.q = {}
        expected = "FEEDBACK_UNAVAILABLE"
    if camera:
        profile["cameras"] = {"observation.images.global": "global"}
        managed.camera.snapshot = lambda: {"age_ms": 999, "error": None}
        expected = "CAMERA_UNAVAILABLE"
    failed = managed.wait(managed.prepare(profile=profile))
    assert failed["state"] == "failed"
    assert failed["errors"][0]["code"] == expected
    assert managed.loads == []
    assert managed.arm.policy_preparation is None
    managed.assert_no_motion()


def test_remote_inspection_is_discovery_without_session_admission(managed):
    document = managed.document(remote=True)
    result = managed.wait(managed.bridge.inspect(document["execution"], {"timeout_s": 3}))
    assert result["state"] == "complete"
    assert result["result"]["admitted"] is False
    assert managed.inspections[0][2] == managed.config["policy_management"]["connections"]["private"]
    snapshot = managed.bridge.snapshot()
    assert snapshot["descriptors"][0]["descriptor"]["instance_id"] == "instance-one"
    assert snapshot["applied"] is None
    assert snapshot["runtime_profiles"] == []
    assert managed.config["profiles"] == {}
    assert managed.arm.model is None
    managed.assert_no_motion()


def test_remote_prepare_registers_descriptor_pinned_profile_without_loading_session(managed):
    document = managed.document(remote=True)
    result = managed.wait(managed.bridge.prepare(document, "sha256:remote-profile", None))
    assert result["state"] == "complete", result
    applied = result["result"]["applied"]
    assert applied["session_admitted"] is False
    assert applied["remote_instance"] == "instance-one"
    profile = managed.config["profiles"][applied["runtime_profile_id"]]
    assert profile["backend"] == "remote"
    assert profile["remote"]["instance"] == "instance-one"
    assert profile["remote"]["expected_artifact"] == "sha256:artifact-one"
    assert profile["remote"]["zenoh_mode"] == "client"
    assert managed.arm.model is None
    assert managed.loads == []
    managed.assert_no_motion()


@pytest.mark.parametrize("prepare", [False, True])
def test_remote_inspector_admission_is_rejected(managed, prepare):
    managed.inspector_result["admitted"] = True
    document = managed.document(remote=True)
    job = managed.bridge.prepare(document, "sha256:remote-profile", None) if prepare else managed.bridge.inspect(document["execution"])
    failed = managed.wait(job)
    assert failed["state"] == "failed"
    assert failed["errors"][0]["code"] == "INSPECTION_ADMITTED"
    assert managed.config["profiles"] == {}
    assert managed.bridge.applied == {}
    assert managed.arm.policy_preparation is None
    managed.assert_no_motion()


def test_runtime_profile_ids_and_records_are_immutable_across_apply_and_runs(managed):
    document, profile = managed.document(), managed.profile()
    first = managed.ready(document=document, profile=profile)
    first_id = first["runtime_profile_id"]
    original = copy.deepcopy(managed.config["profiles"][first_id])
    document["name"] = "Changed outside request"
    profile["action_fps"] = 99
    assert managed.config["profiles"][first_id] == original
    request = {"operation_type": "MODEL", "profile_id": first_id}
    identity = managed.bridge.check_begin(request)
    assert identity["profile_revision"] == "sha256:profile-one"
    identity["name"] = "Caller mutation"
    assert managed.bridge.check_begin(request)["name"] == "Pick one"
    managed.bridge.record_begin(managed.arm, request, {"operation_id": "operation-one"})
    changed = managed.profile()
    changed["action_fps"] = 20
    second = managed.ready(profile=changed, revision="sha256:profile-two")
    second_id = second["runtime_profile_id"]
    assert second_id != first_id
    assert managed.config["profiles"][first_id] == original
    assert managed.config["profiles"][second_id]["action_fps"] == 20
    with pytest.raises(PolicyError) as failure:
        managed.bridge.check_begin(request)
    assert failure.value.code == "PROFILE_NOT_APPLIED"
    with managed.arm.lock:
        managed.arm.op = {**request, "operation_id": "operation-one"}
    snapshot = managed.bridge.snapshot()
    assert snapshot["applied"]["runtime_profile_id"] == second_id
    assert snapshot["active_runs"][0]["policy"]["runtime_profile_id"] == first_id
    run = snapshot["recent_runs"][0]
    assert run["operation_id"] == "operation-one"
    assert run["runtime_boot_id"] == "boot-one"
    assert run["policy"]["profile_revision"] == "sha256:profile-one"
    assert run["policy"]["artifact_revision"] == "sha256:artifact-one"
    snapshot["recent_runs"][0]["policy"]["name"] = "Snapshot mutation"
    assert managed.bridge.snapshot()["recent_runs"][0]["policy"]["name"] == "Pick one"
    assert managed.bridge.check_begin({"operation_type": "DIRECT", "profile_id": first_id}) is None
    assert managed.bridge.check_begin({"operation_type": "MODEL", "profile_id": "unregistered"}) is None
    managed.assert_no_motion()


def test_run_history_is_bounded_and_unknown_profile_is_not_fabricated(managed):
    applied = managed.ready()
    request = {"operation_type": "MODEL", "profile_id": applied["runtime_profile_id"]}
    for index in range(130):
        managed.bridge.record_begin(managed.arm, request, {"operation_id": "operation-" + str(index)})
    managed.bridge.record_begin(managed.arm, {"operation_type": "MODEL", "profile_id": "unregistered"}, {"operation_id": "ignored"})
    history = managed.bridge.snapshot()["recent_runs"]
    assert len(history) == 128
    assert history[0]["operation_id"] == "operation-2"
    assert history[-1]["operation_id"] == "operation-129"
    managed.assert_no_motion()


def test_real_controller_start_and_empty_semantic_reset_reuse_managed_session(tmp_path, monkeypatch):
    """Real owner/supervisor path; only native model work is mocked.

    No control thread starts and no device is opened. MockArm.send is reached
    only after the test explicitly grants permission and begins MODEL STREAM.
    """
    from soarm_station import control, model_runtime
    from soarm_station.config import load
    from soarm_station.policy import ModelOutcome

    monkeypatch.setattr(control, "now", time.monotonic)
    config = load(Path(__file__).resolve().parents[1] / "config/mock.yaml")
    for key in ("socket", "frame_dir", "lock_dir", "database"):
        config[key] = str(tmp_path / key)
    config["policy_management"] = {"source_config": str(tmp_path / "sources.yaml"), "profiles_dir": str(tmp_path / "profiles"), "cache_db": str(tmp_path / "policy.sqlite3"), "run_dir": str(tmp_path / "runs")}
    calibration = tmp_path / "calibration.json"
    calibration.write_text("{}")
    config["arms"][0].update(calibration=str(calibration), calibration_revision="calibration-integration")
    arm = control.ArmController(config["arms"][0], config, "boot-integration", {})
    arm.q, arm.gripper = arm.backend.read()
    arm.sample_time = time.monotonic()
    owner = SimpleNamespace(config=config, arms={arm.id: arm}, cameras={}, boot="boot-integration")
    features = [{"name": name + ".pos", "joint": name, "unit": "radian"} for name in arm.q]
    features.append({"name": "gripper.pos", "joint": "gripper", "unit": "percent"})
    profile = {"arm_id": arm.id, "backend": "local", "device": "cpu", "state_features": copy.deepcopy(features), "action_features": copy.deepcopy(features), "action_fps": 10., "language_conditioned": False, "calibration_revision": "calibration-integration", "cameras": {}, "_artifact": {"root": str(tmp_path / "checkpoint"), "checkpoint_path": str(tmp_path / "checkpoint/pretrained_model"), "artifact_revision": "sha256:integration-artifact", "files": [{"path": "model.safetensors", "size": 12, "sha256": "a" * 64}], "category": "smolvla"}}
    document = {"schema_version": 1, "id": "managed-integration", "name": "Managed integration", "execution": {"kind": "local", "device": "cpu"}, "bindings": {"arm": arm.id, "cameras": {}}, "model": {"source_id": "models", "relative_path": "checkpoint", "artifact_revision": "sha256:integration-artifact"}}

    class TaskSession(FakeSession):
        control_polls = False

        def __init__(self):
            super().__init__()
            self.tasks = []

        def reset_task(self, instruction, token):
            self.tasks.append((instruction, token))
            self.resets += 1
            self.paused = False

        def gate(self, outcome):
            assert outcome.session is self
            return outcome

        def commit(self, outcome, send, hold):
            assert not self.paused
            send()
            return True, outcome

    session = TaskSession()
    loader = Mock(return_value=session)
    forbidden_reload = Mock(side_effect=AssertionError("First Start/reset must reuse the managed session"))
    monkeypatch.setattr(model_runtime, "load_session", forbidden_reload)

    def prediction(loaded, active_profile, q, gripper, cameras, capture_time):
        assert loaded is session
        assert "files" not in active_profile["_artifact"]
        return ModelOutcome("ACTION", {"positions": {name: value + .01 for name, value in q.items()}, "gripper": gripper, "gripper_unit": "percent"}, .1, capture_time, 5., session.resets, session)

    poll = Mock(side_effect=prediction)
    monkeypatch.setattr(model_runtime, "poll_session", poll)
    bridge = PolicyRuntime(owner, loader=loader)
    try:
        job = bridge.prepare(document, "sha256:integration-profile", profile)
        deadline = time.monotonic() + 2
        while bridge.job(job["id"])["state"] not in ("complete", "failed") and time.monotonic() < deadline:
            time.sleep(.002)
        result = bridge.job(job["id"])
        assert result["state"] == "complete", result
        runtime_id = result["result"]["applied"]["runtime_profile_id"]
        compact = config["profiles"][runtime_id]
        assert arm.model.session is session
        assert arm.model.profile_key == execution_key(compact)
        assert "files" not in compact["_artifact"]
        assert arm.backend.writes == 0
        assert arm.permission is False
        assert session.resets == 0

        arm.sample_time = time.monotonic()
        arm.set_permission(True, "operator", arm.session)
        feedback = arm.begin({"principal": "operator", "runtime_boot_id": owner.boot, "controller_session": arm.session, "operation_type": "MODEL", "mode": "STREAM", "profile_id": runtime_id, "instruction": ""})
        operation_id = feedback["operation_id"]

        def drive_until(predicate):
            deadline = time.monotonic() + 2
            while not predicate() and time.monotonic() < deadline:
                with arm.lock:
                    arm.q, arm.gripper = arm.backend.read()
                    arm.sample_time = time.monotonic()
                    arm._tick(arm.sample_time, .01)
                    assert arm.op is not None, arm.error
                time.sleep(.002)
            assert predicate(), {"error": arm.error, "model_state": arm.model_state, "model_reason": arm.model_reason}

        drive_until(lambda: arm.model.actions >= 1 and arm.backend.writes >= 1)
        assert session.tasks[0][0] == ""
        assert session.tasks[0][1][0] == operation_id
        assert arm.model.loads == 1
        loader.assert_called_once()
        forbidden_reload.assert_not_called()
        first_generation = arm.generation
        prior_writes, prior_actions = arm.backend.writes, arm.model.actions
        feedback = arm.feedback()
        acknowledged = arm.input({"runtime_boot_id": owner.boot, "controller_session": arm.session, "operation_id": operation_id, "operation_epoch": arm.op["operation_epoch"], "principal": "operator", "window_id": feedback["window_id"], "sequence": arm.last_sequence + 1, "semantic_revision": 1, "frame_kind": "COMMAND", "input_id": "empty-task-reset", "body": {"instruction": ""}})
        assert acknowledged["accepted"] is True
        assert arm.generation == first_generation + 1
        drive_until(lambda: arm.model.resets >= 2 and arm.model.actions > prior_actions and arm.backend.writes > prior_writes)
        assert [instruction for instruction, _ in session.tasks] == ["", ""]
        assert session.tasks[1][1] == (operation_id, arm.generation)
        assert arm.model.session is session
        assert arm.model.loads == 1
        assert arm.model.profile_key == execution_key(compact)
        assert session.closes == 0
        assert poll.call_count >= 2
        loader.assert_called_once()
        forbidden_reload.assert_not_called()
    finally:
        bridge.close()
        bridge.executor.shutdown(wait=True, cancel_futures=True)
        arm.pool.shutdown(wait=True, cancel_futures=True)
        session.close()
        arm.backend.close()
