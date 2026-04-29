#!/usr/bin/env python

import cv2
import time
import numpy as np
import os
import json
import subprocess
import glob
from flask import Flask, render_template, Response, request, jsonify

app = Flask(__name__, static_folder='templates/assets')

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

imx219_subdev = get_imx219_subdev()
if imx219_subdev:
    print(f"✓ IMX219 physical sensor detected at: {imx219_subdev}")
else:
    print("⚠ WARNING: IMX219 subdevice not found for hardware controls.")

# ==========================================
# CONFIGURATION AND STATE
# ==========================================
color_settings = {
    'r_gain': 1.2,  # Act as extra multipliers on the JSON profile
    'g_gain': 0.9,
    'b_gain': 1.15,
    'contrast': 1.95,
    'brightness': 5,
    'color_temp': 6100 # Base color temperature (daylight)
}

# ==========================================
# JSON PROFILE LOADER (RPi)
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
        print(f"Profile {json_file} loaded successfully.")
    except Exception as e:
        print(f"ERROR loading {json_file}: {e}")

    # Fallback gamma if JSON doesn't include one
    if profiles['gamma_lut'] is None:
        invGamma = 1.0 / 2.2
        profiles['gamma_lut'] = np.array([((i / 255.0) ** invGamma) * 255 for i in np.arange(0, 256)]).astype("uint8")
    return profiles

# Load the color profile (ensure the path matches your folder)
imx219_profile = load_camera_profiles('color-profiles/imx219_noir_pisp.json')

# ==========================================
# IMAGE PROCESSING FUNCTIONS
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
    # Convertir RGB a BGR para OpenCV
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
# V4L2 CAPTURE ROUTINE
# ==========================================
def generate_frames():
    # Run the router script before opening the camera
    print("Running router script...")
    os.system("bash router/imx219-mid-router.sh")

    vid_node = get_video_node()
    print(f"Opening DMA node {vid_node}...")
    
    width, height = 1640, 1232
    target_w, target_h = 1640, 1232 # Streaming resolution

    os.system(f"v4l2-ctl -d {vid_node} --set-fmt-video=width={width},height={height},pixelformat=pRAA > /dev/null 2>&1")
    
    cap = cv2.VideoCapture(vid_node, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'pRAA')) # 10-bit packed
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, 30)
    cap.set(cv2.CAP_PROP_CONVERT_RGB, 0) # Deliver raw image

    if not cap.isOpened():
        raise Exception(f"V4L2 could not open {vid_node}")

    prev_time = time.time()
    frame_count = 0

    while True:
        ret, img = cap.read()
        if not ret or img is None:
            time.sleep(0.01)
            continue

        try:
            # 1. Unpack RAW (the 5-byte trick)
            raw_bytes = img.flatten()
            stride = len(raw_bytes) // height
            valid_bytes = int(width * 1.25)
            padded_2d = raw_bytes.reshape((height, stride))
            clean_bytes = padded_2d[:, :valid_bytes].flatten()
            pixels_8bit = clean_bytes.reshape(-1, 5)[:, :4].flatten()
            bayer_2d = pixels_8bit.reshape((height, width))

            # 2. Fast demosaicing
            bayer_2d = cv2.subtract(bayer_2d, 16) # Black level
            color_img = cv2.cvtColor(bayer_2d, cv2.COLOR_BayerBG2BGR)
            
            # 3. Resize before color processing to save CPU
            color_img_small = cv2.resize(color_img, (target_w, target_h))

            # 4. Raspberry Pi color science + web sliders
            ccm_img = apply_isp_matrices(
                color_img_small, 
                color_settings['color_temp'], 
                color_settings['r_gain'], 
                color_settings['g_gain'], 
                color_settings['b_gain']
            )
            
            adjusted_img = cv2.convertScaleAbs(ccm_img, alpha=color_settings['contrast'], beta=color_settings['brightness'])
            final_img = cv2.LUT(adjusted_img, imx219_profile['gamma_lut'])

        except Exception as e:
            print(f"Error processing frame: {e}")
            continue

        # FPS
        frame_count += 1
        curr_time = time.time()
        elapsed = curr_time - prev_time
        fps = frame_count / elapsed if elapsed > 0 else 0
        if elapsed > 1.0:
            prev_time = curr_time; frame_count = 0

        cv2.putText(final_img, f'RAW V4L2 | {fps:.1f} FPS', (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

        # Encode to JPEG
        ret, buffer = cv2.imencode('.jpg', final_img, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if not ret: continue
            
        frame = buffer.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')

# ==========================================
# FLASK ROUTES
# ==========================================
@app.route('/')
def index():
    return render_template('color-changer.html')

@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/update_settings', methods=['POST'])
def update_settings():
    global color_settings
    data = request.json
    
    # 1. Update software ISP variables
    color_settings['r_gain'] = float(data.get('r_gain', color_settings['r_gain']))
    color_settings['g_gain'] = float(data.get('g_gain', color_settings['g_gain']))
    color_settings['b_gain'] = float(data.get('b_gain', color_settings['b_gain']))
    color_settings['contrast'] = float(data.get('contrast', color_settings['contrast']))
    color_settings['brightness'] = int(data.get('brightness', color_settings['brightness']))
    color_settings['color_temp'] = int(data.get('color_temp', color_settings['color_temp']))
    
    # 2. Send direct commands to V4L2 hardware (now via subdevice!)
    if imx219_subdev:
        if 'exposure' in data:
            exp = int(data['exposure'])
            os.system(f"v4l2-ctl -d {imx219_subdev} --set-ctrl exposure={exp} > /dev/null 2>&1")
        if 'analogue_gain' in data:
            again = int(data['analogue_gain'])
            os.system(f"v4l2-ctl -d {imx219_subdev} --set-ctrl analogue_gain={again} > /dev/null 2>&1")
            
    return jsonify(success=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)