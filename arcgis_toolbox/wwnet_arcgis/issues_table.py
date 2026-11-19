"""Write check issues / design results to a geodatabase-style table via arcpy."""
from __future__ import annotations

import os

ISSUE_FIELDS = [("severity", "TEXT", 12), ("code", "TEXT", 64), ("object_type", "TEXT", 32),
                ("object_id", "TEXT", 64), ("clause", "TEXT", 64), ("message", "TEXT", 1024)]


def write_issues_table(arcpy, out_path: str, issues: list):
    folder, name = os.path.split(out_path)
    arcpy.management.CreateTable(folder, name)
    for fname, ftype, length in ISSUE_FIELDS:
        arcpy.management.AddField(out_path, fname, ftype, field_length=length)
    names = [f[0] for f in ISSUE_FIELDS]
    with arcpy.da.InsertCursor(out_path, names) as cur:
        for i in issues:
            cur.insertRow([str(i.get(n, ""))[:ISSUE_FIELDS[names.index(n)][2]] for n in names])
    return len(issues)
