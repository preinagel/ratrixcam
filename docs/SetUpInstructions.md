# Setting up a RatrixCam System
Note: This system was designed for monitoring animal behavior in a home-cage environment, with minimal disturbance of the animals. For this reason, we use low-wavelength IR illumination, IR-sensitive cameras, and IR-pass filters. This ensures the cameras do not disturb the animal sleep cycle, and the video images are consistent regardless of whether room lights are on or off.  We also use our own IR lamps and remove the built-in IR LEDs on the cameras, because in our experience it was not possible to position the LEDs to prevent reflection glare and hotspots.  If these constraints are not relevant to your application, instructions related to those features can be ignored.

## Parts List
# Electronics
 
- Mac Mini (M2 or higher), with power cord
- Audio cable 
- Bluetooth Keyboard/Mouse
- Portable LCD display, 10.5”, HDMI cable, USB-C to USB-A power cable
- 2 powered Rosonway USB hubs with 4+ USB-A ports (3.0 or higher) → Thunderbolt 3 USB-C
- 2 4TB SSD external drives for fast USB-C or thunderbolt 
- 1 fast USB-C or thunderbolt cable for SSD (or USB-C to USB-A for older macs)
- 8 Arducam USB cameras that support IR-only image capture (model B0506 1080P)
- 3 IR illuminators; mounting hardware if needed 
- 1 grounded plug strip with at least 8 outlets, widely spaced to allow for cubes

# Optics/Cameras
- Wide-angle lenses M12 mount where needed (e.g. home cage)
- Close-up lenses M12 mount where needed (e.g. face view) 
- 8 IR-pass filters (IR-pass acrylic sheet, cut into ~2” squares)
- Adhesive polarizing film sufficient to cover the 8 IR-pass filters
- 8 camera cases with GoPro mount (3-D printed); small self-tapping screws
- 8 light-blocking camera hoods (plastic or 3-D printed)
- Some mechanism to lightly mount hood to camera case (e.g. magnets, velcro…)
- Some mechanism to hold an IR-pass filter in front of camera (e.g., slots in hood)
- 8 3-inch metal gopro arms; additional arms as needed for configuration
- 8 flat-mount adhesive GoPro quick-release helmet mounts
- Extra gopro screws, acorn nuts
- Wrenches and pliers for mounting and tightening go pro mounts 
- Assorted zip ties for cable routing

### Notes on USB hub choice
It may not be obvious, but the specific USB hub brand is critical.  We use Rosonway. Most USB splitters and many powered USB hubs do not work, even if they are rated for sufficient speed. Other hubs may work, but would require testing. The reason for this, briefly, is that Macs do not support direct addressing of video devices or USB ports. Therefore, to make sure the cameras are reliably assigned to the same identity within the ratrixcam code, we are depending on the obscure fact that when the Mac looks for video streams, it polls its thunderbolt ports in a consistent order; and some but not all USB hubs reliably poll their ports in a specific order. Because we depend on this to define camera identities, ratrixCam will not start until the Mac reports that it sees the expected number of video streams.

### Notes on camera choice
The model B0506 Arducam is currently (6/2025) the only Arducam USB camera that has good IR-sensitive recording. (We tested a large number of other supposedly IR-sensitive cameras, but the image through a true IR-pass filter was extremely poor, even with high IR illumination. Probably due to the CMOS chip sensitivity spectrum). Focusing is manual which we consider a plus. A camera without IR-cut technology (which we had to disable) and that runs at 60fps would have been preferred.  Word of warning: some secondary vendors ship this camera in a case; but these have usually glued the lens in place such that it can neither be focused nor removed.

Sending 24 bit color is useless as IR images are monochrome; not sure how the CMOS chip is using the 24bits when operating in IR mode. Transcoding to monochrome doesn’t seem to be widely supported.  

This camera is limited to 30fps; streaming 1080P at 24bit color mjpeg it has a raw uncompressed bit rate of ~1.5Gbps, far below the 5Gbps capacity of the USB channel. However, we use them at lower resolution to avoid overwhelming the computer when streaming 8 cameras at once. Using 640x480 is sufficient for applications like DeepLabCut and SLEAP, and keeps the file sizes smaller so the system can be run for longer sessions before running out of space on the drive.  

### Notes on lenses
The lenses that ship with model B0506 Arducam are fine, but it's useful that the lens is easily removable so custom optics can be swapped in when needed. Wide angle or fisheye lenses are widely available in the consumer market, and generally fine. Close-up lenses were more difficult to find. We tried many from the consumer market and none were acceptable. Edmund Optics has an excellent selection of scientific grade lenses and good technical sales support. We use their 12mm UCi Series Lens, f/2.8 for our application. 

### Notes on mounting hardware
We found the GoPro standard useful because there is a wide array of available mounting hardware options, which are already optimized to be lightweight, low profile, impervious to vibration and moisture. Once mounting screws are tightened, we find GoPro mounts to hold camera positions very securely and stably compared to clip-on, tripod, or bendable camera mounts. 

