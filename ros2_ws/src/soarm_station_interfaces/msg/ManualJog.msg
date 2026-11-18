# Normalized Cartesian rates in the configured operator frame.
float64[3] translation
float64 elevation
float64 roll
uint8 level
bool position_only
float64 gripper_rate
bool has_gripper_target
float64 gripper_target
# Nonzero while the gamepad trigger follows the jaw. A fresh pickup gets a new ID.
uint32 grasp_attempt
