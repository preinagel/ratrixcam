# version 250306_0852
import signal
import subprocess
import shutil
import os
import time

from ratrixcam_IO import configFile  #full path to config file
from ratrixcam_IO import load_settings #function to load config file
from ratrixcam_IO import Config # object for configuration
from ratrix_cam_server import copyfile 

def removeCamStill(cam_idx: int):
    # this function replaces the most recent still image of camera with the default offline image
    camID = str(cam_idx+1).zfill(2) # note the file names go from 01 to Ncameras
    src = application_config.blankImage
    dst = application_config.stillFolder + "cam_" + camID + "_status.png"
    shutil.copy(src, dst)  # NB this will force overwrite by default
    print('Updated still to show camera',camID,'is offline')

def moveTempVideos(config: Config)-> bool:
    #if no temp folder exists, return
    if not os.path.exists(config.tmpStreamPath):
        success=True
        return success
    # if temp folder is empty, return
    d=os.listdir(config.tmpStreamPath)
    if len(d)==0: 
        success=True
        return success

    # TO BE WRITTEN IF NOT EMPTY MOVE STRAY FILES TO SAFETY
    # this should use the copyfile function used to routinely move the files
    print('Cleaning up temporary video folder...')
    success=False
    return success
    #for i in range(len(d)):
         # check if it is a folder, or look for the expected folders 01 thru 08
         # check if it contains videos .mp4 files or anything else
         # use the file dates to construct outfile path for each date
         # create directories if needed
         # move all the files to the outfile directory
         # confirm moved, then delete the folder in the temp location
    

def initializeTempFolder(config:Config)-> bool:
    # if the temp streaming directory doesn't exist create it
    if not os.path.exists(config.tmpStreamPath):
        try: #  create a new empty one
            os.mkdir(config.tmpStreamPath)
            success=True
            return success
        except PermissionError:
            print('Error: Permission denied. Unable to create', config.tmpStreamPath)
        except OSError as e:
            print(f"OS Error: {e}")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
        
    else: #if the temp directory exists, make sure it is empty  
        d=os.listdir(config.tmpStreamPath)
        if len(d)==0: 
            success=True
            return success 
        # if not empty,try to empty it
        try:
            success=moveTempVideos(config)
            return success
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            success=False
            return success

def initializeOutFolder(config:Config):
    if os.path.exists(config.out_path):
        print(config.out_path, 'already exists')
        return
    try:
        os.mkdir(config.out_path)
    except PermissionError:
        print('Error: Permission denied. Unable to create', config.tmpStreamPath)
    except OSError as e:
        print(f"OS Error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
    return   

def graceful_shutdown(config: Config | None, p: list[subprocess.Popen | None], p_TTL: subprocess.Popen | None):
    print("Multicam attempting to shut down nicely")
    for i, process in enumerate(p):
        if process is None:
            continue
        process.terminate()
    if application_config.recording_ttl and p_TTL is not None:
        p_TTL.terminate()

    tries = 100  # try to terminate a few times before killing processes
    for _ in range(tries):
        all_cameras_terminated = all(process is None or process.returncode is not None for process in p)
        TTL_terminated = p_TTL is None or p_TTL.returncode is not None
        if all_cameras_terminated and TTL_terminated:
            break
        time.sleep(0.1)  # wait a bit before trying again

    # in case termination fails, kill
    for i, process in enumerate(p):
        if process is None:
            continue
        print(f"Camera {config.camera_names[i] if config is not None else i} failed to terminate, killing process")
        process.kill()
    if application_config.recording_ttl and p_TTL is not None:
        print("TTL logging failed to terminate, killing process")
        p_TTL.kill()

    print("Done cleaning up; Exiting ratrix_multicam.")

# Main loop: check every second and restart any cameras or processes that are not running
def main():
    # --------------------------------------------------------------
    print("Accessing settings file...")  # get settings from the json file
    config = load_settings(configFile)
    if config is None:
        print("Error loading settings file")
        return
    print("Settings for ", config.study_label, " successfully loaded")

    ok=initializeTempFolder(config) # STILL NOT WRITTEN: IF temp directory is not empty must clean up NOT delete!
    if not ok: return
    ok=initializeOutFolder(config)
    if not ok: return
#RESUME EDITING CODE HERE PR 250306 
    print("Multicam: Starting cameras...")
    # try to start all the cameras
    command = [
        [
            "python3",
            application_config.ratrix_cam_script,
            "-c",
            str(configFile),
            "-i",
            str(index),  # numerical from 1-8
        ]
        for index in range(config.Ncameras)
    ]  # to save the camera starting commands for re-use
    
    # if applicable, try to start a TTL monitoring process
    command_TTL = ["python3", TTLscript]

    camera_processes: list[subprocess.Popen | None] = [
        None for _ in range(config.Ncameras)
    ]  # to contain a list of processes
    p_TTL = None
    
    signal.signal(signal.SIGINT, lambda sig, frame: graceful_shutdown(config, camera_processes, p_TTL))

    while True:
        # otherwise check all cameras and restart if needed
        for camera_idx, process in enumerate(camera_processes):
            if process is not None and process.poll() is None:
                continue
            # if not running, try to restart it
            try:
                camera_processes[camera_idx] = subprocess.Popen(command[camera_idx])
                print(f"Started camera {config.camera_names[camera_idx]}: {command[camera_idx]}")
            except Exception as e:
                print(type(e), e)  # let user know it is dead
                print(f"Camera {config.camera_names[camera_idx]} is offline")
                removeCamStill(camera_idx)  # replace cam image with blank

        # check the TTL process and restart if applicable
        if application_config.recording_ttl:
            if p_TTL is not None and p_TTL.poll() is None:
                continue
            # if not running, try to relaunch
            try:
                p_TTL = subprocess.Popen(command_TTL)
                print("TTL logging restarted")
            except Exception as e:
                print(type(e), e)
                print("Cannot restart TTL logging")
                
        time.sleep(1)
    


# If in recording mode, initialize the cameras and any other processes on entry
# is this the correct way to implement a main guard for this section of the code?
if __name__ == "__main__":
    main()
