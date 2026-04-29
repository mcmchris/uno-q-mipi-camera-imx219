#!/bin/bash

echo "Resetting hardware pipeline..."
media-ctl -r

# ==========================================
# 1. SILENT AUTO-DETECTION (No libcamera)
# ==========================================
# Buscamos silenciosamente en el sistema de archivos de I2C
IMX219_SYSFS=$(grep -l -i "imx219" /sys/bus/i2c/devices/*/name 2>/dev/null | head -n 1)

if [ -z "$IMX219_SYSFS" ]; then
    echo "❌ ERROR: No IMX219 camera detected in sysfs."
    exit 1
fi

# Leemos a qué rama del Device Tree está conectado
OF_NODE=$(readlink -f $(dirname $IMX219_SYSFS)/of_node)

if [[ "$OF_NODE" == *"i2c-bus@0"* ]]; then
    PORT=0
    echo "✅ IMX219 detected on PORT 0 (i2c-bus@0)"
elif [[ "$OF_NODE" == *"i2c-bus@1"* ]]; then
    PORT=1
    echo "✅ IMX219 detected on PORT 1 (i2c-bus@1)"
else
    echo "❌ ERROR: Unknown port in $OF_NODE"
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

echo "Routing pipeline to RAW RDI..."
media-ctl -l "${PHY}:1->${CSID}:0[1]"
media-ctl -l "${CSID}:1->${VFE}:0[1]"

# ---> IMPORTANTE: AQUÍ PONES TU RESOLUCIÓN (1640x1232 para el mid, 3280x2464 para el high)
F1="fmt:SRGGB10_1X10/1640x1232"

media-ctl -V "\"${IMX219_NAME}\":0 [${F1}]"
media-ctl -V "${PHY}:0 [${F1}]"
media-ctl -V "${PHY}:1 [${F1}]"
media-ctl -V "${CSID}:0 [${F1}]"
media-ctl -V "${CSID}:1 [${F1}]"
media-ctl -V "${VFE}:0 [${F1}]"

echo "Hardware successfully routed on port ${PORT}!"