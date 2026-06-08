#!/usr/bin/env python3
import cv2
import threading
import time
import numpy as np
import os
import sys
import subprocess
import re

# Set DISPLAY environment variable
if "DISPLAY" not in os.environ:
    os.environ["DISPLAY"] = ":0"

# Clear previous V4L2 blocks for libcamera
print("Clearing hardware topology for libcamera...")
os.system("media-ctl -d /dev/media0 -r > /dev/null 2>&1")

# Exact paths from the 'cam -l' command
CAM0_PATH = "/base/soc@0/cci@5c1b000/i2c-bus@1/sensor@10"
CAM1_PATH = "/base/soc@0/cci@5c1b000/i2c-bus@0/sensor@10"

def get_gstreamer_pipeline(camera_name):
    width, height, framerate = 400, 480, 30
    
    cam_prop = f'camera-name="{camera_name}" ! ' if camera_name else ""
    return (
        f'libcamerasrc {cam_prop}'
        f'video/x-raw, width={width}, height={height}, framerate={framerate}/1 ! '
        'videoconvert ! '
        'video/x-raw, format=BGR ! '
        'videobalance saturation=1.2 contrast=1.1 ! '
        'appsink drop=true max-buffers=1'
    )

def detect_camera_name():
    print("Searching for MIPI cameras with libcamera...")
    try:
        result = subprocess.run(['cam', '-l'], capture_output=True, text=True, check=True)
        output = result.stdout + result.stderr
        match = re.search(r'\((/base/[^\)]+)\)', output)
        if match:
            camera_path = match.group(1)
            print(f"✅ Camera autodetected at: {camera_path}")
            return camera_path
        else:
            print("⚠️ Camera path not found. Are the .dtbo overlays loaded?")
            return None
    except Exception as e:
        print(f"❌ Error executing autodetection: {e}")
        return None
    
class GStreamerCamera:
    def __init__(self, pipeline, cam_name):
        self.cam_name = cam_name
        print(f"[{self.cam_name}] Attempting to open GStreamer pipeline...")
        self.cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        self.ret = False
        self.frame = np.zeros((480, 400, 3), dtype=np.uint8)
        self.running = True
        self.lock = threading.Lock()
        
        if self.cap.isOpened():
            print(f"[{self.cam_name}] Pipeline opened successfully!")
            threading.Thread(target=self.update, daemon=True).start()
        else:
            print(f"[{self.cam_name}] CRITICAL ERROR: GStreamer rejected the pipeline.")
            cv2.putText(self.frame, "CAPTURE ERROR", (50, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 3)

    def update(self):
        while self.running:
            ret, frame = self.cap.read()
            with self.lock:
                self.ret = ret
                if ret:
                    self.frame = frame

    def read(self):
        with self.lock:
            return self.frame.copy()

    def release(self):
        self.running = False
        if self.cap.isOpened():
            self.cap.release()

if __name__ == "__main__":
    print("Starting HW Accelerated Dual Viewer...")
    camera_name = detect_camera_name()

    cam0 = GStreamerCamera(get_gstreamer_pipeline(camera_name), "CAM0 (CSI1)")
    cam1 = GStreamerCamera(get_gstreamer_pipeline(camera_name), "CAM1 (CSI0)")

    window_name = "Dual_MIPI_Fast"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    prev_time = time.time()
    fps = 0
    frame_count = 0

    print("Showing cameras... Press 'q' to exit.")
    while True:
        f0 = cam0.read()
        f1 = cam1.read()

        # Merge images side-by-side
        full_screen = np.hstack((f1, f0))

        frame_count += 1
        curr_time = time.time()
        elapsed = curr_time - prev_time
        if elapsed > 1.0:
            fps = frame_count / elapsed
            prev_time = curr_time
            frame_count = 0

        cv2.putText(full_screen, f'HW Accelerated | {fps:.1f} FPS', (10, 30), cv2.FONT_HERSHEY_DUPLEX, 0.7, (0, 0, 0), 3)
        cv2.putText(full_screen, f'HW Accelerated | {fps:.1f} FPS', (10, 30), cv2.FONT_HERSHEY_DUPLEX, 0.7, (0, 255, 0), 1)
        
        cv2.imshow(window_name, full_screen)

        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cam0.release()
    cam1.release()
    cv2.destroyAllWindows()