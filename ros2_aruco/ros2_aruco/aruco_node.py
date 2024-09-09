"""
This node locates Aruco AR markers in images and publishes their ids and poses.

Subscriptions:
   /camera/image_raw (sensor_msgs.msg.Image)
   /camera/camera_info (sensor_msgs.msg.CameraInfo)
   /camera/camera_info (sensor_msgs.msg.CameraInfo)

Published Topics:
    /aruco_poses (geometry_msgs.msg.PoseArray)
       Pose of all detected markers (suitable for rviz visualization)

    /aruco_markers (ros2_aruco_interfaces.msg.ArucoMarkers)
       Provides an array of all poses along with the corresponding
       marker ids.

Parameters:
    marker_size - size of the markers in meters (default .0625)
    aruco_dictionary_id - dictionary that was used to generate markers
                          (default DICT_5X5_250)
    image_topic - image topic to subscribe to (default /camera/image_raw)
    camera_info_topic - camera info topic to subscribe to
                         (default /camera/camera_info)

Author: Nathan Sprague
Version: 10/26/2020

"""
import cv2
import numpy as np
import quaternion
import rclpy
import rclpy.node
import tf_transformations
from cv_bridge import CvBridge
from geometry_msgs.msg import Pose, PoseArray
from rcl_interfaces.msg import ParameterDescriptor, ParameterType
from rclpy.qos import qos_profile_sensor_data
from ros2_aruco_interfaces.msg import ArucoMarkers
from sensor_msgs.msg import CameraInfo, Image

from py_usb2can_param.py_usb2can_param import py_usb2can_param
from communication_msgs.msg import CommunicationFrame

