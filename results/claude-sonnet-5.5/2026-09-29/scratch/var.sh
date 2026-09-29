#!/bin/bash
# usage: var.sh name args...
n=$1; shift
cd /c/Users/Jordan/PycharmProjects/tortillabench
timeout 550 "/c/Program Files/Blender Foundation/Blender 4.5/blender.exe" -b --factory-startup --python build_tortilla.py -- --bake-only --out-dir scratch/t1 --dump scratch/$n.json --f-end 46 "$@" > logs/tune_$n.log 2>&1
.venv/Scripts/python.exe scratch/plot.py scratch/$n.json scratch/$n.png
