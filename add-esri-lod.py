#!/usr/bin/env python3
"""
add-esri-lod.py  -- inject the ESRI_lod block that ArcGIS/AGOL uses to switch LODs.

A glb from createLODGLB carries the MSFT_lod chain but not the ESRI_lod extras,
so ArcGIS has no switch thresholds and shows only the top (highest) LOD.
Known-good Esri tree glbs put this on the top node:

    "extras": { "ESRI_lod": { "metric": "screenSpaceRadius",
                              "thresholds": [128.1, 62.1, 25.5, 2] } }

One descending threshold per LOD level (len(ids)+1), screen-space radius in px,
floored at ~2. ESRI_proxyEllipsoid is optional and not written here.

Usage:
    python add-esri-lod.py FILE.glb [FILE.glb ...]            # auto ramp
    python add-esri-lod.py FOLDER                              # all .glb inside
    python add-esri-lod.py FILE.glb --like CitrusSinensis.glb  # copy thresholds
    python add-esri-lod.py FILE.glb --thresholds 128,62,25,2   # explicit

Options:
    --like FILE        copy the ESRI_lod thresholds from a reference glb
                       (level count must match)
    --thresholds CSV   explicit descending values, count == LOD level count
    --top 128 --floor 2   endpoints for the auto geometric ramp
    --in-place         overwrite input (default: write *_lod.glb)
    --dry-run          show what would change without writing
"""

import argparse
import json
import os
import struct
import sys

JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942


def read_glb(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"glTF":
        raise ValueError("not a binary glTF (.glb)")
    _m, _v, length = struct.unpack("<III", data[:12])
    off = 12
    gltf, binb = None, b""
    while off < length:
        cl, ct = struct.unpack("<II", data[off:off + 8]); off += 8
        chunk = data[off:off + cl]; off += cl
        if ct == JSON_CHUNK:
            gltf = json.loads(chunk.decode("utf-8"))
        elif ct == BIN_CHUNK:
            binb = chunk
    if gltf is None:
        raise ValueError("no JSON chunk")
    return gltf, binb


def write_glb(path, gltf, binb):
    jb = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    jb += b" " * ((4 - len(jb) % 4) % 4)
    chunks = struct.pack("<II", len(jb), JSON_CHUNK) + jb
    if binb:
        bp = binb + b"\x00" * ((4 - len(binb) % 4) % 4)
        chunks += struct.pack("<II", len(bp), BIN_CHUNK) + bp
    with open(path, "wb") as f:
        f.write(b"glTF" + struct.pack("<II", 2, 12 + len(chunks)) + chunks)


def top_lod_node(gltf):
    """Return (index, node) of the first scene node carrying MSFT_lod."""
    scenes = gltf.get("scenes", [])
    scene = scenes[gltf.get("scene", 0)] if scenes else {"nodes": []}
    snodes = scene.get("nodes", [])
    nodes = gltf.get("nodes", [])
    first = snodes[0] if snodes else None
    if first is not None:
        n = nodes[first]
        if "MSFT_lod" in n.get("extensions", {}):
            return first, n
    # fall back: any node with MSFT_lod
    for i, n in enumerate(nodes):
        if "MSFT_lod" in n.get("extensions", {}):
            return i, n
    return None, None


def thresholds_from(path):
    gltf, _ = read_glb(path)
    _, n = top_lod_node(gltf)
    if n is None:
        raise ValueError(f"{path}: no MSFT_lod node")
    esri = n.get("extras", {}).get("ESRI_lod")
    if not esri:
        raise ValueError(f"{path}: no ESRI_lod to copy")
    return esri["thresholds"]


def auto_ramp(n, top, floor):
    if n == 1:
        return [floor]
    r = (floor / top) ** (1.0 / (n - 1))
    vals = [round(top * (r ** i), 3) for i in range(n)]
    vals[-1] = floor
    return vals


def gather(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            out.extend(sorted(os.path.join(p, x) for x in os.listdir(p)
                              if x.lower().endswith(".glb")))
        else:
            out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser(prog="add-esri-lod")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--like", help="reference glb to copy thresholds from")
    ap.add_argument("--thresholds", help="explicit comma-separated descending values")
    ap.add_argument("--top", type=float, default=128.0)
    ap.add_argument("--floor", type=float, default=2.0)
    ap.add_argument("--symbol", action="store_true",
                    help="also stamp asset.extras ESRI symbolResource marker")
    ap.add_argument("--symbol-name", help="symbol name (default: from filename)")
    ap.add_argument("--webstyle", default="EsriRealisticTreesStyle",
                    help="webstyle name for the symbol marker")
    ap.add_argument("--in-place", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ref = None
    if args.like:
        ref = thresholds_from(args.like)
        print(f"reference thresholds from {os.path.basename(args.like)}: {ref}")
    explicit = [float(x) for x in args.thresholds.split(",")] if args.thresholds else None

    for path in gather(args.paths):
        try:
            gltf, binb = read_glb(path)
            idx, node = top_lod_node(gltf)
            if node is None:
                print(f"{os.path.basename(path)}: no MSFT_lod node, skipped")
                continue
            level_count = len(node["extensions"]["MSFT_lod"].get("ids", [])) + 1

            if explicit is not None:
                thr = explicit
            elif ref is not None:
                thr = ref
            else:
                thr = auto_ramp(level_count, args.top, args.floor)

            if len(thr) != level_count:
                print(f"{os.path.basename(path)}: SKIP -- {level_count} LOD levels but "
                      f"{len(thr)} thresholds")
                continue

            extras = node.setdefault("extras", {})
            old = extras.get("ESRI_lod")
            extras["ESRI_lod"] = {"metric": "screenSpaceRadius", "thresholds": thr}
            note = f" (was {old['thresholds']})" if old else ""
            print(f"{os.path.basename(path)}: {node.get('name')} ESRI_lod thresholds = {thr}{note}")

            if args.symbol:
                name = args.symbol_name or os.path.splitext(os.path.basename(path))[0]
                gltf.setdefault("asset", {})["extras"] = {
                    "ESRI_type": "symbolResource",
                    "ESRI_version": "1.1",
                    "ESRI_webstyleSymbol": {"name": name, "webstyle": args.webstyle},
                }
                print(f"    asset.extras symbolResource: name={name} webstyle={args.webstyle}")

            if args.dry_run:
                continue
            out = path if args.in_place else os.path.splitext(path)[0] + "_lod.glb"
            write_glb(out, gltf, binb)
            if not args.in_place:
                print(f"    -> wrote {os.path.basename(out)}")
        except Exception as e:
            print(f"{os.path.basename(path)}: ERROR {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
