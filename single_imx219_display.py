#!/usr/bin/env python3

import cv2
import time
import numpy as np
import os
import json
import subprocess
import glob
import argparse

# ==========================================
# AUTO-DETECT V4L2 SUBDEVICE
# ==========================================
def get_imx219_subdev():
    for path in glob.glob('/sys/class/video4linux/v4l-subdev*/name'):
        try:
            with open(path, 'r') as f:
                name = f.read().strip()
                if 'imx219' in name.lower():
                    subdev = path.split('/')[-2]
                    return f"/dev/{subdev}"
        except Exception:
            continue
    return None

def get_video_node():
    for path in glob.glob('/sys/bus/i2c/devices/*/name'):
        try:
            with open(path, 'r') as f:
                if 'imx219' in f.read().lower():
                    of_node_path = os.path.join(os.path.dirname(path), 'of_node')
                    real_path = os.path.realpath(of_node_path)
                    if 'i2c-bus@0' in real_path:
                        return '/dev/video0'
                    elif 'i2c-bus@1' in real_path:
                        return '/dev/video4'
        except Exception:
            continue
    return '/dev/video4' # Safe fallback

imx219_subdev = get_imx219_subdev()
if imx219_subdev:
    print(f"✓ Physical IMX219 sensor detected at: {imx219_subdev}")
else:
    print("⚠ WARNING: IMX219 subdevice not found for hardware controls.")

# ==========================================
# CONFIGURATION AND STATE
# ==========================================
color_settings = {
    'r_gain': 1.15,
    'g_gain': 0.80,
    'b_gain': 1.15,
    'contrast': 1.95,
    'brightness': 5,
    'color_temp': 6100, 
    'current_profile': 'imx219_noir_pisp.json'
}

ROUTER_CONFIGS = {
    "mid":  {"script": "router/imx219-mid-router.sh",  "w": 1640, "h": 1232},
    "high": {"script": "router/imx219-high-router.sh", "w": 3280, "h": 2464}
    # (Add "low" later if needed for 640x480)
}

