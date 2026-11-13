from setuptools import setup,find_packages
from glob import glob
setup(name='soarm_station_system',version='0.1.0',packages=find_packages(),
data_files=[('share/ament_index/resource_index/packages',['resource/soarm_station_system']),
('share/soarm_station_system',['package.xml']),('share/soarm_station_system/launch',glob('launch/*.py'))],
entry_points={'console_scripts':['lerobot_node=soarm_station_system.runtime_node:main','telemetry_node=soarm_station_system.telemetry_node:main',
'workflow_manager=soarm_station_system.workflow_node:main','dashboard_node=soarm_station_system.dashboard_node:main',
'static_transform_publishers=soarm_station_system.transforms:main','recorder=soarm_station_system.recording:main']})
