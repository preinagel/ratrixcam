# from the last fully working version 250306_0852 
# no longer compatible with new refactor, but the GUI tk window logic worked
import json
import os
import sys
from pathlib import Path
import glob
import shutil
from subprocess import Popen, PIPE, call
import subprocess
import time
import datetime as dt
from tkinter import *
from tkinter import ttk
from tkinter import messagebox
import tkinter as tk
 
from PIL import Image, ImageTk
import tkinter.font as font
Ncameras=8 #to do: load this from the json file
 
camImage=[] # create list to hold the currently displayed camera images
cameraStream=[] # create list to hold the open camera streams
pCameras=[] # list to contain handle to process that launches all cameras

if os.environ.get('DISPLAY','') == '':
    os.environ.__setitem__('DISPLAY', ':0.0')

# set hardwired paths here (could most of this be in the json file?)
rootDir = r'/Users/rat_hub_01/Desktop'
initPath = rootDir + r'/ratrixCameras/sys/'
masterScript = rootDir+r'/ratrixCameras/ratrix_multicam.py' #master script
tempStreamPath = rootDir+r'/ratrixCameras/temp/' #temp stream location
blankImage = initPath+r'/init/blanks/offline_status.png' #to show when cameras off
initFile = initPath + 'ratrix_01_id.json' #settings
default_init = initPath + 'ratrix_01_id.json' # in case we want to keep a different default file
stillFolder = initPath + r'/init/status/' # folder where live cam stills will update 
#print('Saving stills in',stillFolder)


# check the paths
if not os.path.exists(initPath):
    print('Fatal Error: No init folder found.')
    sys.exit()  
 
out_path = r'/Volumes/Rack_01/'
if not os.path.exists(out_path):
    print('Fatal Error: No storage disk found')
    sys.exit()
 
# Check if json settings file present
jsonfound = os.path.isfile(initFile)
if jsonfound == False: #  
    try: shutil.copy(default_init, initFile)
    except Exception as e:
        print('Fatal Error: failed to find settings file '+ initFile )
        print(type(e),e)
        sys.exit()

# check if folder exists for still image status files, or try to create it
if not os.path.exists(stillFolder):
    try: os.mkdir(stillFolder)
    except IOError as e:
        print(f"An IOError occurred: {e}")
        print('Can neither find nor create folder for update images')
     
#-----------------------------------------------------------
# BEGIN FUNCTION DEFS  
def getSettings(path_json_ini): 
    # returns: study_label, cameraNames, recording_status, fps, x_res, y_res, time_slice
    # Read INI file
    with open(path_json_ini, 'r') as myfile:
         json_ini = myfile.read()
    # Parse JSOn
    ini_data = json.loads(json_ini)
    # extract settings to variables
    study_label = str(ini_data['study_label'])
    recording_status = ini_data['recording_status']
    cameraNames = ini_data['rack_cam_list']
    fps = ini_data['fps']
    x_res = ini_data['width']
    y_res = ini_data['height']
    time_slice = ini_data['time_slice']
    
    
    return study_label, cameraNames, recording_status, fps, x_res, y_res, time_slice

def time_refresh_record(): # could these be passed as arguments? start_recording_date,recorded_label,window_record
    # accesses general scope variables 
    # start_recording_date (read)
    # recorded_label (modifies)
    # window_record (acts on)
    date_now = dt.datetime.now()
    date_now = dt.datetime.now()
    time_label.config(text = f"{date_now:%H:%M:%S}") 
    date_label.config(text = f"{date_now:%A, %B %d, %Y}")
    time_recorded = date_now - start_recording_date
    time_sec = time_recorded.total_seconds()
    time_recorded_label = "Total recorded: " + str(divmod(time_sec, 86400)[0]) + ' days, ' + str(divmod(time_sec, 3600)[0]) + ' hours'
    recorded_label.config(text = time_recorded_label)
    window_record.after(1, time_refresh_record) # is this circular?  # ? (start_recording_date,recorded_label,window_record)) #
 
def checkDriveUsage():  # out_path,hdd_status when I pass as argument the code crashes
    # uses global scope variables out_path (read)
    #                             hdd_status (object, acts on/modifies?)
    total, used, free = shutil.disk_usage(out_path)
    hdd_space_used = round(100*used/total, 1) # local scope
    hdd_status.value = hdd_space_used
    hdd_used.config(text = str("SSD Space Used: " + str(hdd_space_used) +"%"))
    window_record.after(5000, checkDriveUsage)  # should there be a () here?

def updateStateInJson(initFile,newState):
    # updates the file initFile with the state newState
    print('Attempting to update the json file with status = ',newState)
    with open(initFile, 'r') as myfile:
        json_ini = myfile.read()
    ini_data = json.loads(json_ini)
    ini_data['recording_status'] = newState
    with open(initFile, 'w') as file:
        json.dump(ini_data, file, indent=2)

