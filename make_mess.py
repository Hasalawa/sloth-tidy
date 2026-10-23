#!/usr/bin/env python3
"""Create a deliberately messy demo folder: python3 make_mess.py ./messy"""
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else "messy")
root.mkdir(exist_ok=True)
files = {
    "IMG_4471.txt": "Receipt\nCity Grocers\nMilk 2.10\nBread 3.40\nTotal 5.50 USD",
    "final_FINAL_v3.md": "# Q3 planning notes\n- hire designer\n- renegotiate hosting contract",
    "download (7).csv": "date,steps\n2026-10-01,8200\n2026-10-02,9100",
    "untitled.py": "import random\nprint(random.choice(['tea','coffee']))",
    "Screenshot 2026-10-20 at 09.14.png": "",
    "scan0003.txt": "Electricity bill\nAccount 88231\nAmount due: 64.20\nDue 2026-11-02",
    "setup.exe": "",
    "holiday-photos.zip": "",
    "voice memo 12.m4a": "",
}
for name, body in files.items():
    (root / name).write_text(body)
print(f"Made {len(files)} messy files in {root}/")
