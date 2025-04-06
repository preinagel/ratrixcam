# version 250306_1325
import signal
import cv2
import time
from datetime import datetime
import os
import sys
import argparse
import shutil
from multiprocessing import Process

from ratrixcam_IO import configFile  #full path to config file
from ratrixcam_IO import load_settings #function to load config file
application_config = load_settings(configFile)

video_codec = cv2.VideoWriter_fourcc("m", "p", "4", "v") # this is a function in cv2

 
# ----------------------------------------------------------------------
# BEGIN FUNCTION DEFINITIONS
# function to move video file from temporary to permanent location
def copyfile(file_completed, out_file):
    # utility function for moving video file from temporary to permanent location
    # copy the file from the temporary location to the permanent one
    # print('attempting to copy ',file_completed)
    # print('to ',out_file)
    if not os.path.exists(file_completed):
        print("WARNING! ", file_completed, "not found!")

    # shutil.copy(file_completed, out_file)

    source_file = file_completed
    destination_file = out_file

    try:
        shutil.copy2(source_file, destination_file)
        # print(f"File '{source_file}' copied successfully to '{destination_file}'.")

    except shutil.SameFileError:
        print("Error: Source and destination files are the same.")
    except PermissionError:
        print("Error: Permission denied. Unable to copy the file.")
    except FileNotFoundError:
        print(f"Error: Source file '{source_file}' not found.")
    except OSError as e:
        print(f"OS Error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

    # check if this succeeded
    if os.path.exists(out_file) and os.path.isfile(out_file):
        # print('Saved ', out_file, ' to storage drive')
        os.remove(file_completed)
        if os.path.exists(file_completed):
            print("WARNING: failed to clean up ", file_completed)
        # else: print('Removed ', file_completed)
    else:
        print("WARNING!!! Failed to save ", file_completed, " to permanent storage")


# Function to stream video to drive in slices
def device_stream(
    device_id,
    time_slice,
    width,
    height,
    fps,
    output_folder,
    label,
    time_break,
    cam_number,
):
    # device_id is a hardware address from 0 to 7
    # cam_numer is numeric from 1 to 8
    # time_break is no longer used

    # create full path filename for updating still images (used for GUI display)
    CameraStillFilename = (
        application_config.stillFolder + "cam_" + str(cam_number).zfill(2) + "_status" + ".png"
    )

    # set the path for permanent storage of the current video according to the date
    # note this path will be updated within the loop to reflect date changes
    savedir = (
        output_folder + label + "_" + datetime.now().strftime("%y%m%d")
    )  # note no dashes in date string
    # print("Proposed permanent directory: ", savedir)
    if not os.path.exists(savedir):
        os.mkdir(savedir)
        if os.path.exists(savedir):
            print("Output directory ", savedir, " created successfully.")
        else:
            print("Creation of output directory ", savedir, "failed.")

    # fname depends on camera ID, date and time for redundant bookkeeping
    videoFname = (
        "cam"
        + str(cam_number).zfill(2)
        + "_"
        + str(datetime.now().strftime("%y%m%d_%H-%M-%S"))
        + video_ext
    )

    # full path to the TEMPORARY storage location of this video file on the local hard drive
    # note video_temp_path does not depend on the date, temp files should be cleaned up as transferred
    fullpathVideoFile = os.path.join(video_temp_path + "/" + videoFname)
    print("streaming to ", fullpathVideoFile)
    # full path for the same filename on the permanent drive location
    out_file = os.path.join(savedir + "/" + videoFname)

    # try to connect to the camera
    cap = cv2.VideoCapture(int(device_id))  # hardware address
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)
    cap.set(cv2.CAP_PROP_EXPOSURE, application_config.cam_exposure)  # limits exposure duration
    if cap is None or not cap.isOpened():
        print(
            "Failed to initialize device ",
            device_id,
            " camera ",
            cam_number,
            " Camera not found",
        )

    ret = cap.set(3, width)
    ret = cap.set(4, height)
    video_file_count = 1  # initialize

    # Create video writer for the first video file
    font = cv2.FONT_HERSHEY_PLAIN
    count = 0  # tracks frames since last still image update
    start = time.time()  # to measure elapsed time
    video_writer = cv2.VideoWriter(
        fullpathVideoFile, video_codec, fps, (int(cap.get(3)), int(cap.get(4)))
    )

    processes = []  # keeps track of processes spawned so they can be cleaned up
    # question - is the scope of this list limited to within this function? is that good?

    while (
        cap.isOpened()
    ):  # this loop is executed once per video frame until camera is stcaopped
        # get one frame from the camera
        ret, frame = cap.read()

        if ret:  # if a frame was captured
            # label the video frame
            video_date = datetime.now().strftime("%y%m%d")  # written on video frame
            video_time_long = datetime.now().strftime(
                "%H:%M:%S.%f"
            )  # written on video frame
            cv2.putText(
                frame,
                label,
                (10, height - 10),
                font,
                1,
                (255, 255, 255),
                thickness=1,
                lineType=cv2.LINE_AA,
            )
            cv2.putText(
                frame,
                video_date,
                (width - 115, height - 25),
                font,
                1,
                (255, 255, 255),
                thickness=1,
                lineType=cv2.LINE_AA,
            )
            cv2.putText(
                frame,
                video_time_long,
                (width - 80, height - 10),
                font,
                1,
                (255, 255, 255),
                thickness=1,
                lineType=cv2.LINE_AA,
            )

            # write the just-captured frame to the currently open video writer
            video_writer.write(
                frame
            )  # filename determined by when video file was opened

            # if video slice duration has been exceeded, close video file and initialize new one
            if time.time() - start > time_slice:
                video_file_count += 1  # increment the video count
                start = time.time()  # update the current video start time to now
                # grab timestamps for new file and folder names
                newVideoTimeStamp = datetime.now().strftime(
                    "%H-%M-%S"
                )  # temporarily hold the time stamp for new video file name
                newVideoDateStamp = datetime.now().strftime(
                    "%y%m%d"
                )  # temporary hold the date stamp for permanent save path

                # clean up after any previously spawned file transfer processes
                for process in processes:
                    if not process.is_alive():  # if previous write job is done
                        process.join()  # kill previous write job
                    else:
                        print("Not done moving last video??")

                # this is the temporary file name full path
                file_completed = fullpathVideoFile
                completed_outfile = out_file
                old_videoFname = videoFname
                old_video_writer = (
                    video_writer  # bind another name to the video writer object
                )

                # create a new video filename for the temporary streaming location
                # fname depends on camera ID, date and time for redundant bookkeeping
                videoFname = (
                    "cam"
                    + str(cam_number).zfill(2)
                    + "_"
                    + str(datetime.now().strftime("%y%m%d_%H-%M-%S"))
                    + video_ext
                )
                fullpathVideoFile = os.path.join(video_temp_path + "/" + videoFname)
                print("camera", cameraNumber, "will now stream to", fullpathVideoFile)
                # full path for the same filename on the permanent drive location
                # update the permanent savedir to match the date for the video just opened
                savedir = output_folder + label + "_" + newVideoDateStamp
                out_file = os.path.join(savedir + "/" + videoFname)

                # print('preparing to release ', old_videoFname, 'videowriter...')
                old_video_writer.release()  # closes the video file
                # immediately open the new video writer
                video_writer = cv2.VideoWriter(
                    fullpathVideoFile,
                    video_codec,
                    fps,
                    (int(cap.get(3)), int(cap.get(4))),
                )

                time.sleep(
                    0.1
                )  # enforce a delay after the release() command before attempting
                # to copy the old file
                # print('released ', old_videoFname, 'videowriter')
                # spawn a separate process to move the closed tmp file to permanent location
                p = Process(target=copyfile, args=(file_completed, completed_outfile))
                p.start()
                processes.append(p)  # keep track of process to clean up later

            # once per sec, try to update the still image
            if count % (1 * fps) == 0:
                # print('attempting to overwrite',CameraStillFilename)
                try:
                    cv2.imwrite(CameraStillFilename, frame)
                    # print('success!')
                except Exception as e:
                    print(type(e), e)
                    # print('unable to write still frame ',CameraStillFilename)

            count += 1  # increment frame count whether that succeeds or fails

            # check for quit condition -- will this be sent when stop recording button is pressed??
            if cv2.waitKey(1) & 0xFF == ord("q"):
                print("DETECTED QUIT COMMAND on camera ", cam_number)
                print("at time ", datetime.now().strftime("%H:%M:%S.%f"))
                break  # exits the while loop?

        else:  # if no frame was captured
            print(
                "??? failed to capture a frame from ",
                videoFname,
                "at",
                datetime.now().strftime("%H:%M:%S.%f"),
            )
            print("with code ", ret)
            # if this only happens when system is shut down we should exit now?
            # break

    # final cleanup on exit from function
    print("===EXITING FRAME GRAB LOOP FOR CAMERA", cam_number)
    if video_writer.isOpened():
        video_writer.release()
        # print('released ', videoFname, 'videowriter')
        time.sleep(0.1)  # allow time for flush to disk
    cap.release()
    cv2.destroyAllWindows()
    for process in processes:
        process.join()

    return video_file_count

