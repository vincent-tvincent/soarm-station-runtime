import json
from types import SimpleNamespace
import pytest
from soarm_station.policy_common import PolicyError
from soarm_station.policy_runtime import PolicyRuntime


def test_memory_budget_uses_verified_compact_inventory_not_empty_files(tmp_path,monkeypatch):
    pin=tmp_path/'pin.json';pin.write_text(json.dumps({'files':[{'path':'weights','size':600*1024*1024}]}))
    runtime=SimpleNamespace(config={'policy_management':{'memory_reserve_bytes':256*1024*1024}},arms={})
    registry=PolicyRuntime(runtime)
    monkeypatch.setattr('os.sysconf',lambda key:1024*1024 if key=='SC_AVPHYS_PAGES' else 1024)
    try:
        with pytest.raises(PolicyError) as error:registry._check_memory({'_artifact':{'inventory_path':str(pin)}})
        assert error.value.code=='MEMORY_BUDGET'
        assert error.value.details['estimated_bytes']>1024**3
    finally:registry.close()
