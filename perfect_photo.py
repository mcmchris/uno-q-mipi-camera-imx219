#!/usr/bin/env python

import cv2
import time
import numpy as np
import os
import json
import subprocess
import glob

# ==========================================
# 1. YOUR MASTER SETTINGS (Based on the PiSP Tuner)
# ==========================================
SETTINGS = {
    'exposure': 1700,        #
    'analogue_gain': 220,    #
    'color_temp': 6150,      #
    'r_gain': 1.20,          #
    'g_gain': 0.90,          #
    'b_gain': 1.15,          #
    'contrast': 1.95,        #
    'brightness': 5          #
}

FILENAME = "color_corrected.jpg"

# ==========================================
# 2. AUTO-DETECT AND V4L2 HARDWARE
# ==========================================
def get_imx219_subdev():
    for path in glob.glob('/sys/class/video4linux/v4l-subdev*/name'):
        try:
            with open(path, 'r') as f:
                if 'imx219' in f.read().lower():
                    return f"/dev/{path.split('/')[-2]}"
        except Exception:
            pass
    return None

def get_video_node():
    for path in glob.glob('/sys/bus/i2c/devices/*/name'):
        try:
            with open(path, 'r') as f:
                if 'imx219' in f.read().lower():
                    # Obtenemos la ruta física del árbol de dispositivos
                    of_node_path = os.path.join(os.path.dirname(path), 'of_node')
                    real_path = os.path.realpath(of_node_path)
                    
                    if 'i2c-bus@0' in real_path:
                        return '/dev/video0'
                    elif 'i2c-bus@1' in real_path:
                        return '/dev/video4'
        except Exception:
            continue
    return '/dev/video4' # Safe fallback

def apply_hardware_settings():
    print("Routing V4L2 hardware...")
    os.system("bash router/imx219-high-router.sh > /dev/null 2>&1")
    
    subdev = get_imx219_subdev()
    if subdev:
        print(f"Applying exposure ({SETTINGS['exposure']}) and gain ({SETTINGS['analogue_gain']}) on {subdev}...")
        os.system(f"v4l2-ctl -d {subdev} --set-ctrl exposure={SETTINGS['exposure']} > /dev/null 2>&1")
        os.system(f"v4l2-ctl -d {subdev} --set-ctrl analogue_gain={SETTINGS['analogue_gain']} > /dev/null 2>&1")
    else:
        print("Warning: Subdevice not found. Hardware settings were not applied.")

# ==========================================
# 3. MOTOR DE COLOR (PiSP JSON)
# ==========================================
def load_camera_profiles(json_file):
    profiles = {'ccm': [], 'awb': [], 'gamma_lut': None}
    with open(json_file, 'r') as f:
        data = json.load(f)
        for algo in data.get('algorithms', []):
            if 'rpi.ccm' in algo:
                for ccm_data in algo['rpi.ccm']['ccms']:
                    profiles['ccm'].append({'ct': ccm_data['ct'], 'matrix': np.array(ccm_data['ccm'], dtype=np.float32).reshape(3, 3)})
            elif 'rpi.awb' in algo and 'ct_curve' in algo['rpi.awb']:
                flat = algo['rpi.awb']['ct_curve']
                for i in range(0, len(flat), 3):
                    profiles['awb'].append({'ct': flat[i], 'r_gain': 1.0/flat[i+1], 'b_gain': 1.0/flat[i+2]})
            elif 'rpi.contrast' in algo and 'gamma_curve' in algo['rpi.contrast']:
                flat = algo['rpi.contrast']['gamma_curve']
                x_pts, y_pts = [flat[i]/256.0 for i in range(0, len(flat), 2)], [flat[i+1]/256.0 for i in range(0, len(flat), 2)]
                profiles['gamma_lut'] = np.interp(np.arange(256), x_pts, y_pts).astype(np.uint8)
    
    profiles['ccm'] = sorted(profiles['ccm'], key=lambda x: x['ct'])
    profiles['awb'] = sorted(profiles['awb'], key=lambda x: x['ct'])
    return profiles