def graceful_shutdown(cameraNumber: int, filecount: int):

    # on exit from script, reflect that camera is paused
    print(
        "==== PAUSING CAMERA ",
        cameraNumber,
        "at: ",
        datetime.now().strftime("%Y-%m-%d_%H:%M:%S"),
    )
    print("Saved ", filecount, " files during this run")
    sys.exit(0)

# ------------------------------------------------------------------------
# Parser description
def main():
    parser = argparse.ArgumentParser(description="Ratrix Camera Setup", add_help=False)
    parser.add_argument("-c", "--config", type=str)
    parser.add_argument("-i", "--index", type=int)
    args = vars(parser.parse_args())
    config = load_settings(args["config"])
    if config is None:
        print("ERROR: Cannot load settings")
        return
    cameraNumber = int(args["index"]) # this should be a numeric camera number, starting from 1?


    # check for, or create, temporary folder for streaming to (internal drive)
    # this does not change with recording date or video time
    video_temp_path = os.path.join(
        application_config.tempStreamPath + str(cameraNumber).zfill(2)
    )  # camera numbers 01-08
    if not os.path.isdir(video_temp_path):
        try:
            os.mkdir(video_temp_path)
        except IOError as e:
            # Handle other IOErrors, e.g., permission denied
            print(f"An IOError occurred: {e}")
            print("ERROR: Cannot find or create streaming folder", video_temp_path)
    signal.signal(signal.SIGINT, lambda sig, frame: graceful_shutdown(cameraNumber, filecount))

    
    filecount = device_stream(
        config.device_id,
        time_slice,
        config.width,
        config.height,
        config.fps,
        config.output_folder,
        label,
        cameraNumber,
    )

# END FUNCTION DEFINITIONS
# ----------------------------------------------------------------------

# Starting stream
# print('starting device ',device_id,' camera ',cameraNumber, ' named ',label)
if __name__ == "__main__":
    main()
