# IMX219 Advanced ISP & RAW Capture for Arduino UNO Q

A custom V4L2 RAW capture pipeline and Python Software ISP for the IMX219 MIPI camera. This repository bypasses standard hardware memory limits to capture full 8MP 10-bit frames, applying JSON color profiles (PiSP) for precise colorimetry, Auto White Balance (AWB), and hardware exposure control.

![](assets/thumbnail.png)

## 🛠️ Hardware Requirements
To run this project, you will need the following hardware:
* **Arduino UNO Q**
* **Arduino UNO Media Carrier**
* **IMX219 MIPI Camera Module** (Standard or NOIR)

**Note:** Your Arduino UNO Q must be flashed with one of the latest image versions (> 523).

## 🎨 Color Correction & ISP Bypass

By using lower-level settings and bypassing the default hardware ISP, you can get much better photos.

## 📷 Enable the Media Carrier

You can enable and configure the Media Carrier from the Arduino App Lab settings:

![Media Carrier configuration](assets/enable-camera.png)

Or from the terminal manually setting the overlays:

```bash
cd /boot/efi/dtb/qcom/ # navigate to this directory

sudo fdtoverlay -i qrb2210-arduino-imola-base.dtb -o qrb2210-arduino-imola.dtb qrb2210-arduino-imola-carrier-media.dtbo qrb2210-arduino-imola-carrier-media-camera-imx219-csi1-2lanes.dtbo qrb2210-arduino-imola-video_sound-usbc.dtbo
```

**Note:** Use the `.dtbo` for the right connector where your MIPI camera is attached.

- **CAMERA0:** `qrb2210-arduino-imola-carrier-media-camera-imx219-csi0-2lanes.dtbo`
- **CAMERA1:** `qrb2210-arduino-imola-carrier-media-camera-imx219-csi1-2lanes.dtbo`

## ⚙️ Installation

**1. Clone the repository:**

```bash
git clone https://github.com/mcmchris/uno-q-mipi-camera-imx219.git
cd uno-q-mipi-camera-imx219
```

**2. Install dependencies:**

Make sure you have the required Python libraries installed by running:

```bash
sudo apt update && sudo apt install python3-flask python3-numpy python3-opencv -y
```

(Note: You will also need `v4l2-ctl` and `media-ctl` installed on your Linux system, which are usually included in the v4l-utils package).

**3. Make the router scripts executable:**

Grant execution permissions to the bash scripts inside the `router/` folder so Python can trigger the hardware MIPI routing automatically:

```bash
chmod +x router/*.sh
```

## 🚀 Usage & Scripts

This repository includes two main workflows:

**1. Real-Time Streaming & Tuning Dashboard (single_flask_streaming.py)**

This script launches a Flask web server that streams video from the camera while providing an advanced "PiSP Tuner" web dashboard. You can use it to calibrate the exact color temperature, RGB multipliers, exposure, and analog gain in real time.

**Execution:**

```bash
sudo python3 single_flask_streaming.py
```

**Expected Result:**

The terminal will output the IP address of your board. Open a web browser on any device on the same local network and navigate to `http://<BOARD_IP>:8080`. You will see the live feed and the control panel. Any changes made on the sliders will instantly reflect on the camera's hardware registers and software color matrices.

**2. Color Corrected Still Photo (perfect_photo.py)**

Once you have found your ideal lighting and color settings in the dashboard, you can plug those numbers into the `SETTINGS` dictionary inside this script. This script bypasses OpenCV's video capture limits to grab a single, maximum-quality 8 Megapixel (3280x2464) frame directly from the kernel memory.

**Execution:**

```bash
sudo python3 perfect_photo.py
```

**Expected Result:**

The script will route the V4L2 hardware to maximum resolution, apply your custom exposure/gain, purge unstable initial frames to prevent Bayer phase shifting (magenta tints), capture the RAW data, and process the color science. Finally, it will save a pristine, full-resolution image named `color_corrected.jpg` in your project directory.

**3. DSI Display and Single Camera (single_imx219_display.py and single_imx219_display_fast.py)**

These scripts act as local monitors, capturing the camera feed and rendering it directly to an attached MIPI DSI display, avoiding network latency. We provide two variants:

- `single_imx219_display_fast.py`: Uses GStreamer for hardware-accelerated processing, offering the lowest possible latency and CPU usage (ideal for realtime monitoring).
- `single_imx219_display.py`: Bypasses GStreamer and uses V4L2 raw capture combined with our custom OpenCV color science pipelines, allowing for deep image tuning at the cost of higher CPU overhead.

You must configure the device tree overlays for your DSI display. This can be done via the Arduino App Lab settings:

![Enable DSI Display](assets/enable-display.png)

Alternatively, you can manually inject the overlays via the terminal:

```bash
cd /boot/efi/dtb/qcom/ # navigate to this directory

sudo fdtoverlay -i qrb2210-arduino-imola-base.dtb -o qrb2210-arduino-imola.dtb qrb2210-arduino-imola-carrier-media.dtbo qrb2210-arduino-imola-carrier-media-camera-imx219-csi1-2lanes.dtbo qrb2210-arduino-imola-carrier-media-panel-8in_touch_a-dsi.dtbo
```

This script shows the camera feed on the display.

**Execution:**
(Note: We pass `DISPLAY=:0` to ensure OpenCV can find the local X11 display server).

```bash
DISPLAY=:0 python3 single_imx219_display.py

#or

DISPLAY=:0 python3 single_imx219_display_fast.py
```

**Expected Result:**

The script will initialize the camera, bypass the headless environment, and launch a full-screen window directly on your 8-inch DSI display. You will see a smooth, real-time live feed of the IMX219 sensor along with an active FPS counter rendered in the top corner.


**4. DSI Display and Dual Camera (dual_imx219_display_advanced.py)**

This is the ultimate stereo-vision stress test. This script handles the complex orchestration of initializing two separate IMX219 sensors simultaneously (on CSI0 and CSI1), managing their independent I2C buses, aligning their capture loops, applying individual color correction matrices (CCM) to both feeds, and rendering them side-by-side in real-time.

Both camera ports (CSI0 and CSI1) must be enabled in the device tree alongside the DSI display. Enable them via Arduino App Lab, or manually:

```bash
cd /boot/efi/dtb/qcom/ # navigate to this directory

sudo fdtoverlay -i qrb2210-arduino-imola-base.dtb -o qrb2210-arduino-imola.dtb qrb2210-arduino-imola-carrier-media.dtbo qrb2210-arduino-imola-carrier-media-camera-imx219-csi0-2lanes.dtbo qrb2210-arduino-imola-carrier-media-camera-imx219-csi1-2lanes.dtbo qrb2210-arduino-imola-carrier-media-panel-8in_touch_a-dsi.dtbo
```

This script shows both camera feeds on the display and apply color profiles corrections.

**Execution:**

```bash
DISPLAY=:0 python3 dual_imx219_display_advanced.py
```

**Expected Result:**

The script will trigger the V4L2 routing scripts to prepare the dual hardware paths. Once synchronized, it will open a borderless full-screen application on your DSI display showing a split-screen view. The left half will stream the feed from CAM1 (CSI0), and the right half will stream CAM0 (CSI1). Both streams will have their independent hardware registers optimized and color profiles applied on the fly, complete with dedicated FPS readouts for each sensor.
