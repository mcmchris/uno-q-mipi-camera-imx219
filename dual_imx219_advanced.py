#!/usr/bin/env python3

import cv2
import time
import threading
import numpy as np
import os
import json
import subprocess

def init_mipi_hardware():
    print("Starting V4L2 hardware routing (mid-router.sh)...")
    try:
        os.system("media-ctl -d /dev/media0 -r > /dev/null 2>&1")
        subprocess.run(['bash', 'router/dual-imx219-mid-router.sh'], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        print("Hardware routed successfully!")
    except subprocess.CalledProcessError:
        print("CRITICAL ERROR: media-ctl configuration failed.")
        exit(1)
    except FileNotFoundError:
        print("ERROR: 'router/dual-imx219-mid-router.sh' not found. Make sure the router configures both IMX219 sensors to different pads.")
        # Not exiting to allow execution if already routed by other means
        pass

def load_camera_profiles(json_file):
    profiles = {'ccm': [], 'awb': [], 'gamma_lut': None}
    try:
        with open(json_file, 'r') as f:
            data = json.load(f)
            for algo in data.get('algorithms', []):
                if 'rpi.ccm' in algo:
                    for ccm_data in algo['rpi.ccm']['ccms']:
                        profiles['ccm'].append({
                            'ct': ccm_data['ct'],
                            'matrix': np.array(ccm_data['ccm'], dtype=np.float32).reshape(3, 3)
                        })
                elif 'rpi.awb' in algo and 'ct_curve' in algo['rpi.awb']:
                    flat_awb = algo['rpi.awb']['ct_curve']
                    for i in range(0, len(flat_awb), 3):
                        profiles['awb'].append({
                            'ct': flat_awb[i],
                            'r_gain': 1.0 / flat_awb[i+1],
                            'b_gain': 1.0 / flat_awb[i+2]
                        })
                elif 'rpi.contrast' in algo and 'gamma_curve' in algo['rpi.contrast']:
                    flat_gamma = algo['rpi.contrast']['gamma_curve']
                    x_points = [flat_gamma[i] / 256.0 for i in range(0, len(flat_gamma), 2)]
                    y_points = [flat_gamma[i+1] / 256.0 for i in range(0, len(flat_gamma), 2)]
                    x_eval = np.arange(256)
                    lut = np.interp(x_eval, x_points, y_points).astype(np.uint8)
                    profiles['gamma_lut'] = lut

        profiles['ccm'] = sorted(profiles['ccm'], key=lambda x: x['ct'])
        profiles['awb'] = sorted(profiles['awb'], key=lambda x: x['ct'])
    except Exception as e:
        print(f"ERROR loading {json_file}: {e}")

    if profiles['gamma_lut'] is None:
        invGamma = 1.0 / 2.2
        profiles['gamma_lut'] = np.array([((i / 255.0) ** invGamma) * 255 for i in np.arange(0, 256)]).astype("uint8")
    return profiles

# Both use the IMX219 profile now
db_profiles = {
    "cam0": load_camera_profiles('color-profiles/imx219_noir_pisp.json'),
    "cam1": load_camera_profiles('color-profiles/imx219_noir_pisp.json')
}

# Adjusted for two IMX219 (You must verify your subdevs with 'media-ctl -p')
camera_state = {
    "cam0": {
        'subdev': '/dev/v4l-subdev13', 
        'r_gain': 2.70, 'g_gain': 1.85, 'b_gain': 2.80,
        'contrast': 2.30, 'brightness': 5,
        'color_temp': 4200,
        'exposure': 1700, 'analogue_gain': 210
    },
    "cam1": {
        'subdev': '/dev/v4l-subdev12',
        'r_gain': 2.70, 'g_gain': 1.85, 'b_gain': 2.80,
        'contrast': 2.30, 'brightness': 5,
        'color_temp': 4200,
        'exposure': 1700, 'analogue_gain': 210
    }
}

class DualCameraLocal:
    def __init__(self):
        self.frames = {"cam0": np.zeros((480, 400, 3), dtype=np.uint8), 
                       "cam1": np.zeros((480, 400, 3), dtype=np.uint8)}
        self.lock = threading.Lock()
        self.running = True

    def get_dynamic_awb_gains(self, cam_id, current_ct, manual_r, manual_g, manual_b):
        awb_profiles = db_profiles[cam_id]['awb']
        if not awb_profiles: return manual_r, manual_g, manual_b
        cts = [p['ct'] for p in awb_profiles]
        r_gains = [p['r_gain'] for p in awb_profiles]
        b_gains = [p['b_gain'] for p in awb_profiles]
        base_r = np.interp(current_ct, cts, r_gains)
        base_b = np.interp(current_ct, cts, b_gains)
        return base_r * manual_r, manual_g, base_b * manual_b

    def get_dynamic_ccm_matrix(self, cam_id, current_ct):
        profiles = db_profiles[cam_id]['ccm']
        if not profiles: return np.eye(3, dtype=np.float32)
        cts = [p['ct'] for p in profiles]
        matrices = [p['matrix'].flatten() for p in profiles]
        interpolated_flat = []
        for i in range(9):
            channel_values = [m[i] for m in matrices]
            interp_val = np.interp(current_ct, cts, channel_values)
            interpolated_flat.append(interp_val)
        dynamic_ccm = np.array(interpolated_flat, dtype=np.float32).reshape(3, 3)
        bgr_ccm = np.zeros((3, 3), dtype=np.float32)
        bgr_ccm[0,0], bgr_ccm[0,1], bgr_ccm[0,2] = dynamic_ccm[2,2], dynamic_ccm[2,1], dynamic_ccm[2,0]
        bgr_ccm[1,0], bgr_ccm[1,1], bgr_ccm[1,2] = dynamic_ccm[1,2], dynamic_ccm[1,1], dynamic_ccm[1,0]
        bgr_ccm[2,0], bgr_ccm[2,1], bgr_ccm[2,2] = dynamic_ccm[0,2], dynamic_ccm[0,1], dynamic_ccm[0,0]
        return bgr_ccm

    def apply_isp_matrices(self, img, cam_id, current_ct, manual_r, manual_g, manual_b):
        r, g, b = self.get_dynamic_awb_gains(cam_id, current_ct, manual_r, manual_g, manual_b)
        awb_matrix = np.array([[b, 0., 0.], [0., g, 0.], [0., 0., r]], dtype=np.float32)
        ccm_matrix = self.get_dynamic_ccm_matrix(cam_id, current_ct)
        final_matrix = np.dot(ccm_matrix, awb_matrix)
        img_corrected = cv2.transform(img, final_matrix)
        return np.clip(img_corrected, 0, 255).astype(np.uint8)

    def apply_v4l2_hardware_settings(self, cam_id):
        state = camera_state[cam_id]
        os.system(f"v4l2-ctl -d {state['subdev']} --set-ctrl exposure={int(state['exposure'])} > /dev/null 2>&1")
        os.system(f"v4l2-ctl -d {state['subdev']} --set-ctrl analogue_gain={int(state['analogue_gain'])} > /dev/null 2>&1")

    def start_camera(self, cam_id, device_node, label_name, width, height, is_10bit):
        self.apply_v4l2_hardware_settings(cam_id)
        print(f"[{label_name}] Connecting to {device_node}...")
        cap = cv2.VideoCapture(device_node, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if is_10bit: cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'pRAA'))
        else: cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'RGGB'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, 30)
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)

        if not cap.isOpened():
            print(f"[{label_name}] ERROR: Could not open {device_node}.")
            return
        threading.Thread(target=self._update_loop, args=(cap, cam_id, label_name, width, height, is_10bit), daemon=True).start()

    def _update_loop(self, cap, cam_id, label_name, width, height, is_10bit):
        prev_time = time.time()
        frame_count = 0
        target_w, target_h = 400, 480
        
        while self.running:
            ret, img = cap.read()
            if not ret or img is None:
                time.sleep(0.01)
                continue

            try:
                # 1. RAW Unpacking
                raw_bytes = img.flatten()
                stride = len(raw_bytes) // height
                if is_10bit:
                    valid_bytes = int(width * 1.25)
                    padded_2d = raw_bytes.reshape((height, stride))
                    clean_bytes = padded_2d[:, :valid_bytes].flatten()
                    pixels_8bit = clean_bytes.reshape(-1, 5)[:, :4].flatten()
                    bayer_2d = pixels_8bit.reshape((height, width))
                else:
                    bayer_2d = raw_bytes.reshape((height, stride))[:, :width].flatten().reshape((height, width))

                # 2. Demosaicing (original size, cropped)
                bayer_2d = cv2.subtract(bayer_2d, 16)
                color_img = cv2.cvtColor(bayer_2d, cv2.COLOR_BayerBG2BGR)
                
                # SCALING
                h, w = color_img.shape[:2]
                scale = min(target_w / w, target_h / h)
                new_w, new_h = int(w * scale), int(h * scale)
                small_color = cv2.resize(color_img, (new_w, new_h))

                # 3. Color Magic
                state = camera_state[cam_id]
                ccm_img = self.apply_isp_matrices(small_color, cam_id, state['color_temp'], state['r_gain'], state['g_gain'], state['b_gain'])
                adjusted_img = cv2.convertScaleAbs(ccm_img, alpha=state['contrast'], beta=state['brightness'])
                final_img = cv2.LUT(adjusted_img, db_profiles[cam_id]['gamma_lut'])
                
                # 4. Paste on canvas
                temp_canvas = np.zeros((target_h, target_w, 3), dtype=np.uint8)
                y_off = (target_h - new_h) // 2
                x_off = (target_w - new_w) // 2
                temp_canvas[y_off:y_off+new_h, x_off:x_off+new_w] = final_img

            except Exception as e:
                temp_canvas = np.zeros((target_h, target_w, 3), dtype=np.uint8)
                cv2.putText(temp_canvas, "ERROR", (10, 180), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

            # FPS and Texts
            frame_count += 1
            curr_time = time.time()
            elapsed = curr_time - prev_time
            fps = frame_count / elapsed if elapsed > 0 else 0
            if elapsed > 1.0:
                prev_time = curr_time; frame_count = 0

            cv2.putText(temp_canvas, f'{label_name} RAW | {fps:.1f} FPS', (10, 35), cv2.FONT_HERSHEY_DUPLEX, 0.6, (0, 0, 0), 3)
            cv2.putText(temp_canvas, f'{label_name} RAW | {fps:.1f} FPS', (10, 35), cv2.FONT_HERSHEY_DUPLEX, 0.6, (0, 255, 0), 1)

            with self.lock:
                self.frames[cam_id] = temp_canvas

    def get_frame(self, cam_id):
        with self.lock:
            return self.frames[cam_id].copy()

if __name__ == "__main__":
    init_mipi_hardware()

    streamer = DualCameraLocal()
    # Now both use the IMX219 'mid' resolution (1640x1232)
    streamer.start_camera("cam0", "/dev/video0", "IMX219_A", 1640, 1232, True)
    streamer.start_camera("cam1", "/dev/video4", "IMX219_B", 1640, 1232, True)

    window_name = "MCM_Dual_RAW_ISP"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    print("Showing RAW cameras on screen... Press 'q' to exit.")
    while True:
        f0 = streamer.get_frame("cam0")
        f1 = streamer.get_frame("cam1")

        full_screen = np.hstack((f1, f0))
        cv2.imshow(window_name, full_screen)
        
        if cv2.waitKey(30) & 0xFF == ord('q'):
            streamer.running = False
            break

    cv2.destroyAllWindows()