#!/usr/bin/env python3

import cv2
import time
import subprocess
import re
import threading
import sys
import os

# Camera autodetection (libcamera)
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

# Asynchronous Camera Reader (Zero Latency)
class AsyncCamera:
    def __init__(self, pipeline):
        self.camera = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        if not self.camera.isOpened():
            raise Exception("Could not initialize camera with GStreamer.")
        
        self.ret, self.frame = self.camera.read()
        self.running = True
        
        self.thread = threading.Thread(target=self.update, args=())
        self.thread.daemon = True
        self.thread.start()

    def update(self):
        while self.running:
            if self.camera.isOpened():
                self.ret, self.frame = self.camera.read()

    def get_frame(self):
        return self.ret, self.frame

    def stop(self):
        self.running = False
        self.thread.join()
        self.camera.release()

def get_gstreamer_pipeline(camera_name):
    width, height, framerate = 800, 480, 30
    
    cam_prop = f'camera-name="{camera_name}" ! ' if camera_name else ""
    return (
        f'libcamerasrc {cam_prop}'
        f'video/x-raw, width={width}, height={height}, framerate={framerate}/1 ! '
        'videoconvert ! '
        'video/x-raw, format=BGR ! '
        'videobalance saturation=1.2 contrast=1.1 ! '
        'appsink drop=true max-buffers=1'
    )

def main():
    if "DISPLAY" not in os.environ:
        print("ℹ️ DISPLAY variable not found. Assigning :0 by default...")
        os.environ["DISPLAY"] = ":0"

    camera_name = detect_camera_name()
    if not camera_name:
        sys.exit(1)

    pipeline = get_gstreamer_pipeline(camera_name)
    print("\nStarting camera with pipeline:")
    print(pipeline)
    
    try:
        async_cam = AsyncCamera(pipeline)
        print("\n✅ Camera initialized. Showing on DSI display...")
    except Exception as e:
        print(f"❌ Critical error: {e}")
        sys.exit(1)
    
    window_name = "MCM_Live_View"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    prev_time = time.time()
    frame_count = 0

    try:
        while True:
            ret, frame = async_cam.get_frame()
            if not ret or frame is None:
                time.sleep(0.01)
                continue
                
            frame_count += 1
            curr_time = time.time()
            elapsed_time = curr_time - prev_time
            fps = frame_count / elapsed_time if elapsed_time > 0 else 0
            
            if elapsed_time > 1.0:
                prev_time = curr_time
                frame_count = 0

            cv2.putText(frame, f'Hardware ISP | {fps:.1f} FPS', (10, 35), cv2.FONT_HERSHEY_DUPLEX, 0.7, (0, 0, 0), 3)
            cv2.putText(frame, f'Hardware ISP | {fps:.1f} FPS', (10, 35), cv2.FONT_HERSHEY_DUPLEX, 0.7, (0, 255, 0), 1)

            cv2.imshow(window_name, frame)

            if cv2.waitKey(1) & 0xFF == ord('q'):
                print("Closing application...")
                break

    except KeyboardInterrupt:
        print("\nKeyboard interrupt detected. Exiting...")
    finally:
        async_cam.stop()
        cv2.destroyAllWindows()

if __name__ == "__main__":
    main()