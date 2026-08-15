from time import sleep, strftime
import sys
import os
import cv2
import numpy
import numpy as np
from pelcoD import pelcoD
from tkinter import *
import threading, serial
from PIL import Image
from flask import Flask
from flask import render_template
from flask import Response, make_response, jsonify, request
import atexit
from sys import exit
import time, datetime, os
import logging
from file_management import VideoFileManager
# Import the log rotation module
from log_rotator import setup_application_logging, LogManager
import signal  # Import signal module for handling SIGINT

# Flag to indicate shutdown in progress
shutdown_in_progress = False
    
app = Flask(__name__)

# Set up rotating logs
logger, log_manager = setup_application_logging(app_name="Camera_Control", log_level=logging.DEBUG)

# Log cleanup thread
def log_cleanup_thread():
    """Thread to periodically clean up old log files"""
    global shutdown_in_progress
    while not shutdown_in_progress:
        try:
            # Clean old logs every 24 hours
            log_manager.clean_old_logs()
            logger.info(f"Log cleanup performed. Current log size: {log_manager.get_logs_size():.2f} MB")
            
            # Sleep for 24 hours, but check for shutdown flag periodically
            for _ in range(8640):  # Check every 10 seconds for 24 hours
                if shutdown_in_progress:
                    break
                time.sleep(10)
        except Exception as e:
            logger.error(f"Error in log cleanup thread: {str(e)}")
            # Sleep for an hour before trying again
            time.sleep(3600)

def daytracker():
    global shutdown_in_progress
    
    while not shutdown_in_progress:
        try:
            # Schedule next file rotation at midnight
            dt = datetime.datetime.now()
            midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
            seconds_until_midnight = (midnight - dt).total_seconds()
            
            # Sleep until midnight or until shutdown, checking periodically
            for _ in range(int(seconds_until_midnight / 10) + 1):
                if shutdown_in_progress:
                    break
                time.sleep(min(10, seconds_until_midnight))
                seconds_until_midnight -= 10
            
            if not shutdown_in_progress:
                # Start new recording
                rotate_recording_file()
        except Exception as e:
            logger.error(f"Error in daytracker: {str(e)}")
            time.sleep(60)  # Sleep for a minute before trying again

def rotate_recording_file():
    """
    Rotates the video recording file at midnight or when called manually.
    This creates a new video file with the current date and stops the old recording.
    """
    try:
        global overwrite, out, file_manager
        
        # Signal the current recording to stop
        logger.info("Rotating recording file - creating new file")
        overwrite = True
        
        # Wait for current recording to finish
        sleep(3)
        
        # Reset flag to start a new recording
        overwrite = False
        
        # Create a new video file
        v = threading.Thread(target=writingVideo)
        v.daemon = True
        v.start()
        
        # Clean up old recordings using the file manager
        try:
            file_manager.cleanup_old_files()
            file_manager.check_disk_space()
            logger.info("File cleanup completed during rotation")
        except Exception as e:
            logger.error(f"Error during file cleanup: {str(e)}")
            
    except Exception as e:
        logger.error(f"Error rotating recording file: {str(e)}")
        
def create_directory_structure():
    base_dir = "recordings"
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    daily_dir = os.path.join(base_dir, today)
    
    # Create directories if they don't exist
    os.makedirs(daily_dir, exist_ok=True)
    return daily_dir
            

def frametojpeg():
    global outputframe, sync, shutdown_in_progress
    while not shutdown_in_progress:
        try:
            if outputframe is None:
                time.sleep(0.1)  # Short sleep to avoid CPU spinning
                continue
                
            encodedimage = numpy.ndarray
            (flag, encodedimage) = cv2.imencode(".jpeg", outputframe)
            
            if not flag:
                logger.error("Failed to encode JPEG image")
                time.sleep(0.5)  # Wait before trying again
                continue
                
            yield(b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + 
                  bytearray(encodedimage) + b'\r\n')
                  
        except Exception as e:
            logger.error(f'frametojpeg Error: {str(e)}')
            # Don't exit on error, just sleep and try again
            time.sleep(1)


