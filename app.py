#object detection start
import cv2
import numpy as np
import os.path
from pelcoD import pelcoD
from tkinter import *
import binascii
import socket, datetime, threading, os, serial
from tkinter import messagebox
from PIL import Image, ImageTk
from flask import Flask
from flask import render_template
from flask import Response, make_response, jsonify, request
import imutils
# initialize flask app
app = Flask(__name__)


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
        ser.write(camsocket.panleft(25))
        req = request.get_json()
        print(req)
        res = make_response(jsonify({"message": "JSON received"}), 200)
        return res

@app.route('/stop', methods=["POST"])       
def stop():
        ser.write(camsocket.stop())
        req = request.get_json()
        print(req)
        res = make_response(jsonify({"message": "JSON received"}), 200)
        return res
@app.route('/pan_right', methods=["POST"])       
def pan_right():
        ser.write(camsocket.panright(25))
        req = request.get_json()
        print(req)
        res = make_response(jsonify({"message": "JSON received"}), 200)
        return res
@app.route('/tilt_down', methods=["POST"])       
def tilt_down():
        ser.write(camsocket.tiltdown(25))
        req = request.get_json()
        print(req)
        res = make_response(jsonify({"message": "JSON received"}), 200)
        return res
@app.route('/tilt_up', methods=["POST"])       
def tilt_up():
        ser.write(camsocket.tiltup(25))
        req = request.get_json()
        print(req)
        res = make_response(jsonify({"message": "JSON received"}), 200)
        return res
@app.route('/zoom_tele', methods=["POST"])       
def zoom_tele():
        ser.write(camsocket.zoomtele())
        req = request.get_json()
        print(req)
        res = make_response(jsonify({"message": "JSON received"}), 200)
        return res
@app.route('/zoom_wide', methods=["POST"])       
def zoom_wide():
        ser.write(camsocket.zoomwide())
        req = request.get_json()
        print(req)
        res = make_response(jsonify({"message": "JSON received"}), 200)
        return res
@app.route('/focus_near', methods=["POST"])       
def focus_near():
        ser.write(camsocket.focusnear())
        req = request.get_json()
        print(req)
        res = make_response(jsonify({"message": "JSON received"}), 200)
        return res
@app.route('/focus_far', methods=["POST"])       
def focus_far():
        ser.write(camsocket.focusfar())
        req = request.get_json()
        print(req)
        res = make_response(jsonify({"message": "JSON received"}), 200)
        return res
@app.route('/OSD_menu', methods=["POST"])       
def OSD_menu():
        ser.write(camsocket.auxon(2))
        req = request.get_json()
        print(req)
        res = make_response(jsonify({"message": "JSON received"}), 200)
        return res
@app.route('/Thermal_Camera', methods=["POST"])       
def Thermal_Camera():
        ser.write(camsocket.auxon(4))
        req = request.get_json()
        print(req)
        res = make_response(jsonify({"message": "JSON received"}), 200)
        return res
@app.route('/Visible_Light_Camera', methods=["POST"])       
def Visible_Light_Camera():
        ser.write(camsocket.auxoff(4))
        req = request.get_json()
        print(req)
        res = make_response(jsonify({"message": "JSON received"}), 200)
        return res
@app.route('/Windshield_Wiper', methods=["POST"])       
def Windshield_Wiper():
        ser.write(camsocket.auxon(1))
        req = request.get_json()
        print(req)
        res = make_response(jsonify({"message": "JSON received"}), 200)
        return res
@app.route('/Set_preset', methods=["POST"])       
def Set_preset():
        ser.write(camsocket.setpreset(3))
        req = request.get_json()
        print(req)
        res = make_response(jsonify({"message": "JSON received"}), 200)
        return res
@app.route('/Goto_preset', methods=["POST"])       
def Goto_preset():
        ser.write(camsocket.gotopreset(3))
        req = request.get_json()
        print(req)
        res = make_response(jsonify({"message": "JSON received"}), 200)
        return res