def updateLabels(initFile, study_label_input, camera_label_input):
    # Writing user-input labels to the settings file
    # initFile (string)
    # study_label_input: an object, not modified
    # camera_label_input: an object, not modified
    with open(initFile, 'r') as myfile:
         json_ini = myfile.read()
    ini_data = json.loads(json_ini)
    ini_data['study_label'] = study_label_input.get()
    for i in range(0, len(camera_label_input)): 
        ini_data['rack_cam_list'][i] = camera_label_input[i].get()  
    with open(initFile, 'w') as file:
        json.dump(ini_data, file, indent=2)

def startRecording(recordingState,pCameras):
    # recordingState - reads and modifies
    # pCameras - modifies
    # relies on global scope bariables initFile, masterScript, window
    if recordingState: # is already on
        print('Recording is already on. Stop first if you want to restart')
        messagebox.showinfo('Cameras are already on', 'Stop first if you want to restart') 
    else:
        try: # if cameras are not already on try to start them
            # spawn a separate process launch the multicam script
            p = subprocess.Popen(['python3', masterScript])
            pCameras.append(p)
            updateStateInJson(initFile,True)
            recordingState=True
            time.sleep(2)
            window.destroy() # closes the gui input window so cam window appears?
        except subprocess.CalledProcessError: 
            print('Camera initialization error')
            messagebox.showwarning('Error', 'Unable to launch cameras', icon='warning') 
        
def stop_recording(recordingState): 
    # recordingState is modified and also updated in json file
    if recordingState is False: # should indicate already off. could check process handle?
        confirmation = messagebox.askquestion('Cameras appera to be off already', 'Are you sure?',icon = 'warning') 
        if confirmation == 'no': return
    else:
        confirmation = messagebox.askquestion('STOP all recordings', 'Are you sure?', icon = 'warning') 
        if confirmation == 'no': return
    
    #does this terminate the recording process nicely?
    # should we pass in the process handle for the cameras to check status or terminate?
    window_record.destroy() 
    recordingState = False
    updateStateInJson(initFile, recordingState)
     
def getCameraImage(stillFolder, cam_number, retries=3,delay=0.1): 
    # stillFolder is where camera images will be updated
    # cam_number specifies which camera to operate on starting from 1
    # attempts to load a still image saved by camera to a folder
    # if anything goes wrong return the blank image instead
    
    camInd=cam_number-1 #zero indexing
    global camImage # this allows function to alter camImage in main scope
                    # better to pass reference to camImage explicitly

    # the temp images are simply named cam_01_status.jpg etc
    stillname=stillFolder + 'cam_' + str(cam_number).zfill(2) + '_status.png'
   
    for attempt in range(retries):
        try:
            im = Image.open(stillname, mode='r')
            resized = im.resize((cam_x, cam_y), resample=2)
            tkimage = ImageTk.PhotoImage(resized)
            camImage[camInd] = tkimage 
            return tkimage
        except Exception as e:
            # If the error message indicates a truncated file, wait and retry.
            #print(f"Attempt {attempt+1} to load {stillname} failed: {e}")
            time.sleep(delay)
    
    # If all attempts fail, return the default image.
    no_signal = Image.open(blankImage)
    no_signal_r = no_signal.resize((cam_x, cam_y), resample=2)
    cameraDown = ImageTk.PhotoImage(no_signal_r)
    camImage[camInd] = cameraDown
    return cameraDown

    # try: im = Image.open(stillname, mode = 'r')
    # except Exception as e:
    #     print(type(e), e) 
    #     camImage[camInd] = cameraDown
    #     return cameraDown
    
    # try: resized = im.resize((cam_x, cam_y), resample=2)
    # except Exception as e:
    #     print(type(e), e) 
    #     camImage[camInd] = cameraDown
    #     return cameraDown
    
    # # if image successfully acquired update it and return it (why both?)
    # tkimage = ImageTk.PhotoImage(resized)
    # camImage[camInd] = tkimage 
    # return tkimage

def updateImageRealtime(cam_number):
    # note cam_number is numeric starting with 01

    # I do not know why these global declarations were here:
    # global camImage
    # global cameraStream
    camInd = cam_number - 1 # zero indexing
    # try to get current camera view still 
    newImage = getCameraImage(stillFolder, cam_number)
    try: 
        cameraStream[camInd].config(image = newImage)
        camImage[camInd]=newImage
    except Exception as e:
        print(type(e), e) 
        cameraStream[camInd].config(image = camImage[camInd])
    window_record.after(cam_refresh, updateImageRealtime,cam_number)

# END FUNCTION DEFS  
# ------------------------------------------------------------------------


# MAIN CODE STARTS HERE

