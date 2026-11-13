import os
import time
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile,ReliabilityPolicy,DurabilityPolicy
from rclpy.executors import MultiThreadedExecutor
from diagnostic_msgs.msg import DiagnosticArray,DiagnosticStatus,KeyValue
from soarm_station.config import load
SENSOR=QoSProfile(depth=1,reliability=ReliabilityPolicy.BEST_EFFORT)
STATE=QoSProfile(depth=10,reliability=ReliabilityPolicy.RELIABLE)
def stamp_ns(stamp,n): stamp.sec=int(n//1000000000);stamp.nanosec=int(n%1000000000);return stamp
def duration_s(d):return d.sec+d.nanosec/1e9
def topic(arm,suffix):return f'/soarm_station/arms/{arm}/{suffix}'
class BaseNode(Node):
    def __init__(self,name):
        super().__init__(name,namespace='/soarm_station')
        self.declare_parameter('config',os.environ.get('SOARM_STATION_CONFIG',''))
        self.config=load(self.get_parameter('config').value)
        self.diagnostics=self.create_publisher(DiagnosticArray,'/soarm_station/diagnostics',STATE)
        self.create_timer(1.,self.diagnose)
    def diagnose(self):
        d=DiagnosticArray();d.header.stamp=self.get_clock().now().to_msg()
        s=DiagnosticStatus();s.name=self.get_fully_qualified_name();s.hardware_id=self.config['device_id'];s.message='running'
        s.values=[KeyValue(key='config_revision',value=self.config['_revision'])];d.status=[s];self.diagnostics.publish(d)
def run(cls):
    rclpy.init();n=cls();e=MultiThreadedExecutor(num_threads=max(8,len(n.config['arms'])+4));e.add_node(n)
    try:e.spin()
    except KeyboardInterrupt:pass
    finally:
        n.destroy_node();e.shutdown(timeout_sec=3)
        if rclpy.ok():rclpy.shutdown()
