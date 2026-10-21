#!/usr/bin/env python3
"""
check_cutoffs.py - verify alpha cutoffs on packed Ankara GLBs.

Scope MIRRORS set_alpha_cutoff.py: only LOD3/2/1 crown & reproductive
materials, resolved via the MSFT_lod node walk. LOD0 (billboard) is NOT
checked, because the setter deliberately leaves it untouched.

Expected cutoff is keyed off the FILENAME with the same substring rule the
setter uses for --filter. Pass one --expect per filter:

  python check_cutoffs.py <glb dir> --expect Autumn_Adult=0.10 --expect Autumn_Young=0.05 \
                                    --expect Spring_Adult=0.20 --expect Spring_Young=0.10

If several filters match one file, the LONGEST (most specific) wins, so
"Autumn" and "Autumn_Young" can coexist. Files matching no filter are skipped
and counted. Legacy --autumn / --spring are still accepted as shorthand.

HARD FAIL (exit 1): a material at the wrong mode/value for its file.
WARNING (exit 0): a crown/repro material whose own name carries a different
season token than the file (content issue, not a cutoff issue).
"""
import json, struct, os, sys, re, argparse

DEFAULT_ROOT = r"C:\Users\mar11366\Documents\GitHub\Ankara_Trees_Clean_Up\glb"
TOL = 1e-4
SEASONS = ("autumn", "spring", "summer", "winter")

ap = argparse.ArgumentParser()
ap.add_argument("root", nargs="?", default=DEFAULT_ROOT)
ap.add_argument("--expect", action="append", default=[], metavar="FILTER=CUTOFF")
ap.add_argument("--autumn", type=float)   # legacy shorthand
ap.add_argument("--spring", type=float)
args = ap.parse_args()

EXPECT = {}
for e in args.expect:
    if "=" not in e:
        print(f"bad --expect '{e}', want FILTER=CUTOFF"); sys.exit(2)
    k, v = e.split("=", 1)
    EXPECT[k.strip()] = float(v)
if args.autumn is not None: EXPECT.setdefault("Autumn", args.autumn)
if args.spring is not None: EXPECT.setdefault("Spring", args.spring)
if not EXPECT:
    print("No expectations given (--expect FILTER=CUTOFF)"); sys.exit(2)

season_re = re.compile("(" + "|".join(SEASONS) + ")", re.I)


def read_glb_json(path):
    with open(path, "rb") as f:
        magic, ver, length = struct.unpack("<4sII", f.read(12))
        if magic != b"glTF":
            raise ValueError("not a GLB")
        while f.tell() < length:
            clen, ctype = struct.unpack("<II", f.read(8))
            data = f.read(clen)
            if ctype == 0x4E4F534A:
                return json.loads(data)
    return {}


def season_in(text):
    m = season_re.search(text or "")
    return m.group(1).lower() if m else None


def expected_for(fname):
    """Same rule as set_alpha_cutoff.py --filter: case-insensitive substring. Longest wins."""
    low = fname.lower()
    hits = [(len(k), k) for k in EXPECT if k.lower() in low]
    if not hits:
        return None, None
    k = max(hits)[1]
    return k, EXPECT[k]


def lod321_material_indices(gltf):
    nodes = gltf.get("nodes", [])
    meshes = gltf.get("meshes", [])
    lod_meshes = []
    for node in nodes:
        ext = (node.get("extensions") or {}).get("MSFT_lod")
        if not ext:
            continue
        lod3_mesh = node.get("mesh")
        if lod3_mesh is None:
            continue
        lod_meshes = [lod3_mesh]
        for nid in ext.get("ids", []):
            m = nodes[nid].get("mesh")
            if m is not None:
                lod_meshes.append(m)
        break
    if not lod_meshes:
        return None
    target_meshes = lod_meshes[:-1] if len(lod_meshes) > 1 else lod_meshes  # drop LOD0
    idxs = set()
    for mi in target_meshes:
        if mi < len(meshes):
            for prim in meshes[mi].get("primitives", []):
                mat_idx = prim.get("material")
                if mat_idx is not None:
                    idxs.add(mat_idx)
    return idxs


def glbs(r):
    for dp, _, fs in os.walk(r):
        for fn in fs:
            if fn.lower().endswith(".glb"):
                yield os.path.join(dp, fn)


root = args.root
if not os.path.exists(root):
    print(f"Root not found: {root}")
    sys.exit(2)

problems, warnings = [], []
checked = skipped_no_lod = skipped_no_match = 0
per_filter = {k: 0 for k in EXPECT}

for path in sorted(glbs(root)):
    fname = os.path.basename(path)
    key, want = expected_for(fname)
    if key is None:
        skipped_no_match += 1
        continue
    try:
        js = read_glb_json(path)
    except Exception as e:
        problems.append(f"{fname}: unreadable ({e})")
        continue
    idxs = lod321_material_indices(js)
    if idxs is None:
        skipped_no_lod += 1
        continue
    per_filter[key] += 1
    file_season = season_in(fname)
    materials = js.get("materials", [])
    for mat_idx in sorted(idxs):
        if mat_idx >= len(materials):
            continue
        m = materials[mat_idx]
        name = m.get("name", "")
        n = name.lower()
        if "crown" not in n and "reproductive" not in n:  # same filter as the setter
            continue
        checked += 1
        mode = m.get("alphaMode", "OPAQUE")
        cut = m.get("alphaCutoff")
        if mode != "MASK":
            problems.append(f"{fname} :: {name} -> alphaMode {mode}, expected MASK")
        elif cut is None or abs(cut - want) > TOL:
            problems.append(f"{fname} :: {name} -> cutoff {cut}, expected {want} ({key})")
        mat_season = season_in(name)
        if mat_season and file_season and mat_season != file_season:
            warnings.append(f"{fname} :: {name} is '{mat_season}' content in a '{file_season}' file")

print(f"Checked {checked} LOD3/2/1 crown/repro materials under {root}")
print("  expected: " + ", ".join(f"{k}={v} ({per_filter[k]} files)" for k, v in EXPECT.items()))
notes = ["LOD0 billboards not checked by design"]
if skipped_no_lod:   notes.append(f"{skipped_no_lod} no-MSFT_lod")
if skipped_no_match: notes.append(f"{skipped_no_match} matched no filter")
print("  (" + "; ".join(notes) + ")\n")

if warnings:
    print("CROSS-SEASON WARNINGS (non-blocking - fix in the asset, not the cutoff):")
    for w in warnings:
        print("  WARN " + w)
    print()

if checked == 0:
    print("FAIL - nothing to check: no crown/repro materials found under any matching filter. "
          "Either the glb folder is empty or the filters don't match the filenames.")
    sys.exit(1)
if not problems:
    print("PASS - all in-scope materials are MASK at the cutoff for their filter."
          + (f"  ({len(warnings)} cross-season warning(s) above.)" if warnings else ""))
else:
    for p in problems:
        print("  MISMATCH " + p)
    print(f"\nFAIL - {len(problems)} material(s) off-spec.")
    sys.exit(1)