# open the home page of the webserver
@app.route('/')
def index():
        return render_template('index.html')
# start infinite loop to grab camera frames
@app.route('/camera')
def camera():
        return Response(frametojpeg(), mimetype = "multipart/x-mixed-replace; boundary=frame")
@app.route('/pan_left', methods=["POST"])
def pan_left():
    req = request.get_json()
    try:
        if 'ser' in globals() and ser is not None:
            ser.write(camsocket.panleft(25))
        else:
            logger.warning("Serial connection not available for pan left command")
    except Exception as e:
        logger.error(f"Error sending pan left command: {str(e)}")
    res = make_response(jsonify({"message": "JSON received"}), 200)
    return res

@app.route('/stop', methods=["POST"])       
def stop():
    req = request.get_json()
    try:
        if 'ser' in globals() and ser is not None:
            ser.write(camsocket.stop())
        else:
            logger.warning("Serial connection not available for stop command")
    except Exception as e:
        logger.error(f"Error sending stop command: {str(e)}")
    res = make_response(jsonify({"message": "JSON received"}), 200)
    return res

@app.route('/pan_right', methods=["POST"])       
def pan_right():
    req = request.get_json()
    try:
        if 'ser' in globals() and ser is not None:
            ser.write(camsocket.panright(25))
        else:
            logger.warning("Serial connection not available for pan right command")
    except Exception as e:
        logger.error(f"Error sending pan right command: {str(e)}")
    res = make_response(jsonify({"message": "JSON received"}), 200)
    return res

@app.route('/tilt_down', methods=["POST"])       
def tilt_down():
    req = request.get_json()
    try:
        if 'ser' in globals() and ser is not None:
            ser.write(camsocket.tiltdown(25))
        else:
            logger.warning("Serial connection not available for tilt down command")
    except Exception as e:
        logger.error(f"Error sending tilt down command: {str(e)}")
    res = make_response(jsonify({"message": "JSON received"}), 200)
    return res

@app.route('/tilt_up', methods=["POST"])       
def tilt_up():
    req = request.get_json()
    try:
        if 'ser' in globals() and ser is not None:
            ser.write(camsocket.tiltup(25))
        else:
            logger.warning("Serial connection not available for tilt up command")
    except Exception as e:
        logger.error(f"Error sending tilt up command: {str(e)}")
    res = make_response(jsonify({"message": "JSON received"}), 200)
    return res

@app.route('/zoom_tele', methods=["POST"])       
def zoom_tele():
    req = request.get_json()
    try:
        if 'ser' in globals() and ser is not None:
            ser.write(camsocket.zoomtele())
        else:
            logger.warning("Serial connection not available for zoom tele command")
    except Exception as e:
        logger.error(f"Error sending zoom tele command: {str(e)}")
    res = make_response(jsonify({"message": "JSON received"}), 200)
    return res

@app.route('/zoom_wide', methods=["POST"])       
def zoom_wide():
    req = request.get_json()
    try:
        if 'ser' in globals() and ser is not None:
            ser.write(camsocket.zoomwide())
        else:
            logger.warning("Serial connection not available for zoom wide command")
    except Exception as e:
        logger.error(f"Error sending zoom wide command: {str(e)}")
    res = make_response(jsonify({"message": "JSON received"}), 200)
    return res

@app.route('/focus_near', methods=["POST"])       
def focus_near():
    req = request.get_json()
    try:
        if 'ser' in globals() and ser is not None:
            ser.write(camsocket.focusnear())
        else:
            logger.warning("Serial connection not available for focus near command")
    except Exception as e:
        logger.error(f"Error sending focus near command: {str(e)}")
    res = make_response(jsonify({"message": "JSON received"}), 200)
    return res

