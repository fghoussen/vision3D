#!/usr/bin/env python3
# -*- coding:utf-8 -*-

# Imports.
import os
import numpy as np
from PyQt5.QtCore import QRunnable, pyqtSignal, QObject
import threading
import cv2
import logging
import time

logger = logging.getLogger('post')

class PostThreadSignals(QObject):
    # Signals enabling to update application from thread.
    updatePostFrame = pyqtSignal(np.ndarray, str, str) # Update postprocessed frame (depth, ...).

class PostThread(QRunnable): # QThreadPool must be used with QRunnable (NOT QThread).
    def __init__(self, args, threadLeft, threadRight, vision3D):
        # Initialise.
        super().__init__()
        self._args = args.copy()
        self._run = True
        self._post = {'left': None, 'right': None}
        self._postLock = threading.Lock()
        self.signals = PostThreadSignals()
        self._stereo = None
        self._stitcher = cv2.Stitcher_create(cv2.Stitcher_PANORAMA)
        self._wsdColors = [] # Color used to segment with watershed.

        # Event subscribe.
        threadLeft.signals.updatePrepFrame.connect(self.updatePrepFrame)
        threadRight.signals.updatePrepFrame.connect(self.updatePrepFrame)
        vision3D.signals.changeParam.connect(self.onParameterChanged)
        vision3D.signals.stop.connect(self.stop)

        # Set up info/debug log on demand.
        logging.basicConfig(format='%(asctime)s %(message)s', datefmt='%H:%M:%S', level=logging.INFO)

        # Set up detection.
        labels = open('coco.names').read().strip().split("\n") # Load the COCO class labels.
        np.random.seed(42) # Initialize colors to represent each possible class label.
        colors = np.random.randint(0, 255, size=(len(labels), 3), dtype="uint8")
        self._detect = {'YOLO': {}, 'SSD': {}}
        self._setupYOLO(labels, colors)
        self._setupSSD(labels, colors)
        for key in self._detect:
            net = self._detect[key]['net']
            if self._args['hardware'] == 'arm-jetson':
                net.setPreferableBackend(cv2.dnn.DNN_BACKEND_CUDA)
                net.setPreferableTarget(cv2.dnn.DNN_TARGET_CUDA)
            else:
                net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
                net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)

    def onParameterChanged(self, param, objType, value):
        # Lots of events may be spawned: check impact is needed.
        newValue = None
        if objType == 'int':
            newValue = int(value)
        elif objType == 'double':
            newValue = float(value)
        elif objType == 'bool':
            newValue = bool(value)
        elif objType == 'str':
            newValue = str(value)
        else:
            assert True, 'unknown type.'
        if self._args[param] == newValue:
            return # Nothing to do.

        # Update logger level.
        if self._args['DBGpost']:
            logger.setLevel(logging.DEBUG)
        else:
            logger.setLevel(logging.INFO)

        # Adapt parameter values to allowed values.
        if param == 'numDisparities': # Must be divisible by 16.
            newValue = (newValue//16)*16
        if param == 'blockSize': # Must be odd.
            newValue = (newValue//2)*2+1

        # Reset stereo.
        if param == 'numDisparities' or param == 'blockSize':
            self._stereo = None

        # Apply change.
        self._args[param] = newValue

    def run(self):
        # Execute post-processing.
        while self._run:
            # Debug on demand.
            if self._args['DBGpost']:
                msg = '[post-run]'
                msg += self._generateMessage()
                logger.debug(msg)

            # Checks.
            if self._post['left'] is None or self._post['right'] is None:
                continue

            # Get frames to postprocess.
            self._postLock.acquire()
            frameL = self._post['left'].copy()
            frameR = self._post['right'].copy()
            self._postLock.release()

            # Postprocess.
            start = time.time()
            frame, fmt, msg = np.ones(frameL.shape, np.uint8), 'GRAY', 'None'
            try:
                for key in ['detectHits', 'dnnTime']:
                    if key in self._args:
                        del self._args[key]

                if self._args['detection']:
                    frameL, fmt, msgL = self._runDetection(frameL)
                    frameR, fmt, msgR = self._runDetection(frameR)
                    msg = 'detection %s: '%self._args['detectMode'] + msgL + ', ' + msgR
                    frame = np.concatenate((frameL, frameR), axis=1)
                elif self._args['depth']:
                    frame, fmt, msg = self._runDepth(frameL, frameR)
                    msg = 'depth: ' + msg
                elif self._args['keypoints']:
                    frame, fmt, msg = self._runKeypoints(frameL, frameR)
                    msg = '%s keypoints: '%self._args['kptMode'] + msg
                elif self._args['stitch']:
                    frame, fmt, msg = self._runStitch(frameL, frameR)
                    msg = 'stitch: ' + msg
                elif self._args['segmentation']:
                    frameL, fmt, msgL = self._runSegmentation(frameL)
                    frameR, fmt, msgR = self._runSegmentation(frameR)
                    msg = '%s segmentation: '%self._args['segMode'] + msgL + ', ' + msgR
                    frame = np.concatenate((frameL, frameR), axis=1)
            except:
                if msg == '': # Otherwise, keep more relevant message.
                    msg = 'OpenCV exception!...'
            stop = time.time()
            self._args['postTime'] = stop - start
            msg += ' - ' + 'time %.3f s'%self._args['postTime']

            # Get image back to application.
            start = time.time()
            self.signals.updatePostFrame.emit(frame, fmt, msg)
            stop = time.time()
            self._args['updatePostFrameTime'] = stop - start
            self._args['updatePostFrameSize'] = frame.nbytes

    def stop(self):
        # Stop thread.
        self._run = False

    def updatePrepFrame(self, frame, dct):
        # Postprocess incoming frame.
        self._postLock.acquire()
        side = dct['side']
        self._post[side] = frame # Refresh frame.
        self._postLock.release()

    def _setupYOLO(self, labels, colors):
        # Load our YOLO object detector trained on COCO dataset (80 classes).
        net = cv2.dnn.readNetFromDarknet('yolov3-tiny.cfg', 'yolov3-tiny.weights')

        # Determine only the *output* layer names that we need.
        ln = net.getLayerNames()
        ln = [ln[idx - 1] for idx in net.getUnconnectedOutLayers()]

        # Remind YOLO setup.
        self._detect['YOLO']['labels'] = labels
        self._detect['YOLO']['colors'] = colors
        self._detect['YOLO']['net'] = net
        self._detect['YOLO']['ln'] = ln

    def _setupSSD(self, labels, colors):
        # Load our SSD object detector.
        protoTxt = os.path.join('models_VGGNet_coco_SSD_512x512', 'models', 'VGGNet',
                                'coco', 'SSD_512x512', 'deploy.prototxt')
        caffemodel = os.path.join('models_VGGNet_coco_SSD_512x512', 'models', 'VGGNet',
                                  'coco', 'SSD_512x512', 'VGG_coco_SSD_512x512_iter_360000.caffemodel')
        net = cv2.dnn.readNetFromCaffe(protoTxt, caffemodel)

        # Determine only the *output* layer names that we need.
        ln = net.getLayerNames()
        ln = [ln[idx - 1] for idx in net.getUnconnectedOutLayers()]

        # Remind SSD setup.
        self._detect['SSD']['labels'] = labels
        self._detect['SSD']['colors'] = colors
        self._detect['SSD']['net'] = net
        self._detect['SSD']['ln'] = ln

    def _runDetection(self, frame):
        # Construct a blob from the input frame.
        blob = cv2.dnn.blobFromImage(frame, 1 / 255.0, (416, 416), swapRB=True, crop=False)

        # Perform a forward pass of the YOLO object detector.
        detectMode = self._args['detectMode']
        detect = self._detect[detectMode]
        net, ln = detect['net'], detect['ln']
        net.setInput(blob)
        start = time.time()
        layerOutputs = net.forward(ln)
        stop = time.time()
        self._args['dnnTime'] = stop - start

        # Initialize our lists of detected bounding boxes, confidences, and class IDs.
        boxes, confidences, classIDs = [], [], []

        # Loop over each of the layer outputs.
        height, width = frame.shape[:2]
        for output in layerOutputs:
            # Loop over each of the detections.
            for detection in output:
                # Extract the class ID and confidence (i.e., probability) of the current detection.
                scores = detection[5:]
                if len(scores) == 0:
                    continue
                classID = np.argmax(scores)
                confidence = scores[classID]

                # Filter out weak predictions by ensuring the detected probability is greater than the minimum probability.
                if confidence > self._args['confidence']:
                    # Scale the bounding box coordinates back relative to the size of the image, keeping in mind that YOLO
                    # returns the center (x, y)-coordinates of the bounding box followed by the boxes' width and height.
                    box = detection[0:4] * np.array([width, height, width, height])
                    boxCenterX, boxCenterY, boxWidth, boxHeight = box.astype("int")

                    # Use the center (x, y)-coordinates to derive the top and and left corner of the bounding box.
                    boxCenterX = int(boxCenterX - (boxWidth / 2))
                    boxCenterY = int(boxCenterY - (boxHeight / 2))

                    # Update our list of bounding box coordinates, confidences, and class IDs
                    boxes.append([boxCenterX, boxCenterY, int(boxWidth), int(boxHeight)])
                    confidences.append(float(confidence))
                    classIDs.append(classID)

        # Check if we have detected some objects.
        self._args['detectHits'] = len(boxes)
        if len(boxes) == 0:
            msg = 'no hit'
            return frame, 'BGR', msg

        # Apply non-maxima suppression to suppress weak, overlapping bounding boxes.
        idxs = cv2.dnn.NMSBoxes(boxes, confidences, self._args['confidence'], self._args['nms'])
        self._args['detectHits'] = len(idxs)

        # Ensure at least one detection exists.
        colors, labels = detect['colors'], detect['labels']
        if len(idxs) > 0:
            # Loop over the indexes we are keeping.
            for idx in idxs.flatten():
                # Extract the bounding box coordinates.
                (boxCenterX, boxCenterY) = (boxes[idx][0], boxes[idx][1])
                (boxWidth, boxHeight) = (boxes[idx][2], boxes[idx][3])

                # Draw a bounding box rectangle and label on the frame.
                color = [int(clr) for clr in colors[classIDs[idx]]]
                cv2.rectangle(frame, (boxCenterX, boxCenterY), (boxCenterX + boxWidth, boxCenterY + boxHeight), color, 2)
                text = "{}: {:.4f}".format(labels[classIDs[idx]], confidences[idx])
                cv2.putText(frame, text, (boxCenterX, boxCenterY - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
        msg = '%d hit(s)'%self._args['detectHits']

        return frame, 'BGR', msg

    def _runDepth(self, frameL, frameR):
        # Convert frames to grayscale.
        grayL = self._convertToGrayScale(frameL)
        grayR = self._convertToGrayScale(frameR)

        # Compute depth map.
        if self._stereo is None:
            self._stereo = cv2.StereoBM_create(numDisparities=self._args['numDisparities'],
                                               blockSize=self._args['blockSize'])
        disparity = self._stereo.compute(grayL, grayR)
        scaledDisparity = disparity - np.min(disparity)
        if np.max(scaledDisparity) > 0:
            scaledDisparity = scaledDisparity * (255/np.max(scaledDisparity))
        frame = scaledDisparity.astype(np.uint8)
        msg = 'range 0-255, mean %03d, std %03d'%(np.mean(scaledDisparity), np.std(scaledDisparity))

        return frame, 'GRAY', msg

    def _computeKeypoints(self, frameL, frameR):
        # To achieve more accurate results, convert frames to grayscale.
        grayL = self._convertToGrayScale(frameL)
        grayR = self._convertToGrayScale(frameR)

        # Detect keypoints.
        kptMode, nbFeatures, msg = None, self._args['nbFeatures'], ''
        if self._args['kptMode'] == 'ORB':
            kptMode = cv2.ORB_create(nfeatures=nbFeatures)
        elif self._args['kptMode'] == 'SIFT':
            kptMode = cv2.SIFT_create(nfeatures=nbFeatures)
        kptL, dscL = kptMode.detectAndCompute(grayL, None)
        kptR, dscR = kptMode.detectAndCompute(grayR, None)
        if len(kptL) == 0 or len(kptR) == 0:
            msg = 'KO no keypoint'
            return kptL, kptR, [], msg

        # Match keypoints.
        norm = None
        if self._args['kptMode'] == 'ORB':
            norm = cv2.NORM_HAMMING # Better for ORB.
        elif self._args['kptMode'] == 'SIFT':
            norm = cv2.NORM_L2 # Better for SIFT.
        bf = cv2.BFMatcher(norm, crossCheck=False) # Need crossCheck=False for knnMatch.
        matches = bf.knnMatch(dscL, dscR, k=2) # knnMatch crucial to get 2 matches m1 and m2.
        if len(matches) == 0:
            msg = 'KO no match'
            return kptL, kptR, matches, msg

        # To keep only strong matches.
        bestMatches = []
        for m1, m2 in matches: # For every descriptor, take closest two matches.
            if m1.distance < 0.6 * m2.distance: # Best match has to be closer than second best.
                bestMatches.append(m1) # Lowe’s ratio test.
        if len(bestMatches) == 0:
            msg = 'KO no best match'
            return kptL, kptR, bestMatches, msg

        return kptL, kptR, bestMatches, msg

    def _runKeypoints(self, frameL, frameR):
        # Compute keypoints.
        kptL, kptR, bestMatches, msg = self._computeKeypoints(frameL, frameR)
        if len(kptL) == 0 or len(kptR) == 0 or len(bestMatches) == 0:
            frame = np.ones(frameL.shape, np.uint8) # Black image.
            return frame, 'GRAY', msg

        # Draw matches.
        frame = cv2.drawMatches(frameL, kptL, frameR, kptR, bestMatches, None)
        minDist = np.min([match.distance for match in bestMatches])
        msg = 'nb best matches %03d, min distance %.3f'%(len(bestMatches), minDist)

        return frame, 'BGR', msg

    def _runStitch(self, frameL, frameR):
        # Compute keypoints.
        kptL, kptR, bestMatches, msg = self._computeKeypoints(frameL, frameR)

        # Stitch images.
        frame, fmt, msg = None, None, ''
        minMatchCount = 10 # Need at least 10 points to find homography.
        if len(bestMatches) > minMatchCount:
            # Find homography (RANSAC).
            srcPts = np.float32([kptL[match.queryIdx].pt for match in bestMatches]).reshape(-1, 1, 2)
            dstPts = np.float32([kptR[match.trainIdx].pt for match in bestMatches]).reshape(-1, 1, 2)
            homo, mask = cv2.findHomography(srcPts, dstPts, cv2.RANSAC, 5.0)

            # Warp perspective: change field of view.
            rowsL, colsL = frameL.shape[:2]
            rowsR, colsR = frameR.shape[:2]
            lsPtsL = np.float32([[0, 0], [0, rowsL], [colsL, rowsL], [colsL, 0]]).reshape(-1, 1, 2)
            lsPtsR = np.float32([[0, 0], [0, rowsR], [colsR, rowsR], [colsR, 0]]).reshape(-1, 1, 2)
            lsPtsR = cv2.perspectiveTransform(lsPtsR, homo)

            # Stitch images.
            lsPts = np.concatenate((lsPtsL, lsPtsR), axis=0)
            [xMin, yMin] = np.int32(lsPts.min(axis=0).ravel() - 0.5)
            [xMax, yMax] = np.int32(lsPts.max(axis=0).ravel() + 0.5)
            transDist = [-xMin, -yMin] # Translation distance.
            homoTranslation = np.array([[1, 0, transDist[0]], [0, 1, transDist[1]], [0, 0, 1]])
            frame = cv2.warpPerspective(frameR, homoTranslation.dot(homo), (xMax-xMin, yMax-yMin))
            frame[transDist[1]:rowsL+transDist[1], transDist[0]:colsL+transDist[0]] = frameL
            fmt = 'BGR'
            msg += 'OK (%03d %s keypoints)'%(len(bestMatches), self._args['kptMode'])

            # Crop on demand.
            if self._args['crop']:
                frame = self._cropFrame(frame)

            # Resize frame to initial frame size (to avoid huge change of dimensions that may occur).
            frame = cv2.resize(frame, (colsL, rowsR), interpolation = cv2.INTER_LINEAR)
        else:
            frame = np.ones(frameL.shape, np.uint8) # Black image.
            fmt = 'GRAY'
            msg += 'KO not enough matches found (%03d/%03d)'%(len(bestMatches), minMatchCount)

        return frame, fmt, msg

    def _runSegmentationWatershed(self, frame):
        # Convert to gray scale.
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        ret, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        # Remove noise.
        kernel = np.ones((3, 3), np.uint8)
        opening = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=2)

        # Finding sure background area.
        sureBg = cv2.dilate(opening, kernel, iterations=3)

        # Finding sure foreground area.
        distTransform = cv2.distanceTransform(opening, cv2.DIST_L2, 5)
        ret, sureFg = cv2.threshold(distTransform, 0.7*distTransform.max(), 255, 0)

        # Finding unknown region.
        sureFg = np.uint8(sureFg)
        unknown = cv2.subtract(sureBg, sureFg)

        # Marker labelling.
        ret, markers = cv2.connectedComponents(sureFg)

        # Add one to all labels to make sure background is not 0, but 1.
        markers = markers+1

        # Now, mark the region of unknown with zero.
        markers[unknown==255] = 0

        # Run watershed segmentation.
        markers = cv2.watershed(frame, markers)
        fmt = 'BGR'
        uniqueMarkers = np.unique(markers)
        msg = 'OK (%03d markers)'%len(uniqueMarkers)

        # Color image according to different region centered/attributed to each marker.
        if len(self._wsdColors) < len(uniqueMarkers):
            self._wsdColors = np.random.choice(range(256), size=(len(uniqueMarkers), 3))
        for idx, uniqueMarker in enumerate(uniqueMarkers):
            frame[markers == uniqueMarker] = self._wsdColors[idx]

        return frame, fmt, msg

    def _runSegmentationKMeans(self, frame):
        # Run KMeans segmentation.
        frame = np.float32(frame)
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0)
        K, attempts = self._args['K'], self._args['attempts']
        ret, label, center = cv2.kmeans(frame, K, None, criteria, attempts, cv2.KMEANS_RANDOM_CENTERS)

        # Convert back to image.
        shape = frame.shape
        center = np.uint8(center)
        frame = center[label.flatten()]
        frame = frame.reshape(shape)
        fmt = 'BGR'
        msg = 'OK (K=%02d, attempts=%02d)'%(self._args['K'], self._args['attempts'])

        return frame, fmt, msg

    def _runSegmentation(self, frame):
        # Run segmentation.
        if self._args['segMode'] == 'Watershed':
            frame, fmt, msg = self._runSegmentationWatershed(frame)
        elif self._args['segMode'] == 'KMeans':
            frame, fmt, msg = self._runSegmentationKMeans(frame)

        return frame, fmt, msg

    @staticmethod
    def _cropFrame(frame):
        # Add black borders around frame to ease thresholding.
        frame = cv2.copyMakeBorder(frame, 10, 10, 10, 10, cv2.BORDER_CONSTANT, (0, 0, 0))

        # Thresholding (gray scale).
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        thrFrame = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY)[1]

        # Find biggest contour in thresholded frame.
        cnts, _ = cv2.findContours(thrFrame.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        roiArea = max(cnts, key=cv2.contourArea)
        mask = np.zeros(thrFrame.shape, dtype="uint8")
        xCenter, yCenter, width, height = cv2.boundingRect(roiArea)
        cv2.rectangle(mask, (xCenter, yCenter), (xCenter + width, yCenter + height), 255, -1)

        # Find best Region Of Interest (ROI).
        roiMinRectangle = mask.copy()
        sub = mask.copy()
        while cv2.countNonZero(sub) > 0:
            roiMinRectangle = cv2.erode(roiMinRectangle, None)
            sub = cv2.subtract(roiMinRectangle, thrFrame)

        # Get best Region Of Interest (ROI).
        cnts, _ = cv2.findContours(roiMinRectangle.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        roiArea = max(cnts, key=cv2.contourArea)

        # Crop.
        xCenter, yCenter, width, height = cv2.boundingRect(roiArea)
        frame = frame[yCenter:yCenter + height, xCenter:xCenter + width]

        return frame

    @staticmethod
    def _convertToGrayScale(frame):
        # Convert to gray scale if needed.
        convertToGrayScale = True
        if len(frame.shape) == 3: # RGB, BGR or GRAY.
            if frame.shape[2] == 1: # GRAY.
                convertToGrayScale = False
        else: # GRAY.
            convertToGrayScale = False
        if convertToGrayScale:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        return frame

    def _generateMessage(self):
        # Generate message from options.
        msg = ''
        if self._args['DBGrun']:
            msg += ', detect %s'%self._args['detection']
            if self._args['detection']:
                msg += ', detectMode %s'%self._args['detectMode']
                msg += ', conf %.3f'%self._args['confidence']
                msg += ', nms %.3f'%self._args['nms']
                if 'detectHits' in self._args:
                    msg += ', detectHits %d'%self._args['detectHits']
            msg += ', depth %s'%self._args['depth']
            if self._args['depth']:
                msg += ', numDisparities %d'%self._args['numDisparities']
                msg += ', blockSize %d'%self._args['blockSize']
            msg += ', keypoints %s'%self._args['keypoints']
            if self._args['keypoints']:
                msg += ', kptMode %s'%self._args['kptMode']
                msg += ', nbFeatures %d'%self._args['nbFeatures']
            msg += ', stitch %s'%self._args['stitch']
            if self._args['stitch']:
                msg += ', crop %s'%self._args['crop']
            msg += ', segmentation %s'%self._args['segmentation']
            if self._args['segmentation']:
                msg += ', segMode %s'%self._args['segMode']
                msg += ', K %s'%self._args['K']
                msg += ', attempts %s'%self._args['attempts']
        if self._args['DBGprof']:
            if self._args['detection']:
                msg += ', detection %s'%self._args['detection']
            if self._args['depth']:
                msg += ', depth'
            if self._args['keypoints']:
                msg += ', keypoints'
            if self._args['stitch']:
                msg += ', stitch'
            if 'postTime' in self._args:
                msg += ', postTime %.3f'%self._args['postTime']
            if 'dnnTime' in self._args:
                msg += ', dnnTime %.3f'%self._args['dnnTime']
        if self._args['DBGcomm']:
            msg += ', comm'
            if 'updatePostFrameTime' in self._args:
                msg += ', updatePostFrameTime %.3f'%self._args['updatePostFrameTime']
            if 'updatePostFrameSize' in self._args:
                msg += ', updatePostFrameSize %d'%self._args['updatePostFrameSize']

        return msg
