#!/bin/bash

echo "Resetting hardware pipeline..."
media-ctl -r

# ==========================================
# 1. AUTO-DETECTION WITH 'cam -l'
# ==========================================
CAM_INFO=$(cam -l | grep -i "imx219" | head -n 1)

if [ -z "$CAM_INFO" ]; then
    echo "❌ ERROR: No IMX219 camera detected (check 'cam -l')."
    exit 1
fi

if [[ "$CAM_INFO" == *"i2c-bus@0"* ]]; then
    PORT=0
    echo "✅ IMX219 detected on PORT 0 (i2c-bus@0)"
elif [[ "$CAM_INFO" == *"i2c-bus@1"* ]]; then
    PORT=1
    echo "✅ IMX219 detected on PORT 1 (i2c-bus@1)"
else
    echo "❌ ERROR: Unknown port in $CAM_INFO"
    exit 1
fi

# ==========================================
# 2. EXACT V4L2 SENSOR NAME
# ==========================================
IMX219_NAME=$(grep -h -i "imx219" /sys/class/video4linux/v4l-subdev*/name | head -n 1)

# ==========================================
# 3. DYNAMIC VARIABLES AND ROUTING
# ==========================================
PHY="\"msm_csiphy${PORT}\""
CSID="\"msm_csid${PORT}\""
VFE="\"msm_vfe${PORT}_rdi0\""

echo "Routing pipeline to RAW RDI (3280x2464)..."
media-ctl -l "${PHY}:1->${CSID}:0[1]"
media-ctl -l "${CSID}:1->${VFE}:0[1]"

F1="fmt:SRGGB10_1X10/3280x2464"

media-ctl -V "\"${IMX219_NAME}\":0 [${F1}]"
media-ctl -V "${PHY}:0 [${F1}]"
media-ctl -V "${PHY}:1 [${F1}]"
media-ctl -V "${CSID}:0 [${F1}]"
media-ctl -V "${CSID}:1 [${F1}]"
media-ctl -V "${VFE}:0 [${F1}]"

echo "Hardware successfully routed on port ${PORT}!"