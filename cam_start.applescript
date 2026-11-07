-- Launches the cameras from a double-click: opens a Terminal window and runs the camera
-- program on the config file the config editor writes by default. Assumes the repository
-- is at ~/Desktop/ratrixcam; edit the two paths if yours differ. Open this file in Script
-- Editor and use File > Export with the format "Application" to make a double-clickable
-- launcher; compiled copies (.scpt, .app) are this machine's and are not kept in git.
tell application "Terminal"
	do script "cd ~/Desktop/ratrixcam && python3 ratrixcam_IO.py -c ~/Desktop/camera_config.json"
end tell
