import os
def getVideoPaths(output_folder:str,label:str,cam_number:int,video_ext:str='.mp4'):
    # set the path for permanent storage of the current video according to the date
    savedir = output_folder + label + '_' + datetime.now().strftime('%y%m%d') # note no dashes in date string
    # print("Proposed permanent directory: ", savedir)
    if not os.path.exists(savedir):
        try:
            os.mkdir(savedir)
        except IOError as e:
            print(f"An IOError occurred: {e}")
            print('ERROR: Cannot create external folder',savedir)

        if os.path.exists(savedir):
            print('Output directory ',savedir, ' created successfully.')
        else:
            print('Creation of output directory ',savedir, 'failed.')

    # fname depends on camera ID, date and time for redundant bookkeeping
    videoFname= 'cam' + str(cam_number).zfill(2) + '_' + str(datetime.now().strftime('%y%m%d_%H-%M-%S')) + video_ext
    # full path on the permanent drive location
    permanentFilePath = os.path.join(savedir + '/' + videoFname)

    # full path to the TEMPORARY storage location
    streamingFilePath = os.path.join(video_temp_path + '/' + videoFname)
    print('streaming to ',streamingPath)

    return streamingFilePath,permanentFilePath



in deviceStream
replace
    # set the path for permanent storage of the current video according to the date
    # note this path will be updated within the loop to reflect date changes
    savedir = output_folder + label + '_' + datetime.now().strftime('%y%m%d') # note no dashes in date string
    #print("Proposed permanent directory: ", savedir)
    if not os.path.exists(savedir):
        os.mkdir(savedir)
        if os.path.exists(savedir):
            print('Output directory ',savedir, ' created successfully.')
        else:
            print('Creation of output directory ',savedir, 'failed.')

    # fname depends on camera ID, date and time for redundant bookkeeping
    videoFname= 'cam' + str(cam_number).zfill(2) + '_' + str(datetime.now().strftime('%y%m%d_%H-%M-%S')) + video_ext

    # full path to the TEMPORARY storage location of this video file on the local hard drive
    # note video_temp_path does not depend on the date, temp files should be cleaned up as transferred
    fullpathVideoFile = os.path.join(video_temp_path + '/' + videoFname)
    print('streaming to ',fullpathVideoFile)
    # full path for the same filename on the permanent drive location
    out_file = os.path.join(savedir + '/' + videoFname)

with:
fullpathVideoFile,out_file = getVideoPaths(output_folder,label,cam_number,video_ext)