@app.route('/focus_far', methods=["POST"])       
def focus_far():
    req = request.get_json()
    try:
        if 'ser' in globals() and ser is not None:
            ser.write(camsocket.focusfar())
        else:
            logger.warning("Serial connection not available for focus far command")
    except Exception as e:
        logger.error(f"Error sending focus far command: {str(e)}")
    res = make_response(jsonify({"message": "JSON received"}), 200)
    return res

@app.route('/OSD_menu', methods=["POST"])       
def OSD_menu():
    req = request.get_json()
    try:
        if 'ser' in globals() and ser is not None:
            ser.write(camsocket.auxon(2))
        else:
            logger.warning("Serial connection not available for OSD menu command")
    except Exception as e:
        logger.error(f"Error sending OSD menu command: {str(e)}")
    res = make_response(jsonify({"message": "JSON received"}), 200)
    return res

@app.route('/Thermal_Camera', methods=["POST"])       
def Thermal_Camera():
    req = request.get_json()
    try:
        if 'ser' in globals() and ser is not None:
            ser.write(camsocket.auxon(4))
        else:
            logger.warning("Serial connection not available for thermal camera command")
    except Exception as e:
        logger.error(f"Error sending thermal camera command: {str(e)}")
    res = make_response(jsonify({"message": "JSON received"}), 200)
    return res

@app.route('/Visible_Light_Camera', methods=["POST"])       
def Visible_Light_Camera():
    req = request.get_json()
    try:
        if 'ser' in globals() and ser is not None:
            ser.write(camsocket.auxoff(4))
        else:
            logger.warning("Serial connection not available for visible light camera command")
    except Exception as e:
        logger.error(f"Error sending visible light camera command: {str(e)}")
    res = make_response(jsonify({"message": "JSON received"}), 200)
    return res

@app.route('/Windshield_Wiper', methods=["POST"])       
def Windshield_Wiper():
    req = request.get_json()
    try:
        if 'ser' in globals() and ser is not None:
            ser.write(camsocket.auxon(1))
        else:
            logger.warning("Serial connection not available for windshield wiper command")
    except Exception as e:
        logger.error(f"Error sending windshield wiper command: {str(e)}")
    res = make_response(jsonify({"message": "JSON received"}), 200)
    return res

@app.route('/Set_preset', methods=["POST"])       
def Set_preset():
    req = request.get_json()
    try:
        number = int(req['flabber'])
        if 'ser' in globals() and ser is not None:
            ser.write(camsocket.setpreset(number))
        else:
            logger.warning("Serial connection not available for set preset command")
    except Exception as e:
        logger.error(f"Error sending set preset command: {str(e)}")
    res = make_response(jsonify({"message": "JSON received"}), 200)
    return res

@app.route('/Goto_preset', methods=["POST"])       
def Goto_preset():
    req = request.get_json()
    try:
        number = int(req['flabber'])
        if 'ser' in globals() and ser is not None:
            ser.write(camsocket.gotopreset(number))
        else:
            logger.warning("Serial connection not available for goto preset command")
    except Exception as e:
        logger.error(f"Error sending goto preset command: {str(e)}")
    res = make_response(jsonify({"message": "JSON received"}), 200)
    return res

@app.route('/Tour_2', methods=["POST"])       
def Tour_2():
    req = request.get_json()
    try:
        if 'ser' in globals() and ser is not None:
            ser.write(camsocket.gotopreset(82))
        else:
            logger.warning("Serial connection not available for tour 2 command")
    except Exception as e:
        logger.error(f"Error sending tour 2 command: {str(e)}")
    res = make_response(jsonify({"message": "JSON received"}), 200)
    return res

@app.route('/Tour_1', methods=["POST"])       
def Tour_1():
    req = request.get_json()
    try:
        if 'ser' in globals() and ser is not None:
            ser.write(camsocket.gotopreset(81))
        else:
            logger.warning("Serial connection not available for tour 1 command")
    except Exception as e:
        logger.error(f"Error sending tour 1 command: {str(e)}")
    res = make_response(jsonify({"message": "JSON received"}), 200)
    return res

