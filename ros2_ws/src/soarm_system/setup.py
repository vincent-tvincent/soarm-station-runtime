from setuptools import setup,find_packages
from glob import glob
setup(name='soarm_system',version='0.1.0',packages=find_packages(),
data_files=[('share/ament_index/resource_index/packages',['resource/soarm_system']),
('share/soarm_system',['package.xml']),('share/soarm_system/launch',glob('launch/*.py'))],
entry_points={'console_scripts':['lerobot_node=soarm_system.runtime_node:main','telemetry_node=soarm_system.telemetry_node:main',
'workflow_manager=soarm_system.workflow_node:main','dashboard_node=soarm_system.dashboard_node:main',
'static_transform_publishers=soarm_system.transforms:main','recorder=soarm_system.recording:main']})
