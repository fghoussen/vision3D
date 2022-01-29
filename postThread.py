#!/usr/bin/env python3
# -*- coding:utf-8 -*-

# Imports.
import numpy as np
from PyQt5.QtCore import QRunnable, pyqtSignal, QObject
import threading
import cv2
import logging

logger = logging.getLogger('post')

class PostThreadSignals(QObject):
    # Signals enabling to update application from thread.
    updatePostFrame = pyqtSignal(np.ndarray, str, str) # Update postprocessed frame (depth, ...).

class PostThread(QRunnable): # QThreadPool must be used with QRunnable (NOT QThread).
    def __init__(self, args, vision3D, threadLeft, threadRight):
        # Initialise.
        super().__init__()
        self._args = args.copy()
        vision3D.signals.changeParam.connect(self.onParameterChanged)
        vision3D.signals.stop.connect(self.stop)
        threadLeft.signals.updatePrepFrame.connect(self.updatePrepFrame)
        threadRight.signals.updatePrepFrame.connect(self.updatePrepFrame)
        self._run = True
        self._post = {'left': None, 'right': None}
        self._postLock = threading.Lock()
        self.signals = PostThreadSignals()
        self._stereo = None
        self._stitcher = cv2.Stitcher_create(cv2.Stitcher_PANORAMA)

        # Set up info/debug log on demand.
        logging.basicConfig(format='%(asctime)s %(message)s', datefmt='%H:%M:%S', level=logging.INFO)

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
                msg += ' depth %s'%self._args['depth']
                if self._args['depth']:
                    msg += ', numDisparities %d'%self._args['numDisparities']
                    msg += ', blockSize %d'%self._args['blockSize']
                msg += ', keypoints %s'%self._args['keypoints']
                if self._args['keypoints']:
                    msg += ', kptMode %s'%self._args['kptMode']
                    msg += ', nbFeatures %d'%self._args['nbFeatures']
                msg += ', stitch %s'%self._args['stitch']
                if self._args['stitch'] and 'stitchStatus' in self._args:
                    msg += ', stitchStatus %s'%self._args['stitchStatus']
                logger.debug(msg)

            # Checks.
            runPost = self._args['depth']
            runPost = runPost or self._args['keypoints']
            runPost = runPost or self._args['stitch']
            if not runPost:
                continue
            if self._post['left'] is None or self._post['right'] is None:
                continue

            # Get frames to postprocess.
            self._postLock.acquire()
            frameL = cv2.cvtColor(self._post['left'], cv2.COLOR_BGR2GRAY)
            frameR = cv2.cvtColor(self._post['right'], cv2.COLOR_BGR2GRAY)
            self._postLock.release()

            # Postprocess.
            frame, msg, fmt = np.ones(frameL.shape, np.uint8), '', 'GRAY'
            try:
                if self._args['depth']:
                    frame, msg, fmt = self._runDepth(frameL, frameR)
                elif self._args['keypoints']:
                    frame, msg, fmt = self._runKeypoints(frameL, frameR)
                elif self._args['stitch']:
                    frame, msg, fmt = self._runStitch(frameL, frameR)
            except:
                if msg == '': # Otherwise, keep more relevant message.
                    msg = 'OpenCV exception!...'
            self.signals.updatePostFrame.emit(frame, msg, fmt)

    def stop(self):
        # Stop thread.
        self._run = False

    def updatePrepFrame(self, frame, side):
        # Postprocess incoming frame.
        self._postLock.acquire()
        self._post[side] = frame # Refresh frame.
        self._postLock.release()

    def _runDepth(self, frameL, frameR):
        # Compute depth map.
        if self._stereo is None:
            self._stereo = cv2.StereoBM_create(numDisparities=self._args['numDisparities'],
                                               blockSize=self._args['blockSize'])
        disparity = self._stereo.compute(frameL, frameR)
        scaledDisparity = disparity - np.min(disparity)
        if np.max(scaledDisparity) > 0:
            scaledDisparity = scaledDisparity * (255/np.max(scaledDisparity))
        frame = scaledDisparity.astype(np.uint8)
        msg = 'depth (range 0-255, mean %03d, std %03d)'%(np.mean(scaledDisparity), np.std(scaledDisparity))

        return frame, msg, 'GRAY'

    def _runKeypoints(self, frameL, frameR):
        # Detect keypoints.
        kptMode, nbFeatures = None, self._args['nbFeatures']
        if self._args['kptMode'] == 'ORB':
            kptMode = cv2.ORB_create(nfeatures=nbFeatures)
        elif self._args['kptMode'] == 'SIFT':
            kptMode = cv2.SIFT_create(nfeatures=nbFeatures)
            self._convertToGrayScale(frameL)
            self._convertToGrayScale(frameR)
        kptL, dscL = kptMode.detectAndCompute(frameL, None)
        kptR, dscR = kptMode.detectAndCompute(frameR, None)
        if len(kptL) == 0 or len(kptR) == 0:
            frame = np.ones(frameL.shape, np.uint8) # Black image.
            msg = 'KO: no keypoint'
            return frame, msg, 'GRAY'

        # Match keypoints.
        norm = None
        if self._args['kptMode'] == 'ORB':
            norm = cv2.NORM_HAMMING # Better for ORB.
        elif self._args['kptMode'] == 'SIFT':
            norm = cv2.NORM_L2 # Better for SIFT.
        bf = cv2.BFMatcher(norm, crossCheck=True)
        matches = bf.match(dscL, dscR)
        matches = sorted(matches, key = lambda x: x.distance)

        # Draw matches.
        frame = cv2.drawMatches(frameL, kptL, frameR, kptR, matches, None)
        minDist = np.min([match.distance for match in matches])
        msg = 'keypoints (min distance %.3f, nb matches %d)'%(minDist, len(matches))

        return frame, msg, 'BGR'

    def _runStitch(self, frameL, frameR):
        # Stitch frames.
        status, frame = self._stitcher.stitch([frameL, frameR])
        self._args['stitchStatus'] = status
        msg = 'stitch'
        if status != cv2.Stitcher_OK:
            frame = np.ones(frameL.shape, np.uint8) # Black image.
            msg += ' KO'
            if status == cv2.Stitcher_ERR_NEED_MORE_IMGS:
                msg += ': not enough keypoints detected'
            elif status == cv2.Stitcher_ERR_HOMOGRAPHY_EST_FAIL:
                msg += ': RANSAC homography estimation failed'
            elif status == cv2.Stitcher_ERR_CAMERA_PARAMS_ADJUST_FAIL:
                msg += ': failing to properly estimate camera features'
        else:
            msg += ' OK'

        return frame, msg, 'BGR'

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