@app.route('/Start_New_File', methods=["POST"])       
def Start_New_File():
    global overwrite
    overwrite = True        
    sleep(3)
    overwrite = False
    v = threading.Thread(target = writingVideo)
    v.daemon = True
    v.start()
    req = request.get_json()
    print(req)
    res = make_response(jsonify({"message": "JSON received"}), 200)
    return res

@app.route('/Exit_program', methods=["POST"])       
def Exit_program():
    req = request.get_json()
    print(req)
    logger.info("Exit requested through web interface")
    # Schedule a clean shutdown
    threading.Thread(target=shutdown_application).start()
    res = make_response(jsonify({"message": "Server shutting down..."}), 200)
    return res

def shutdown_application():
    """Clean application shutdown sequence"""
    # Give the response time to be sent back to the client
    time.sleep(1)
    # Get the werkzeug server shutdown function
    func = request.environ.get('werkzeug.server.shutdown')
    if func is not None:
        func()
    else:
        # If werkzeug shutdown function is not available, use os._exit as last resort
        logger.warning("Werkzeug shutdown function not available, using os._exit")
        cleanup_and_exit()
        
def signal_handler(sig, frame):
    """Handle signals like SIGINT (Ctrl+C) and SIGTERM"""
    logger.info(f"Signal {sig} received, initiating graceful shutdown")
    cleanup_and_exit()

def cleanup_and_exit():
    """Perform all necessary cleanup and exit the application"""
    global shutdown_in_progress, out, ser, cam
    
    if shutdown_in_progress:
        # Already shutting down, avoid double cleanup
        return
        
    # Set shutdown flag to stop ongoing operations in threads
    shutdown_in_progress = True
    
    logger.info('Initiating cleanup sequence')
    
    try:
        # Give video writing thread a chance to complete
        logger.info('Stopping video recording')
        global overwrite
        overwrite = True
        time.sleep(2)  # Wait for video loop to break
        
        # Clean up resources
        try:
            if out is not None and hasattr(out, 'release'):
                logger.info('Releasing video writer')
                out.release()
        except Exception as e:
            logger.error(f'Error releasing video writer: {str(e)}')
            
        try:
            if 'ser' in globals() and ser is not None:
                logger.info('Closing serial connection')
                ser.close()
        except Exception as e:
            logger.error(f'Error closing serial connection: {str(e)}')
            
        try:
            if 'cam' in globals() and cam is not None:
                logger.info('Releasing camera')
                cam.release()
        except Exception as e:
            logger.error(f'Error releasing camera: {str(e)}')
            
        logger.info('Cleanup complete, exiting application')
    except Exception as e:
        logger.error(f'Error during cleanup: {str(e)}')
    
    # Exit with success code
    os._exit(0)
    
# Legacy exit handler for compatibility with atexit
def exit_handler():
    logger.info('Legacy exit handler called')
    cleanup_and_exit()