# is this the correct place to put the main guard? this code will eventually spawn a
# subprocess for the ratrix_multicam.py call but the GUI must remain responsive to
# detect the user clicking the stop recording button
if __name__ == '__main__':
    # Get the settings from the json file
    study_label, cameraNames, recordingState, fps, x_res, y_res, time_slice = getSettings(initFile)
    # Recording status override - is false regardless of how it is set in the json file
    recordingState = False
    updateStateInJson(initFile,recordingState) # set state to false at onset of GUI

    # GUI paramters  
    columns = 2
    cam_refresh = 1500
    cam_x, cam_y = 280, 215
    bgcolor = '#3b0a0a'  #ratrix maroonƒ

    # create the gui input window
    window = Tk()
    window.configure(background=bgcolor)
    entry_font = font.Font(family='Helvitica', size=19)
    version_font = font.Font(family='Helvitica', size=9)
    window.geometry('1024x600')
    window.title('Ratrix Cameras')

    study_label_input = StringVar()
    cameraLabelInput = [None]*Ncameras
    cameraIDentry = [None]*Ncameras
    date_now = dt.datetime.now()

    # Create Label to display the Date
    time_label = Label(window, text=f"{date_now:%A, %B %d, %Y}", bg=bgcolor, fg='#ffffff', font = version_font)
    time_label.place(x=570, y=40)

    # Study label input field - this is a highly optional feature, it can be handled by editing json by hand
    study_label_title = Label(window, text="Study Name:", bg=bgcolor, fg='#ffffff', font = entry_font)
    study_label_input = StringVar(window, value = study_label)
    study_label_entry = Entry(window, textvariable=study_label_input, bg=bgcolor, fg='#ffffff', font = entry_font)
    study_label_entry.configure(bg=bgcolor, insertbackground = "white")
    study_label_title.place(x=65, y=20)
    study_label_entry.place(x=240, y=20)

    # Layout for camera label input fields - this is a highly optional feature, it can be handled by editing json by hand
    row_step = 60
    pivot_point = 120
    x1=[65, 540]
    x2=[200,675]
    for cl in range(2): # left vs right columns
        for rw in range(0, int(Ncameras/2)): # rows
            ind=2*cl+rw  
            id_label = 'Cam ' + str(ind+1) + ' ID:'  #camera ids start with 1 not 0
            cameraID_label = Label(window, text=id_label, bg=bgcolor, fg='#ffffff', font = entry_font)
            cameraLabelInput[ind] = StringVar(window, value = cameraNames[ind])
            cameraIDentry = Entry(window, textvariable=cameraLabelInput[ind], bg=bgcolor, font = entry_font, fg='#ffffff')
            cameraIDentry.configure(bg=bgcolor, insertbackground = "white")
            cameraID_label.place(x=x1[cl], y=pivot_point+rw*row_step)
            cameraIDentry.place(x=x2[cl], y=pivot_point+rw*row_step) 

    # Create buttons
    
    # to update camera labels in the json file - this is a highly optional feature, it can be handled by editing json by hand
    button_font = font.Font(family='Helvitica', size=15)
    hdd_font = font.Font(family='Helvitica', size=17)
    submit = Button(window, text='Update Labels', # updates labels but which ones?
                    bd=0,  height=2, width=20, font = button_font,
                    command = lambda : updateLabels(initFile, study_label_input, cameraLabelInput)).place(x=205, y=pivot_point+(rw+1)*row_step)
    on_button = Button(window, text='Start Recording',bg='green', fg='green',
                    bd=0,  height=3, width=19, font = button_font,
                    command = lambda: startRecording(recordingState,pCameras)).place(x=100,y=500) 

    # we don't really want a stop button here only after we start recording, right?
    # stop_button = Button(window, text='Stop Recording', bg='red', fg='red',
    #                 bd=0,  height=3, width=19, font = button_font,
    #                 command = lambda:stop_recording()).place(x=550, y=500)
    #=============================================================================================================
    # THE ESSENTIAL FUNCTIONALITY IS HERE:
    # Data collection loop
    window.mainloop()
    
    window_record = Tk()

    bgcolor = bgcolor
    window_record.configure(background=bgcolor)
    window_record.geometry('1424x1200')
    right_row = 1150
    window_record.title('Ratrix Camera System')
    small_font = font.Font(family='Helvitica', size=12)
    study_font = font.Font(family='Helvitica', size=14)
    date_font = font.Font(family='Helvitica', size=11)

    # Recording current start time
    date_now = dt.datetime.now()
    start_recording_date = date_now
    # Create Label to display the Date
    date_label = Label(window_record, text=f"{date_now:%A, %B %d, %Y}", bg=bgcolor, fg='#ffffff', font = date_font)
    date_label.place(x=right_row, y=10)
    time_label = Label(window_record, text=f"{date_now:%H:%M:%S}", bg=bgcolor, fg='#ffffff', font = date_font)
    time_label.place(x=right_row + 165, y=10)

    # HDD status progressbar
    total, used, free = shutil.disk_usage(out_path)
    hdd_space_used = round(100*used/total, 1)
    hdd_space_used_label = "SSD Drive Space Used: " + str(hdd_space_used) +"%"
    hdd_used = Label(window_record, text=str(hdd_space_used_label), borderwidth=0, bg=bgcolor, font = hdd_font, fg='#ffffff')
    hdd_used.place(x=right_row, y=140)
    used_label = round((total // (2**30))/1000, 1)
    hdd_space = 'SSD Drive Capacity: ' + str(used_label) + ' TB'
    Label(window_record, text=str(hdd_space), borderwidth=0, bg=bgcolor, font = small_font, fg='#ffffff').place(x=right_row, y=165)
    hdd_status = ttk.Progressbar(window_record, orient = 'horizontal', style = "red.Horizontal.TProgressbar", 
                                length = 220, mode = "determinate", takefocus = True, maximum = 100, value = hdd_space_used)
    del hdd_space_used # do not keep this variable in main scope
    
    # Showing recording duration
    time_recorded = start_recording_date - dt.datetime.now()
    time_recorded_label = time_recorded.total_seconds()
    start_time_label_01 = Label(window_record, text='Recording started at: ', bg=bgcolor, fg='#ffffff', font = small_font)
    start_time_label_01.place(x=right_row, y=220)
    start_time_label_02 = Label(window_record, text=f"{start_recording_date:%A, %B %d, %Y, %H:%M:%S}", bg=bgcolor, fg='#ffffff', font = small_font)
    start_time_label_02.place(x=right_row, y=245)
    recorded_time = "Total recorded: " + str(divmod(time_recorded_label, 86400)[0]) + ' days, ' + str(divmod(time_recorded_label, 3600)[0]) + ' hours'
    recorded_label = Label(window_record, text=str(recorded_time), borderwidth=0, bg=bgcolor, font = small_font, fg='#ffffff')
    recorded_label.place(x=right_row + 1, y=270)

    # Setup for the camera view display
    study_label_title = Label(window_record, text="Study Name: " + str(study_label), bg=bgcolor, 
                            fg='#ffffff', font = study_font).place(x=right_row, y=295)
    pivot_point = 320
    row_step = 18
    min_slice = int(int(time_slice)/60)
    specs = 'Recording: ' + str(x_res) + 'x' + str(y_res) + 'x' + str(fps) + 'fps, ' + str(min_slice) + ' min'
    rec_specs_title = Label(window_record, text=specs, bg=bgcolor, fg='#ffffff', 
                            font = small_font).place(x=right_row, y=330)

    # Set up camera display
    # initialize to blank images
    no_signal = Image.open(blankImage)
    no_signal_r = no_signal.resize((cam_x, cam_y), resample=2)
    resized_no_signal = ImageTk.PhotoImage(no_signal_r)

    camRows=[1,1,2,2,1,1,2,2] # row positions for camera images
    camCols=[1,2,1,2,4,5,4,5] # col positions for camera images
    for i in range(Ncameras): 
        camImage.append(resized_no_signal)
        cameraStream.append(Label(window_record, image=camImage[i], borderwidth=0, bg=bgcolor))
        cameraStream[i].image = camImage[i]
        cameraStream[i].grid(row=camRows[i], column=camCols[i])
    

    recording_stop_button = Button(window_record, text='Stop Recording', bg='red', fg='red',
                    bd=0,  height=3, width=19, font = button_font,
                    command = lambda:stop_recording(recordingState)).place(x=550, y=500)
    
    # Realtime updates
    #time_refresh_record(start_recording_date,recorded_label,window_record) # passing params causes crashes?
    time_refresh_record()
    checkDriveUsage() # checkDriveUsage(out_path,hdd_status)?  passing arguments causes crashes
    for i in range(Ncameras):
        updateImageRealtime(i+1) #camera numbers start at 1

    window_record.mainloop()
    #window_record.after(cam_refresh, updateImageRealtime(cam_number))

    # is this a good place to clean up the process spawned by the 
    # startRecording button, after the user stops the recording?
    print('Waiting for cameras to stop...')
    time.sleep(10)
    print('Cleaning up...')
    tries=3 # try to terminate a few times before killing processes
    while tries>0:
        tries -= 1 # count down
        if pCameras[0].returncode is None: #still running
            pCameras[0].terminate()  # try to terminate nicely                    
        time.sleep(0.1) #wait a bit before trying again
    # in case termination fails, kill
    if pCameras[0].returncode is None:
        pCameras[0].kill()
    print('Session Ended')
