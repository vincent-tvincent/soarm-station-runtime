import math
import sys
import types

import numpy as np
import pytest

from soarm_station.kinematics import plan_cartesian


def fake_kinematics(monkeypatch,*,bad_residual=False):
    calls=[]
    class Solver:
        def __init__(self,*args,**kwargs):
            self.solver=types.SimpleNamespace(enable_joint_limits=lambda enabled:calls.append(('limits',enabled)))
        def inverse_kinematics(self,current,pose,**kwargs):
            assert calls==[('limits',False)]
            self.pose=pose.copy();return np.array([math.degrees(2.4)])
        def forward_kinematics(self,positions):
            result=self.pose.copy()
            if bad_residual:result[0,3]+=1.
            return result
    module=types.ModuleType('lerobot.model.kinematics');module.RobotKinematics=Solver
    monkeypatch.setitem(sys.modules,'lerobot.model.kinematics',module)
    return calls


def test_cartesian_solver_disables_urdf_joint_limits_and_accepts_outside_old_yaml_domain(monkeypatch):
    calls=fake_kinematics(monkeypatch)
    config={'urdf':'model.urdf','base_frame':'base','joints':[{'name':'q','min_rad':-.1,'max_rad':.1}]}
    target={'frame_id':'base','position':[.3,0.,.2],'orientation_mode':'POSITION_ONLY','duration_s':3.}
    path=plan_cartesian(config,[target],{'q':0.},42.)
    assert calls==[('limits',False)]
    assert path==[{'positions':{'q':pytest.approx(2.4)},'gripper':42.,'time_s':3.}]


def test_cartesian_solver_still_requires_its_measured_fk_residual_to_converge(monkeypatch):
    fake_kinematics(monkeypatch,bad_residual=True)
    config={'urdf':'model.urdf','base_frame':'base','joints':[{'name':'q'}]}
    with pytest.raises(ValueError,match='FK residual'):
        plan_cartesian(config,[{'frame_id':'base','position':[.3,0.,.2],'duration_s':3.}],{'q':0.},42.)