class ArucoNode(rclpy.node.Node):
    def __init__(self):
        super().__init__("aruco_node")

        # Declare and read parameters
        self.declare_parameter(
            name="marker_size",
            value=0.0625,
            descriptor=ParameterDescriptor(
                type=ParameterType.PARAMETER_DOUBLE,
                description="Size of the markers in meters.",
            ),
        )

        self.declare_parameter(
            name="aruco_dictionary_id",
            value="DICT_5X5_250",
            descriptor=ParameterDescriptor(
                type=ParameterType.PARAMETER_STRING,
                description="Dictionary that was used to generate markers.",
            ),
        )

        self.declare_parameter(
            name="image_topic",
            value="/camera/image_raw",
            descriptor=ParameterDescriptor(
                type=ParameterType.PARAMETER_STRING,
                description="Image topic to subscribe to.",
            ),
        )


        self.declare_parameter(
            name="camera_info_topic",
            value="/camera/camera_info",
            descriptor=ParameterDescriptor(
                type=ParameterType.PARAMETER_STRING,
                description="Camera info topic to subscribe to.",
            ),
        )

        self.declare_parameter(
            name="camera_frame",
            value="",
            descriptor=ParameterDescriptor(
                type=ParameterType.PARAMETER_STRING,
                description="Camera optical frame to use.",
            ),
        )

        self.declare_parameter(
            name="imshow_isshow",
            value=True,
            descriptor=ParameterDescriptor(
                type=ParameterType.PARAMETER_BOOL,
                description="Show vison with bbox"
            )
        )

        self.declare_parameter(
            name="id_whitelist",
            value=[],
            descriptor=ParameterDescriptor(
                type=ParameterType.PARAMETER_INTEGER_ARRAY,
                description="List of marker ids to consider. Empty list means all markers are considered."
            )
        )

        self.marker_size = (
            self.get_parameter("marker_size").get_parameter_value().double_value
        )
        self.get_logger().info(f"Marker size: {self.marker_size}")

        dictionary_id_name = (
            self.get_parameter("aruco_dictionary_id").get_parameter_value().string_value
        )
        self.get_logger().info(f"Marker type: {dictionary_id_name}")

        image_topic = (
            self.get_parameter("image_topic").get_parameter_value().string_value
        )
        self.get_logger().info(f"Image topic: {image_topic}")

        info_topic = (
            self.get_parameter("camera_info_topic").get_parameter_value().string_value
        )
        self.get_logger().info(f"Image info topic: {info_topic}")

        self.camera_frame = (
            self.get_parameter("camera_frame").get_parameter_value().string_value
        )
        self.imshow_isshow = (
            self.get_parameter("imshow_isshow").get_parameter_value().bool_value
        )

        self.id_whitelist = (
            self.get_parameter("id_whitelist").get_parameter_value().integer_array_value
        )
        # Make sure we have a valid dictionary id:
        try:
            dictionary_id = cv2.aruco.__getattribute__(dictionary_id_name)
            if type(dictionary_id) != type(cv2.aruco.DICT_5X5_100):
                raise AttributeError
        except AttributeError:
            self.get_logger().error(
                "bad aruco_dictionary_id: {}".format(dictionary_id_name)
            )
            options = "\n".join([s for s in dir(cv2.aruco) if s.startswith("DICT")])
            self.get_logger().error("valid options: {}".format(options))

        # Set up subscriptions
        self.info_sub = self.create_subscription(
            CameraInfo, info_topic, self.info_callback, qos_profile_sensor_data
        )

        self.create_subscription(
            Image, image_topic, self.image_callback, qos_profile_sensor_data
        )

        # Set up publishers
        self.poses_pub = self.create_publisher(PoseArray, "aruco_poses", 10)
        self.markers_pub = self.create_publisher(ArucoMarkers, "aruco_markers", 10)
        self.can_pub = self.create_publisher(CommunicationFrame, "can_frame", 10)

        # Set up fields for camera parameters
        self.info_msg = None
        self.intrinsic_mat = None
        self.distortion = None

        dictionary = cv2.aruco.getPredefinedDictionary(dictionary_id)
        parameters = cv2.aruco.DetectorParameters()
        self.detector = cv2.aruco.ArucoDetector(dictionary, parameters)

        self.bridge = CvBridge()

    def info_callback(self, info_msg):
        self.get_logger().info("Camera info received.")
        self.info_msg = info_msg
        self.intrinsic_mat = np.reshape(np.array(self.info_msg.k), (3, 3))
        self.distortion = np.array(self.info_msg.d)
        # Assume that camera parameters will remain the same...
        self.destroy_subscription(self.info_sub)

    def image_callback(self, img_msg):
        if self.info_msg is None:
            self.get_logger().warn("No camera info has been received!")
            return

        cv_image = self.bridge.imgmsg_to_cv2(img_msg, desired_encoding="mono8")
        markers = ArucoMarkers()
        pose_array = PoseArray()
        if self.camera_frame == "":
            markers.header.frame_id = self.info_msg.header.frame_id
            pose_array.header.frame_id = self.info_msg.header.frame_id
        else:
            markers.header.frame_id = self.camera_frame
            pose_array.header.frame_id = self.camera_frame

        markers.header.stamp = img_msg.header.stamp
        pose_array.header.stamp = img_msg.header.stamp

        corners, marker_ids, rejected = self.detector.detectMarkers(cv_image)

        if self.imshow_isshow :
            ar_img = cv2.aruco.drawDetectedMarkers(cv_image,corners,marker_ids)
            cv2.imshow("ArucoMarker",ar_img)
            cv2.waitKey(1)
        
        if marker_ids is not None:
            if cv2.__version__ > "4.0.0":
                rvecs, tvecs, _ = cv2.aruco.estimatePoseSingleMarkers(
                    corners, self.marker_size, self.intrinsic_mat, self.distortion
                )
            else:
                rvecs, tvecs = cv2.aruco.estimatePoseSingleMarkers(
                    corners, self.marker_size, self.intrinsic_mat, self.distortion
                )
            for i, marker_id in enumerate(marker_ids):
                if marker_id in self.id_whitelist or len(self.id_whitelist) == 0:
                    pose = Pose()
                    pose.position.x = tvecs[i][0][0]
                    pose.position.y = tvecs[i][0][1]
                    pose.position.z = tvecs[i][0][2]

                    rot_matrix = np.eye(4)
                    rot_matrix[0:3, 0:3] = cv2.Rodrigues(np.array(rvecs[i][0]))[0]
                    quat = tf_transformations.quaternion_from_matrix(rot_matrix)

                    pose.orientation.x = quat[0]
                    pose.orientation.y = quat[1]
                    pose.orientation.z = quat[2]
                    pose.orientation.w = quat[3]

                    pose_array.poses.append(pose)
                    markers.poses.append(pose)
                    markers.marker_ids.append(marker_id[0])

            if len(markers.marker_ids) > 0:
                can_topic = CommunicationFrame()
                can_topic.header = img_msg.header
                
                can_topic.id = 0x41
                can_topic.frame = CommunicationFrame.CAN_DATA
                can_topic.format = CommunicationFrame.STANDARD_FORMAT
                
                
                
                for pose in pose_array.poses:
                    data_array1 = list()
                    data_array2 = list()
                    quat = np.quaternion(pose.orientation.w, pose.orientation.x, pose.orientation.y, pose.orientation.z)
                    euler = quaternion.as_euler_angles(quat)
                    print(euler)
                    r = 180 - np.int32(np.rad2deg(euler[1]))
                    
                    position_x = np.int32(pose.position.x * 100)
                    position_z = np.int32(pose.position.z * 100)
                    self.get_logger().info(f"Position: {position_x}, {position_z}, {r}")
                    
                    data_array1 = np.append(data_array1, 0x02)
                    data_array1 = np.append(data_array1, np.right_shift(position_x,8) & 0xFF)
                    data_array1 = np.append(data_array1, (position_x + 128) & 0xFF)
                    data_array1 = np.append(data_array1, ((position_z + 128) >> 8) & 0xFF)
                    data_array1 = np.append(data_array1, position_z & 0xFF)
                    data_array2 = np.append(data_array2, 0x03)
                    data_array2 = np.append(data_array2, (r >> 8) & 0xFF)
                    data_array2 = np.append(data_array2, (r) & 0xFF)
                    data_array2 = np.append(data_array2, 0x00)
                    data_array2 = np.append(data_array2, 0x00)

                    can_topic.data = data_array1.astype(np.uint8).tolist()
                    self.can_pub.publish(can_topic)
                    can_topic.data = data_array2.astype(np.uint8).tolist()
                    self.can_pub.publish(can_topic)
                self.poses_pub.publish(pose_array)
                self.markers_pub.publish(markers)


def main():
    rclpy.init()
    node = ArucoNode()
    rclpy.spin(node)

    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
