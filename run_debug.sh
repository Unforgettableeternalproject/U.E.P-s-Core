#!/bin/bash
cd "$(dirname "$0")"
source env/Scripts/activate && python Entry.py --debug-gui
