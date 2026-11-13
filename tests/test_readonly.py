import json
import pytest
from soarm.hardware import ReadOnlySO101
def test_allowlist_and_no_motion():
 a=ReadOnlySO101({})
 with pytest.raises(ValueError):a._read(1,42,2)
 with pytest.raises(PermissionError):a.send({},0)
 assert a.writes==0
def test_offset_sign_and_existing_calibration(tmp_path):
 c={'joint':{'id':1,'homing_offset':-240,'range_min':895,'range_max':3325}}
 p=tmp_path/'calibration.json';p.write_text(json.dumps(c))
 a=ReadOnlySO101({'port':'unused','calibration':str(p)})
 # No actual port is opened; exercise the register comparisons with a fake module.
 import sys,types
 from unittest.mock import patch
 class Port:
  def __init__(self,*args,**kwargs):pass
  def close(self):pass
 values={3:777,31:2288,9:895,11:3325}
 a._read=lambda motor,address,size:values[address]
 with patch.dict(sys.modules,{'serial':types.SimpleNamespace(Serial=Port)}):a.connect();a.close()
