"""Test helper: a GeoPackage LINESTRING blob between two model nodes (with an offset middle vertex)."""
import struct

from fake_arcpy import gpkg_point_xy


def line_blob_between(con, a: str, b: str, mid=(0.0, 0.0)) -> bytes:
    pa, pb = (gpkg_point_xy(con.execute("SELECT geom FROM wwn_nodes WHERE node_id=?", (n,)).fetchone()[0])
              for n in (a, b))
    m = ((pa[0] + pb[0]) / 2 + mid[0], (pa[1] + pb[1]) / 2 + mid[1])
    pts = [pa, m, pb]
    wkb = struct.pack("<BII", 1, 2, len(pts)) + b"".join(struct.pack("<dd", *p) for p in pts)
    return b"GP" + bytes([0, 0b00000001]) + struct.pack("<i", 7850) + wkb
