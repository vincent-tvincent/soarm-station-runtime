#!/usr/bin/env python3
"""Loopback-only dashboard QA with real temporary policy-management stores.

Operations and ROS graph stay the existing preview's in-memory fixtures. The
management HTTP handlers are imported from the backend implementation; this
script does not reproduce their CRUD logic. No native/ROS bridge is supplied,
so policy preparation must be reported unavailable rather than simulated ready.

The sample weight files are explicitly metadata-only fixtures, not trained
models. All writable management paths are inside one temporary directory.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import importlib
import importlib.util
import inspect
import json
from pathlib import Path
import struct
import sys
import tempfile

from aiohttp import web


ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
DEFAULT_BACKEND = Path("/private/tmp/soarm-policy-backend/repo/python")
POLICY_ASSETS = {
    "policy-management.js", "policy-management.css", "policy-draft.js",
    "policy-api.js", "yaml-vendor.js", "LICENSE-yaml.txt",
}
MARKER = "soarm-policy-management-preview-only-v1"


def load_dashboard_preview():
    """Load the existing mock without editing or executing its main entry point."""
    source = ROOT / "scripts/preview-dashboard.py"
    spec = importlib.util.spec_from_file_location("station_dashboard_preview", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load scripts/preview-dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ASSETS = set(module.ASSETS) | POLICY_ASSETS
    return module


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def fixture_weights(path: Path):
    """Write a tiny valid safetensors container with no model tensors."""
    header = json.dumps({"__metadata__": {"purpose": MARKER}}, separators=(",", ":")).encode()
    header += b" " * ((8 - len(header) % 8) % 8)
    path.write_bytes(struct.pack("<Q", len(header)) + header)


def seed_checkpoint(directory: Path, *, family: str, name: str, cameras: list[dict], complete=True):
    checkpoint = directory / "pretrained_model"
    checkpoint.mkdir(parents=True)
    joints = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"]
    state_features = [{"name": f"arm1/{joint}.pos", "joint": joint, "unit": "degree"} for joint in joints]
    state_features.append({"name": "arm1/gripper.pos", "joint": "gripper", "unit": "percent"})
    config = {
        "type": family, "n_obs_steps": 1, "fps": 30, "device": "cpu",
        "input_features": {"observation.state": {"type": "STATE", "shape": [6]}},
        "output_features": {"action": {"type": "ACTION", "shape": [6]}},
    }
    for camera in cameras:
        config["input_features"][camera["key"]] = {"type": "VISUAL", "shape": camera["shape"]}
    write_json(checkpoint / "config.json", config)
    fixture_weights(checkpoint / "model.safetensors")
    processors = {"steps": [], "preview_only": True, "purpose": MARKER}
    write_json(checkpoint / "policy_preprocessor.json", processors)
    if complete:
        write_json(checkpoint / "policy_postprocessor.json", processors)
    contract = {
        "state_features": state_features, "action_features": copy.deepcopy(state_features),
        "action_fps": 30, "calibration_revision": "preview-calibration-v1",
        "language_conditioned": family != "act", "camera_inputs": cameras,
    }
    # JSON is valid YAML; these fixture files need no extra serializer dependency.
    write_json(directory / "station-policy.yaml", {"schema_version": 1, "name": name, "contract": contract})
    return contract


def seed_store(directory: Path, preview):
    """Create only task-owned temporary files; never accept a production data root."""
    directory = directory.resolve()
    if not directory.is_dir() or any(directory.iterdir()):
        raise RuntimeError("Preview seeding requires an empty temporary directory")
    (directory / ".preview-only").write_text(MARKER, encoding="utf-8")
    project = directory / "models/project"
    ssd = directory / "models/ssd"
    profiles = directory / "local/policy-profiles"
    incoming = directory / "incoming-upload-example"
    for path in (project, ssd, profiles, incoming):
        path.mkdir(parents=True)
    act_cameras = [
        {"key": "observation.images.front", "role": "Scene view", "masked": False, "shape": [3, 480, 640]},
        {"key": "observation.images.wrist", "role": "Tool view", "masked": False, "shape": [3, 480, 640]},
    ]
    smol_cameras = [
        {"key": "observation.images.camera1", "role": "Global", "masked": False, "shape": [3, 480, 640]},
        {"key": "observation.images.camera2", "role": "Wrist", "masked": False, "shape": [3, 480, 640]},
        {"key": "observation.images.camera3", "role": "Additional", "masked": False, "shape": [3, 480, 640]},
    ]
    seed_checkpoint(project / "act-cup", family="act", name="Preview ACT · cup placement", cameras=act_cameras)
    seed_checkpoint(ssd / "smolvla-cup", family="smolvla", name="Preview SmolVLA · three views", cameras=smol_cameras)
    seed_checkpoint(ssd / "smolvla-incomplete", family="smolvla", name="Preview incomplete checkpoint", cameras=smol_cameras, complete=False)
    seed_checkpoint(incoming / "smolvla-new", family="smolvla", name="Preview uploaded SmolVLA", cameras=smol_cameras)
    roots = [
        {"id": "project", "label": "Project models · preview", "path": str(project), "enabled": True, "allow_upload": True},
        {"id": "ssd", "label": "SSD model store · preview", "path": str(ssd), "enabled": True, "allow_upload": True},
    ]
    source_config = directory / "config/model-sources.yaml"
    write_json(source_config, {"schema_version": 1, "roots": roots})
    camera_bindings = {
        "cup_act": {"observation.images.front": "forward", "observation.images.wrist": "wrist"},
        "cup_smol": {"observation.images.camera1": "forward", "observation.images.camera2": "wrist", "observation.images.camera3": "overview"},
    }
    for identifier, name, source, relative in (
        ("cup_act", "Preview cup placement · ACT", "project", "act-cup"),
        ("cup_smol", "Preview SO-ARM · SmolVLA", "ssd", "smolvla-cup"),
    ):
        value = {"schema_version": 1, "id": identifier, "name": name,
                 "execution": {"kind": "local", "device": "cpu"},
                 "model": {"source_id": source, "relative_path": relative},
                 "bindings": {"arm": "arm1", "cameras": camera_bindings[identifier]},
                 "extensions": {"preview_only": True, "purpose": MARKER}}
        (profiles / f"{identifier}.yaml").write_text("# Preview fixture: no trained weights or native bridge.\n" + json.dumps(value, indent=2) + "\n", encoding="utf-8")
    remote = {"schema_version": 1, "id": "remote_example", "name": "Remote deployment · unavailable in preview",
              "execution": {"kind": "remote", "endpoint": "tcp/192.0.2.10:7447", "deployment": "example-vla",
                            "expected_artifact": "sha256:" + "0" * 64},
              "bindings": {"arm": "arm1", "cameras": {"observation.images.front": "forward", "observation.images.wrist": "wrist"}},
              "extensions": {"preview_only": True}}
    write_json(profiles / "remote_example.yaml", remote)
    resources = copy.deepcopy(preview.data["resources"])
    for arm in resources.get("arms", []):
        arm["calibration_revision"] = "preview-calibration-v1"
    config = {
        "schema_version": 1, "device_id": "policy-management-preview", "_root": str(directory),
        "_path": str(directory / "config/runtime.json"),
        "arms": copy.deepcopy(resources.get("arms", [])), "cameras": copy.deepcopy(resources.get("cameras", [])),
        "profiles": {},
        "policy_management": {"source_config": str(source_config), "profiles_dir": str(profiles),
                              "cache_db": str(directory / "local/policy-cache.sqlite")},
    }
    for arm in config["arms"]:
        arm["backend"] = "mock"
    write_json(Path(config["_path"]), config)
    return {"config": config, "resources": resources, "source_config": source_config,
            "profile_store": profiles, "root": directory, "incoming": incoming}


def import_backend(backend_python: Path):
    backend_python = backend_python.resolve()
    needed = backend_python / "soarm_station/policy_http.py"
    if not needed.is_file():
        raise RuntimeError(f"Management HTTP module is not available yet: {needed}. "
                           "Use --backend-python with the completed backend checkout; no fake CRUD routes are substituted.")
    sys.path.insert(0, str(backend_python))
    manager_module = importlib.import_module("soarm_station.policy_manager")
    http_module = importlib.import_module("soarm_station.policy_http")
    return manager_module, http_module


async def mount_management(app, seeded, backend_python):
    """Mount the actual backend file/catalog/upload routes without a native bridge."""
    manager_module, http_module = import_backend(backend_python)
    manager_class = getattr(manager_module, "PolicyManager", None)
    mount = getattr(http_module, "mount_policy_routes", None)
    if manager_class is None or mount is None:
        raise RuntimeError("Backend mounting contract is not complete: expected "
                           "PolicyManager(config, resources=..., bridge=None) and mount_policy_routes(app, manager)")
    # Binding validates the signature before construction; it does not contact
    # a robot or invoke a fallback native runtime.
    inspect.signature(manager_class).bind(seeded["config"], resources=seeded["resources"], bridge=None)
    manager = manager_class(seeded["config"], resources=seeded["resources"], bridge=None)
    if inspect.isawaitable(manager):
        manager = await manager
    mounted = mount(app, manager)
    if inspect.isawaitable(mounted):
        await mounted

    async def close_manager(_app):
        close = getattr(manager, "close", None)
        if close:
            result = close()
            if inspect.isawaitable(result):
                await result
    app.on_cleanup.append(close_manager)
    return manager


async def create_app(args, directory):
    preview_module = load_dashboard_preview()
    preview_module.PORT = args.port
    preview = preview_module.Preview(args.fixture, args.web_root, args.reference_root, args.scenario)
    preview.fixture["preview"]["environment"]["label"] = "MOCK · policy management / no runtime bridge"
    seeded = seed_store(directory, preview)
    preview.data["resources"] = copy.deepcopy(seeded["resources"])
    app = preview_module.build_app(preview)

    @web.middleware
    async def no_runtime_or_remote_discovery(request, handler):
        # File/catalog APIs below are the real backend. This preview boundary
        # explicitly prevents network/native setup even if a future manager
        # accidentally grows a direct remote-inspection fallback without bridge.
        tail = request.path.rstrip("/").split("/")[-1]
        if request.method == "POST" and request.path.startswith("/api/v1/policy-profiles/") and tail in {"inspect-draft", "inspect", "prepare"}:
            return web.json_response({"schema_version": 1, "code": "PREVIEW_RUNTIME_UNAVAILABLE",
                                      "message": "This hardware-free preview has no native bridge or remote inference connection.",
                                      "details": {"hardware_connected": False, "remote_discovery": False}}, status=503)
        return await handler(request)

    app.middlewares.insert(0, no_runtime_or_remote_discovery)
    await mount_management(app, seeded, args.backend_python)
    app["policy_preview_root"] = directory
    app["policy_preview_seed"] = seeded
    print(f"POLICY UI QA / temporary stores / no hardware: http://{HOST}:{args.port}", flush=True)
    print(f"Temporary source configuration: {seeded['source_config']}", flush=True)
    print(f"Temporary profiles: {seeded['profile_store']}", flush=True)
    print(f"Small upload fixture: {seeded['incoming']}", flush=True)
    print("Native policy preparation is unavailable. No remote server discovery is authorized by this preview.", flush=True)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--fixture", type=Path, default=ROOT / "tests/dashboard-fixture.json")
    parser.add_argument("--web-root", type=Path, default=ROOT / "web")
    parser.add_argument("--reference-root", type=Path, default=Path.cwd())
    parser.add_argument("--scenario", choices=["station", "mobile", "unknown"], default="station")
    parser.add_argument("--backend-python", type=Path,
                        default=ROOT / "python" if (ROOT / "python/soarm_station/policy_http.py").is_file() else DEFAULT_BACKEND)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be between 1024 and 65535")
    with tempfile.TemporaryDirectory(prefix="soarm-policy-preview-") as name:
        try:
            web.run_app(create_app(args, Path(name)), host=HOST, port=args.port, access_log=None, print=None)
        except (ImportError, RuntimeError, TypeError) as error:
            parser.exit(2, f"Policy preview unavailable: {error}\n")


if __name__ == "__main__":
    main()
