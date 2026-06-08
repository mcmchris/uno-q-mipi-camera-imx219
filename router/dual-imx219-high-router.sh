#!/bin/bash

echo "Limpiando hardware..."
media-ctl -r

# ==========================================
# AUTO-DETECCIÓN DUAL IMX219
# ==========================================
CAM0_NAME=$(grep -h -i "imx219" /sys/class/video4linux/v4l-subdev*/name | sed -n '1p')
CAM1_NAME=$(grep -h -i "imx219" /sys/class/video4linux/v4l-subdev*/name | sed -n '2p')

if [ -z "$CAM0_NAME" ] || [ -z "$CAM1_NAME" ]; then
    echo "ERROR: No se detectaron DOS cámaras IMX219."
    echo "Detectadas:"
    echo "1: '$CAM0_NAME'"
    echo "2: '$CAM1_NAME'"
    exit 1
fi

echo "Cámaras IMX219 detectadas:"
echo " - SENSOR A: $CAM0_NAME"
echo " - SENSOR B: $CAM1_NAME"
echo "------------------------------------"

# Resolución HIGH (Nativa completa)
FMT="fmt:SRGGB10_1X10/3280x2464"

# ==========================================
# RUTA 0: CSI0 -> RAW RDI0
# ==========================================
echo "Soldando RUTA 0 (CSI0 -> RDI0) a 3280x2464..."
media-ctl -l '"msm_csiphy0":1->"msm_csid0":0[1]'
media-ctl -l '"msm_csid0":1->"msm_vfe0_rdi0":0[1]'

media-ctl -V "\"${CAM0_NAME}\":0 [${FMT}]"
media-ctl -V '"msm_csiphy0":0 ['$FMT']'
media-ctl -V '"msm_csiphy0":1 ['$FMT']'
media-ctl -V '"msm_csid0":0 ['$FMT']'
media-ctl -V '"msm_csid0":1 ['$FMT']'
media-ctl -V '"msm_vfe0_rdi0":0 ['$FMT']'

# ==========================================
# RUTA 1: CSI1 -> RAW RDI1 (Usando VFE1)
# ==========================================
echo "Soldando RUTA 1 (CSI1 -> RDI0 en VFE1) a 3280x2464..."
media-ctl -l '"msm_csiphy1":1->"msm_csid1":0[1]'
media-ctl -l '"msm_csid1":1->"msm_vfe1_rdi0":0[1]'

media-ctl -V "\"${CAM1_NAME}\":0 [${FMT}]"
media-ctl -V '"msm_csiphy1":0 ['$FMT']'
media-ctl -V '"msm_csiphy1":1 ['$FMT']'
media-ctl -V '"msm_csid1":0 ['$FMT']'
media-ctl -V '"msm_csid1":1 ['$FMT']'
media-ctl -V '"msm_vfe1_rdi0":0 ['$FMT']'

echo "¡Hardware DUAL IMX219 (HIGH) enrutado exitosamente!"