We 3D printed custom GoPro mount camera cases. Here are the stl files for the [front](./ratrixCamCameraCaseFront.stl) and [back](./ratrixCamCameraCaseBack.stl). We derived these from files that were provided under CC BY-NC 4.0 license [here](https://www.printables.com/model/661757-arducam-b0205-ir-case-gopro-mount/files). Specifically, we modified the depth of the box and the positions and sizes of openings for connectors and LEDs to accomodate differences in the camera card layout; and removed the photosensor opening. We printed these in PLA, high res, dense infill.

## Step 1. Pre-assemble 8 cameras
1. Disable the IR cut filters  (for model B0506 Arducam)
   - Plug camera into any computer and view image in any software
   - Turn off lights/cover the light sensor until you hear IR cut filter retract
   - Verify that image is good in dark under IR illumination through an IR pass filter
   - Unplug camera before turning on lights (to keep filter retracted)
   - Remove photo sensor and unplug the IR-filter-motor cable from camera card
2. Use pliers to gently loosen the focus lock ring, so lens can be focused manually
3. Remove the illuminating LEDs for any camera that would otherwise cause reflected glare 
4. Put each camera in a 3D-printed camera case with openings for cooling
5. Use dremel to  make/enlarge openings for connectors or LEDs, if needed
6. Secure case closed with 2-4 screws
7. Swap in alternative lenses if applicable
8. Mount the camera case to a go-pro extender arm.
   - Large thumb-screw GoPro screws are easier to work with in the dark, but think about which way you want them to protrude
   - Zip tie the camera cable to the closest GoPro arm such that tension on the cable will not unplug or stress the connector at the camera board
9. Connect the other end of the GoPro arm to a GoPro quick-disconnect connector, snapped into a flat adhesive-mount quick-disconnect base.
10. Put polarizing film on acrylic IR-pass filter, mount filter on hood, put hood on camera using magnets.
11. Use a silver sharpie to label camera case, USB-A end of its cable, and outside of hood


## Step 2. Prepare the Mac 
(These steps can be done before bringing to deployment location, using any keyboard, mouse and monitor)
1. Set up new Mac Mini with desired user name and password
2. Connect to a network
3. Download these programs from the internet and install:
- Visual Studio (from https://visualstudio.microsoft.com/downloads/)
- Python (from https://www.python.org/downloads/)
- GitHub desktop  (from https://desktop.github.com/download/)
4. Launch GitHub Desktop app and log in  
5. Use the clone repository menu option to clone preinagel/ratrixcam to the desktop.  
6. Give the terminal Full Disk Access permission:
    - In System Settings - Privacy & Security - Full Disk Access, click add (+)
    - Provide password when prompted, select Terminal from the applications list and ‘open’
7. Open a Terminal window and type the following commands (expect some text to scroll by indicating successful installation of each item. If you get error messages read them.)
```
> pip3 install –upgrade pip
> pip3 install basedpyright
> pip3 install pydantic
> pip3 install pillow
> pip3 install opencv-python
> python3
```
(that should launch python within the terminal; then type:)
```
    >>> import cv2
    >>> cv2.version
```
(if a version number is displayed, you’re good)

8. Close the terminal window, turn wireless off or put in Airplane Mode
9. Set the computer name as desired. Label computer box with name and MAC address.
10. Connect any thumb drive or SSD drive, set its name to ‘data’ (to test video output)
11. Connect the powered USB hubs into thunderbolt ports (left hub in left port, right hub in next one), connect the hubs to power sources, and power on only the ports you plan to use
12. Connect any USB camera to each of the 4 ports on each hub
13. Open the ratrixcam folder, and copy config.json and  cam_start.scpt to the desktop (you can rename if desired)
14. Click the script to open it and edit the paths
  - path includes the username you are logged in as, on the mac
  - change “config.json” to the path and name of your renamed config file
  - change the path and name of the output drive if necessary
  - click the hammer icon to save changes.

15. Run script to try launching the camera code. If this fails, check terminal window for errors.
16. Click Start Recording, wait until all 8 cameras show up. The camera numbers are associated with positions on the screen as follows:
     ![layout diagram](img/layout.png)
   - Label the USB ports on the hubs according to their camera numbers 1 through 8
   - Label the thunderbolt ports on the Mac to reflect which port serves 1-4 vs. 5-8
   - Label the Mac-end of the USB hub cable with this information as well
     
17. Check if videos appear in the specified temp folder

18. To the right of the camera view images the GUI displays text with session information. Stretch window if needed to see this. One fact displayed is the duration of individual video files that will be saved. 

19. Allow cameras to run for at least this duration, then check if videos get transferred to the output drive when the file closes.

20. Re-launch once more to confirm everything comes up in the right place again.

### Notes on how cameras are linked to IDs during setup
When a new system is assembled you need to determine which USB port corresponds to which video stream. If you do not change the hardware setup (don’t move USB hubs to different ports, don’t change USB hubs or which USB ports on hubs are used, don’t plug cameras into any other port) the order should be stable. This isn’t formally guaranteed by macOS, but seems to be reliable.

You can edit your custom config.json file in any text editor if you need to enter custom camera settings (frame rate, resolution, exposure time, GUI display position, enable/disable specific cameras).

The cameras will not launch until all the expected cameras are detected. This ensures they are assigned the correct video stream IDs. Therefore if you want to run this program with fewer than 8 cameras, you’ll need to edit LocalConfig.json to indicate how many cameras to expect and which camera streams (ports) will have cameras installed. 

Once the cameras launch, for the duration of that session you should be able to unplug any camera without the other cameras changing their identities. If you plug the camera back in again it should resume with the correct identity. However if more than one camera has been unplugged, all of them have to be plugged in again before any of them will re-launch. This protects against cameras switching identities if they are re-connected in the wrong order.

# Connecting in deployment location
Once cameras are prepared and Mac is set up, you can bring to the location you want to deploy for final setup. (The intended use of the system is in-cage monitoring of animals, which is typically in a less convenient environment).

1. Plug in the mac, attach the mini monitor and mount where it will be convenient
2. Plug Audio cable into mac, connect other end to any grounded device (e.g. a monitor). 
  *if you do not do this, the Mac will be ungrounded, as well as the cameras!*
3. Connect the external data drive
    - Mount one end of a high speed data cable somewhere accessible 
    - Plug one 4TB SSD drive into the cable
    - Plug the other end of the cable into the mac. Use a thunderbolt port if available, otherwise a USB-C port, otherwise a USB-A port.
      *we do not recommend using the USB hub that serves the cameras, even if it has open ports*
4. Mount IR lamps so that they illuminate what you want to capture in the videos. Adjustable mounts are best.
5. Mount USB hubs near where cameras will be deployed
   - route hub power cables to plug strip & secure with zip ties
   - plug into the thunderbolt ports of the mac as labeled during pre-setup
   - if an extender cable is needed, use a high speed/thunderbolt rated cable
   - faulty extension cables are a common cause of system instability or failure
6. Plug cameras into the hub ports in the positions previously labeled
7. Turn on USB ports you will use for the cameras, turn off the others (using local power buttons)
8. Turn on the mac and launch the camera code
9. Start recording and verify the camera views appear at the desired positions and have correct labels. If not, follow instructions above to rearrange until correct.
10. Finalize camera positions and focus them
    - position the camera so the field of view is as desired
    - mark the position of the GoPro mount base with a sharpie, then stick the adhesive base on
    - mount the camera on the GoPro bracket
    - fine-adjust the position and arm angles to get desired FOV, and tighten screws with wrenches to fix the camera position
    - Manually focus on an object at the distance you intend to video record; hand-tighten the focus lock ring
11. Put light blocking hood with IR-pass filter on, confirm image is good in the dark
12. Secure all cables with zip ties to absolutely minimize loose wires.

At this point the camera assignments should be stable.  

### Before the first run
The code comes with an Apple script `cam_start.scpt` and a configuration file `config.json` (both of which we recommend you move out of the ratrixcam folder and keep on the Mac’s desktop). You may rename these to anything you wish. 

Open `cam_start.scpt` in the Apple Script Editor by double clicking on its icon.  Edit the first path in the script to reflect where the ratrixcam folder has been installed on your computer. Edit the second path in the script to reflect the full path to the configuration file. Click the hammer icon to save your changes. You will not have to do these steps again unless you change paths or configuration files. 

Open the file `config.json` in a Python editor or any text editor, and edit the paths to indicate where various temporary and permanent data files should go. The setting:
`"save_path": "/Volumes/data/" `
determines where video output is saved. If you want to save to an external SSD drive and you name your SSD drive ‘data’, you can leave the default path as is. If you want to save files somewhere else, edit this setting. Note that the cameras won’t start if the specified path does not exist. The remaining paths are for temporary files, which we recommend putting on the desktop. If ratrixcam is installed on the desktop you only need to change “username” to the username you are logged in under on the Mac
```
  "temp_path": "/Users/username/Desktop/temp_videos/"
  "blank_image":"/Users/username/Desktop/ratrixcam/blanks/offline_status.png"
  "stills_path": "/Users/username/Desktop/temp_videos/status/" 
```
If you will be running the system with 8 cameras, these are the only things that must be edited for the code to run. If you have fewer than 8 cameras, you’ll need to reduce the number of cameras defined in the `config.json` file.

The descriptive strings for the study name and camera views can be edited in the config file, but you’ll also be able to do this in the configuration editor when you run the program (see User Manual). 

## For additional operating instructions see the User Manual






 