# ==========================================
# HARDWARE STARTUP ROUTINE
# ==========================================
def init_mipi_hardware(router_script):
    print(f"Starting V4L2 hardware routing ({router_script})...")
    try:
        os.system("media-ctl -d /dev/media0 -r > /dev/null 2>&1")
        subprocess.run(['bash', router_script], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        print("✓ Hardware routed successfully!")
    except subprocess.CalledProcessError:
        print(f"CRITICAL ERROR: media-ctl configuration failed when executing {router_script}.")
        exit(1)
    except FileNotFoundError:
        print(f"ERROR: '{router_script}' not found.")
        exit(1)

# ==========================================
# JSON PROFILE LOADER
# ==========================================
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
        print(f"✓ Profile {json_file} loaded successfully.")
    except Exception as e:
        print(f"ERROR loading {json_file}: {e}")

    if profiles['gamma_lut'] is None:
        invGamma = 1.0 / 2.2
        profiles['gamma_lut'] = np.array([((i / 255.0) ** invGamma) * 255 for i in np.arange(0, 256)]).astype("uint8")
    return profiles

imx219_profile = load_camera_profiles('color-profiles/imx219_noir_pisp.json')

# ==========================================
# IMAGE PROCESSING FUNCTIONS (ISP)
# ==========================================
def get_dynamic_awb_gains(current_ct, manual_r, manual_g, manual_b):
    awb_profiles = imx219_profile['awb']
    if not awb_profiles: return manual_r, manual_g, manual_b
    cts = [p['ct'] for p in awb_profiles]
    r_gains = [p['r_gain'] for p in awb_profiles]
    b_gains = [p['b_gain'] for p in awb_profiles]
    base_r = np.interp(current_ct, cts, r_gains)
    base_b = np.interp(current_ct, cts, b_gains)
    return base_r * manual_r, manual_g, base_b * manual_b

def get_dynamic_ccm_matrix(current_ct):
    profiles = imx219_profile['ccm']
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

def apply_isp_matrices(img, current_ct, manual_r, manual_g, manual_b):
    r, g, b = get_dynamic_awb_gains(current_ct, manual_r, manual_g, manual_b)
    awb_matrix = np.array([[b, 0., 0.], [0., g, 0.], [0., 0., r]], dtype=np.float32)
    ccm_matrix = get_dynamic_ccm_matrix(current_ct)
    final_matrix = np.dot(ccm_matrix, awb_matrix)
    img_corrected = cv2.transform(img, final_matrix)
    return np.clip(img_corrected, 0, 255).astype(np.uint8)

# ==========================================
# CAPTURE AND DISPLAY ROUTINE
# ==========================================
def display_camera(selected_config):
    vid_node = get_video_node()
    width = selected_config["w"]
    height = selected_config["h"]
    
    screen_w, screen_h = 800, 480
    window_name = "MCM_Local_Vision"
    
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    print(f"Opening DMA node {vid_node} at {width}x{height}...")
    os.system(f"v4l2-ctl -d {vid_node} --set-fmt-video=width={width},height={height},pixelformat=pRAA > /dev/null 2>&1")
    
    cap = cv2.VideoCapture(vid_node, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'pRAA'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, 30)
    cap.set(cv2.CAP_PROP_CONVERT_RGB, 0) 

    if not cap.isOpened():
        print(f"ERROR: V4L2 could not open {vid_node}")
        return

    prev_time = time.time()
    frame_count = 0

    print("Live stream started. Press 'q' to exit.")
    while True:
        ret, img = cap.read()
        if not ret or img is None:
            time.sleep(0.01)
            continue

        try:
            raw_bytes = img.flatten()
            stride = len(raw_bytes) // height
            valid_bytes = int(width * 1.25)
            padded_2d = raw_bytes.reshape((height, stride))
            clean_bytes = padded_2d[:, :valid_bytes].flatten()
            pixels_8bit = clean_bytes.reshape(-1, 5)[:, :4].flatten()
            bayer_2d = pixels_8bit.reshape((height, width))

            bayer_2d = cv2.subtract(bayer_2d, 16) 
            color_img = cv2.cvtColor(bayer_2d, cv2.COLOR_BayerBG2BGR)
            
            scale = min(screen_w / width, screen_h / height)
            new_w, new_h = int(width * scale), int(height * scale)
            color_img_small = cv2.resize(color_img, (new_w, new_h))

            ccm_img = apply_isp_matrices(
                color_img_small, 
                color_settings['color_temp'], 
                color_settings['r_gain'], 
                color_settings['g_gain'], 
                color_settings['b_gain']
            )
            adjusted_img = cv2.convertScaleAbs(ccm_img, alpha=color_settings['contrast'], beta=color_settings['brightness'])
            final_img = cv2.LUT(adjusted_img, imx219_profile['gamma_lut'])
            
            canvas = np.zeros((screen_h, screen_w, 3), dtype=np.uint8)
            y_off = (screen_h - new_h) // 2
            x_off = (screen_w - new_w) // 2
            canvas[y_off:y_off+new_h, x_off:x_off+new_w] = final_img

        except Exception as e:
            canvas = np.zeros((screen_h, screen_w, 3), dtype=np.uint8)
            cv2.putText(canvas, f"PROCESSING ERROR: {e}", (10, 240), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

        frame_count += 1
        curr_time = time.time()
        elapsed = curr_time - prev_time
        fps = frame_count / elapsed if elapsed > 0 else 0
        if elapsed > 1.0:
            prev_time = curr_time; frame_count = 0

        label = f'IMX219 ({width}x{height}) | {fps:.1f} FPS'
        cv2.putText(canvas, label, (10, 40), cv2.FONT_HERSHEY_DUPLEX, 0.7, (0, 0, 0), 3)
        cv2.putText(canvas, label, (10, 40), cv2.FONT_HERSHEY_DUPLEX, 0.7, (0, 255, 0), 1)

        cv2.imshow(window_name, canvas)

        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()

# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IMX219 MIPI Local Viewer (DSI Mode)")
    parser.add_argument('--router', type=str, choices=['low', 'mid', 'high'], default='mid', 
                        help="Resolution level to use: low, mid or high (default: mid)")
    args = parser.parse_args()

    selected_config = ROUTER_CONFIGS[args.router]
    init_mipi_hardware(selected_config["script"])
    
    # Reset exposure/gain to safe values if sensor access is available
    if imx219_subdev:
        os.system(f"v4l2-ctl -d {imx219_subdev} --set-ctrl exposure=1500 > /dev/null 2>&1")
        os.system(f"v4l2-ctl -d {imx219_subdev} --set-ctrl analogue_gain=200 > /dev/null 2>&1")

    display_camera(selected_config)