def apply_color_science(img, profile):
    ct = SETTINGS['color_temp']
    
    # 1. AWB: Interpolated (if available) + web multipliers
    if profile['awb']:
        base_r = np.interp(ct, [p['ct'] for p in profile['awb']], [p['r_gain'] for p in profile['awb']])
        base_b = np.interp(ct, [p['ct'] for p in profile['awb']], [p['b_gain'] for p in profile['awb']])
    else:
        # NOIR mode: if no AWB data, use neutral base
        base_r, base_b = 1.0, 1.0
        
    r = base_r * SETTINGS['r_gain']
    g = 1.0 * SETTINGS['g_gain']
    b = base_b * SETTINGS['b_gain']
    awb_matrix = np.array([[b, 0., 0.], [0., g, 0.], [0., 0., r]], dtype=np.float32)
    
    # 2. CCM matrix: interpolated (if available)
    if profile['ccm']:
        mats = [p['matrix'].flatten() for p in profile['ccm']]
        dynamic_ccm = np.array([np.interp(ct, [p['ct'] for p in profile['ccm']], [m[i] for m in mats]) for i in range(9)], dtype=np.float32).reshape(3, 3)
        
        bgr_ccm = np.zeros((3, 3), dtype=np.float32)
        bgr_ccm[0,0], bgr_ccm[0,1], bgr_ccm[0,2] = dynamic_ccm[2,2], dynamic_ccm[2,1], dynamic_ccm[2,0]
        bgr_ccm[1,0], bgr_ccm[1,1], bgr_ccm[1,2] = dynamic_ccm[1,2], dynamic_ccm[1,1], dynamic_ccm[1,0]
        bgr_ccm[2,0], bgr_ccm[2,1], bgr_ccm[2,2] = dynamic_ccm[0,2], dynamic_ccm[0,1], dynamic_ccm[0,0]
    else:
        # Fallback if the color matrix is missing
        bgr_ccm = np.eye(3, dtype=np.float32)
        
    final_matrix = np.dot(bgr_ccm, awb_matrix)
    img_corrected = cv2.transform(img, final_matrix)
    return np.clip(img_corrected, 0, 255).astype(np.uint8)

# ==========================================
# 4. CAPTURE AND PROCESSING
# ==========================================
def take_photo():
    print("Routing V4L2 hardware...")
    os.system("bash router/imx219-high-router.sh > /dev/null 2>&1")
    
    profile = load_camera_profiles('color-profiles/imx219_noir_pisp.json')
    
    vid_node = get_video_node()
    print(f"Opening DMA node {vid_node}...")
    width, height = 3280, 2464
    
    # 1. Open the camera FIRST using the dynamic node
    cap = cv2.VideoCapture(vid_node, cv2.CAP_V4L2)
    # ----------------------------------
  
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1) 
    cap.set(cv2.CAP_PROP_FPS, 15)       

    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'pRAA'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)

    # 2. Apply EXPOSURE and GAIN AFTER opening OpenCV
    subdev = get_imx219_subdev()
    if subdev:
        print(f"Applying exposure ({SETTINGS['exposure']}) and gain ({SETTINGS['analogue_gain']}) on {subdev}...")
        os.system(f"v4l2-ctl -d {subdev} --set-ctrl exposure={SETTINGS['exposure']} > /dev/null 2>&1")
        os.system(f"v4l2-ctl -d {subdev} --set-ctrl analogue_gain={SETTINGS['analogue_gain']} > /dev/null 2>&1")
    else:
        print("Warning: Subdevice not found.")

    print("Flushing buffers and aligning Bayer pattern (15 frames)...")
    # 3. Flush 15 frames (~1 second) to avoid Bayer phase shift (magenta)
    for _ in range(15): 
        cap.read()
        
    print("Capturing clean RAW 10-bit frame...")
    ret, img = cap.read()
    cap.release()

    if not ret:
        print("Error: Could not read the buffer.")
        return

    print("Processing color at 3280x2464...")
    
    # Unpack RAW 10-bit to 8-bit
    raw_bytes = img.flatten()
    stride = len(raw_bytes) // height
    valid_bytes = int(width * 1.25)
    clean_bytes = raw_bytes.reshape((height, stride))[:, :valid_bytes].flatten()
    pixels_8bit = clean_bytes.reshape(-1, 5)[:, :4].flatten()
    bayer_2d = pixels_8bit.reshape((height, width))
    
    # Demosaic at full resolution
    bayer_2d = cv2.subtract(bayer_2d, 16)
    color_img = cv2.cvtColor(bayer_2d, cv2.COLOR_BayerBG2BGR)

    # Apply color algorithms
    ccm_img = apply_color_science(color_img, profile)
    adjusted_img = cv2.convertScaleAbs(ccm_img, alpha=SETTINGS['contrast'], beta=SETTINGS['brightness'])
    final_img = cv2.LUT(adjusted_img, profile['gamma_lut'])

    print(f"Saving final image: {FILENAME}...")
    cv2.imwrite(FILENAME, final_img, [cv2.IMWRITE_JPEG_QUALITY, 100])
    print("Success! Photo captured with maximum sharpness and correct color.")

if __name__ == "__main__":
    take_photo()