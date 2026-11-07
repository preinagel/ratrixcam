-- Opens the camera config editor from a double-click: opens a Terminal window and starts
-- the editor's server, which opens the editor page in the browser. Assumes the repository
-- is at ~/Desktop/ratrixcam; edit the path if yours differs. Open this file in Script
-- Editor and use File > Export with the format "Application" to make a double-clickable
-- launcher; compiled copies (.scpt, .app) are this machine's and are not kept in git.
tell application "Terminal"
	do script "cd ~/Desktop/ratrixcam && python3 gui/config_editor.py"
end tell
