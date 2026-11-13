from geometry_msgs.msg import TransformStamped
from tf2_ros.static_transform_broadcaster import StaticTransformBroadcaster
from .common import BaseNode,run
class Transforms(BaseNode):
    def __init__(self):
        super().__init__('static_transform_publishers');self.broadcaster=StaticTransformBroadcaster(self)
        children=set();transforms=[]
        for t in self.config.get('transforms',[]):
            if t['child'] in children:raise ValueError('Duplicate TF authority')
            children.add(t['child']);m=TransformStamped();m.header.stamp=self.get_clock().now().to_msg()
            m.header.frame_id=t['parent'];m.child_frame_id=t['child'];p=t['translation_m'];q=t['quaternion_xyzw']
            m.transform.translation.x=float(p[0]);m.transform.translation.y=float(p[1]);m.transform.translation.z=float(p[2])
            m.transform.rotation.x=float(q[0]);m.transform.rotation.y=float(q[1]);m.transform.rotation.z=float(q[2]);m.transform.rotation.w=float(q[3]);transforms.append(m)
        self.broadcaster.sendTransform(transforms)
def main():run(Transforms)