def writingVideo():
    try:
        global shutdown_in_progress, overwrite, outputframe, out
        
        # Wait for camera to initialize fully
        time.sleep(5)
        
        # Ensure we have a frame before creating the video writer
        retry_count = 0
        while outputframe is None and retry_count < 10 and not shutdown_in_progress:
            logger.debug("Waiting for first valid camera frame...")
            time.sleep(2)
            retry_count += 1
            
        if outputframe is None:
            logger.error("Failed to get valid frame for video recording")
            return
            
        # Run cleanup of old files before starting new recording
        deleteold()
        
        # Initialize video recording parameters
        starttime = time.time()
        fpslimit = .066  # ~15fps
        
        # Create a timestamped filename
        td = datetime.datetime.now()
        date = td.strftime("%Y-%m-%d-%H-%M-%S")
        video_path = './videos/'+date+'.avi'
        
        # Ensure videos directory exists
        os.makedirs(os.path.dirname(video_path), exist_ok=True)
        
        # Use a try block specifically for video writer initialization
        try:
            # Get frame dimensions
            height = outputframe.shape[0]
            width = outputframe.shape[1]
            
            # Create video writer
            out = cv2.VideoWriter(
                video_path,
                cv2.VideoWriter_fourcc('H','2','6','4'),
                15,  # fps
                (width, height)
            )
            
            if not out.isOpened():
                logger.error(f"Failed to open video writer for {video_path}")
                return
                
            logger.info(f'Started new video recording: {video_path}')
            frames_written = 0
            last_frame = None  # Store last frame for comparison
            last_stall_check = time.time()  # Time of last stall check
            stall_check_interval = 10  # Check for stalls every 10 seconds
            
            # Main recording loop
            while not shutdown_in_progress:
                currenttime = time.time()
                
                # Check if we need to rotate the video file
                if overwrite is True:
                    logger.info("Video rotation flag detected, ending current recording")
                    break
                    
                # Write frame at specified interval
                if (currenttime - starttime) > fpslimit:
                    # Check for valid frame
                    if outputframe is not None:
                        try:
                            # Check for actual frame changes
                            if currenttime - last_stall_check >= stall_check_interval:
                                if last_frame is not None and numpy.array_equal(outputframe, last_frame):
                                    logger.warning("No new frames detected in last interval, possible camera issue")
                                last_frame = outputframe.copy()  # Store current frame for next comparison
                                last_stall_check = currenttime
                            
                            out.write(outputframe)
                            frames_written += 1
                            starttime = time.time()
                            
                            # Periodically log progress
                            if frames_written % 1000 == 0:
                                logger.debug(f"Recorded {frames_written} frames to {video_path}")
                        except Exception as e:
                            logger.error(f"Error writing video frame: {str(e)}")
                    else:
                        logger.warning("Skipping frame write - no valid frame available")
                        
                # Small sleep to prevent CPU spinning
                time.sleep(0.01)
                
            # Properly close the file
            logger.info(f"Finalizing video file with {frames_written} frames")
            if out is not None and hasattr(out, 'release'):
                out.release()
                
            logger.debug(f"Video recording completed: {video_path}")
            deleteold()  # Clean up old files after successful recording
            
        except Exception as e:
            logger.error(f"Error during video recording process: {str(e)}")
            # Make sure to release the writer even on error
            if out is not None and hasattr(out, 'release'):
                out.release()
                
    except Exception as e:
        logger.error(f'writingVideo outer error: {str(e)}')
        # Ensure video writer is released on any exception
        try:
            if out is not None and hasattr(out, 'release'):
                out.release()
        except Exception as inner_e:
            logger.error(f"Error releasing video writer in exception handler: {str(inner_e)}")
            
    return