# Run each frame of the camera feed through the image detection model
def getimage():
    while True:
        global everyotherframe, DT, out
        global outputframe, sync
        success, img = cam.read()
        if not success:
            continue
        img = imutils.resize(img, width=1024, height = 768)
        everyotherframe = False
        if everyotherframe == True:
            height = img.shape[0]
            width = img.shape[1]
            blob = cv2.dnn.blobFromImage(img, 1/255, (416,416), (0,0,0),swapRB=True, crop=False)
            net.setInput(blob)
            output_layers_names = net.getUnconnectedOutLayersNames()
            layerOutputs = net.forward(output_layers_names)
            boxes = []
            confidences = []
            predclasss = []
            for output in layerOutputs:
                for detection in output:
                    scores = detection[5:]
                    predclass = np.argmax(scores)
                    confidence = scores[predclass]
                    if predclass != 0:
                        continue
                    if confidence > 0.2:
                        center_x = int(detection[0]*width)
                        center_y = int(detection[1]*height)
                        w = int(detection[2]*width)
                        h = int(detection[3]*height)
                        left = int(center_x - w/2)
                        top = int(center_y - h/2)
                        boxes.append([left,top,w,h])
                        confidences.append((float(confidence)))
                        predclasss.append(predclass)
                # Cull and Draw boxes on image
            FilterBoxes = cv2.dnn.NMSBoxes(boxes, confidences,0.2,0.2)
            font = cv2.FONT_HERSHEY_PLAIN
            if len(boxes) != 0:
                for i in FilterBoxes.flatten():
                    left,top,w,h = boxes[i]
                    label = str(objNames[predclasss[i]])
                    confidence = str(round(confidences[i], 2))
                    cv2.rectangle(img, (left,top), (left+w, top+h), (255,255,255), 3)
                    cv2.putText(img, label + confidence, (left,round(top-(20))), font, 1.2, (242,207,7), 2)
        out.write(img)
        currentDT = datetime.datetime.now()
        if (currentDT.day - (DT.day)) > 0:
            print('creating next video file')
            out.release()
            DT = datetime.datetime.now()
            formatDT = (str(DT.year)+'-'+str(DT.month)+'-'+str(DT.day)+'-'+str(DT.hour)+'-'+str(DT.minute))
            out = cv2.VideoWriter(formatDT+'.avi',cv2.VideoWriter_fourcc('D','I','V','X'),18,(1024,768))
            print(formatDT)
        with sync:  
            outputframe = img.copy()
# Convert the processed image to something displayable
def frametojpeg():
    global outputframe, sync
    while True:
        with sync:
            if outputframe is None:
                continue
            (flag,encodedimage)=cv2.imencode(".jpeg",outputframe)
            if not flag:
                continue
            encodedimage = bytearray()
            yield(b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + 
            bytearray(encodedimage) + b'\r\n')
if __name__ == '__main__':
    # Initialize all of the stuff
    everyotherframe = True
    cam = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    cv2.setUseOptimized(True)
    cv2.useOptimized()
    DT = datetime.datetime.now()
    formatDT = (str(DT.year)+'-'+str(DT.month)+'-'+str(DT.day)+'-'+str(DT.hour)+'-'+str(DT.minute))
    out = cv2.VideoWriter(formatDT+'.avi',cv2.VideoWriter_fourcc('D','I','V','X'),18,(1024,768))
    outputframe = None
    sync = threading.Lock()
    ser = serial.Serial('COM4',9600)
    camsocket =  pelcoD()
    objNames = []
    with open('coco.names','r') as f:
        objNames = f.read().splitlines()
    net = cv2.dnn.readNet('yolov3-tiny.weights','yolov3-tiny.cfg')
    # create threading for camera feed and start it
    t = threading.Thread(target = getimage)
    t.daemon = True
    t.start()
    # Run flask with debug mode
    app.run(host = "127.0.0.1",port = "5000",debug = True,threaded = True,use_reloader = False)


# Cleanup
out.release()
ser.close()
cam.release()