def getimage():
    global shutdown_in_progress, cam, outputframe
    consecutive_failures = 0
    max_failures = 10
    
    while not shutdown_in_progress:
        try:
            # Check if camera is connected/accessible
            if cam is None or not cam.isOpened():
                # Try to reopen the camera if it was disconnected
                logger.warning("Camera disconnected, attempting to reconnect...")
                if cam is not None:
                    cam.release()  # Close existing camera object if present
                
                # Create a new camera connection
                cam = cv2.VideoCapture(0)
                if cam.isOpened():
                    logger.info("Camera successfully reconnected")
                    consecutive_failures = 0
                else:
                    logger.error("Failed to reconnect to camera")
                    consecutive_failures += 1
                    time.sleep(5)  # Wait before retry
                    continue
                    
            # Read a frame from the camera
            success, img = cam.read()
            if success:
                consecutive_failures = 0  # Reset failure counter on success
                td = datetime.datetime.now()
                timestamp = td.strftime("%m/%d/%Y %H:%M:%S")
                cv2.putText(img, str(timestamp), (10, 475), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
                outputframe = img
            else:
                logger.warning("Failed to read frame from camera")
                consecutive_failures += 1
                time.sleep(1)
                
            # If we've had too many consecutive failures, try to reset the camera
            if consecutive_failures >= max_failures:
                logger.error(f"Too many consecutive camera failures ({max_failures}), attempting to reset camera connection")
                if cam is not None:
                    cam.release()
                cam = None  # Force reconnection on next iteration
                consecutive_failures = 0
                time.sleep(5)
                
        except Exception as e:
            logger.error(f'getImage Error: {str(e)}')
            consecutive_failures += 1
            if not shutdown_in_progress:
                time.sleep(1)  # Avoid tight loop if camera fails

def deleteold():
    try:
        path = "./videos/"
        now = time.time()
        for filename in os.listdir(path):
            filestamp = os.stat(os.path.join(path, filename)).st_mtime
            filecompare = now - 108000
            if filestamp < filecompare:
                logger.debug(f'Deleted {filename}')
                try:
                    os.remove(path+filename)
                except Exception as e:
                    logger.error(f'Could not delete file {filename}: {str(e)}')
    except Exception as e:
        logger.error(f'deleteold Error: {str(e)}')
        # Don't exit the program on file management errors
        return False
    return True

def videolist():
        path = "./videos/"
        mylist = os.listdir(path)
        for v, videos in enumerate(mylist):
                mytuple = [(v,videos)]
        return mytuple        


if __name__ == '__main__':
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Also keep the atexit handler for normal termination
    atexit.register(exit_handler)
    
    # Initialize all of the stuff
    path = "./videos/"
    file_manager = VideoFileManager(
        retention_days=5,
        max_disk_usage_gb=500
    )
    
    # Create required directories
    if not os.path.exists('videos'):
        os.makedirs('videos')
    if not os.path.exists('logs'):
        os.makedirs('logs')
    
    # Log initial status
    logger.info("Camera Control application starting")
    logger.info(f"Python version: {sys.version}")
    logger.info(f"OpenCV version: {cv2.__version__}")
    
    # Initialize video and camera resources
    out = cv2.VideoWriter()
    everyotherframe = True
    cam = cv2.VideoCapture(0)
    if not (cam.isOpened()):
        logger.error('Could not access camera.')
    cv2.setUseOptimized(True)
    cv2.useOptimized()
    outputframe = None
    sync = threading.Lock()
    
    # Initialize serial connection
    try:    
        serialportname = "/dev/ttyUSB0"
        ser = serial.Serial(serialportname,9600)
        logger.info(f"Serial port {serialportname} opened successfully")
    except Exception as e:
        logger.error(f'Serial Communications not established: {str(e)}')
        ser = None  # Set ser to None so we can check if it exists later
        
    camsocket = pelcoD()
    objNames = []
    
    # Set global flag for shutdown coordination
    shutdown_in_progress = False
    
    # Start image capture thread
    t = threading.Thread(target=getimage)
    t.daemon = True
    t.start()
    
    overwrite = False
    
    # Start video recording thread
    v = threading.Thread(target=writingVideo)
    v.daemon = True
    v.start()
    
    # Start day tracker thread
    d = threading.Thread(target=daytracker)
    d.daemon = True
    d.start()
    
    # Start log cleanup thread
    l = threading.Thread(target=log_cleanup_thread)
    l.daemon = True
    l.start()
    
    FilterBoxes = None
    
    # Log application start
    logger.info("All threads initialized. Starting web server...")
    
    try:
        # Start Flask server
        app.run(host = "0.0.0.0",port = "5000",debug = False,threaded = True,use_reloader = False)
    except KeyboardInterrupt:
        # This is here as an extra safety net in case the signal handler doesn't catch it
        logger.info("KeyboardInterrupt caught, shutting down")
        cleanup_and_exit()
    finally:
        # Make sure cleanup happens even if app.run fails
        cleanup_and_exit()







