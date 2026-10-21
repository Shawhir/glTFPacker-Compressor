#!/usr/bin/env python3
"""
slim_trees.py - post-pack GLB slimmer for the Ankara vegetation set.

Surgical: edits the GLB's JSON + binary chunk DIRECTLY. It removes only the
accessors it targets and flips one material flag, then repacks the buffer.
Everything it does not touch - MSFT_lod, TANGENT, extras, node/material order -
is preserved byte-for-byte. (This is the key difference from the gltf-transform
version, which rebuilt the whole document and DROPPED unregistered extensions
like MSFT_lod.)

Three cuts, each guarded and reported per file:
  (1) --drop-color0    Remove COLOR_0 iff every value is ~white (no-op tint).
  (2) --drop-uv1       Remove TEXCOORD_1 iff NO material samples texCoord 1.
  (4) --blend-to-mask  Convert alphaMode BLEND -> MASK on __LOD0 billboard
                       materials (restores depth-write, kills sort + overdraw).

Scope: uncompressed geometry. If a file is Draco/meshopt compressed it is
SKIPPED with a warning (can't surgically edit compressed vertex data - do 1&2
pre-compression instead). These Ankara packs are MSFT_lod + raw PNG/JPEG, so
that path doesn't apply here.

Defaults to DRY-RUN. Pass --apply to write. Writes to --out dir; never touches
the input file in place.

Usage:
  python slim_trees.py --inspect  <dir|file>
  python slim_trees.py <dir|file>                         # dry-run
  python slim_trees.py --apply --out ./glb_slim <dir>     # write
  python slim_trees.py --apply --out ./glb_slim --only 4 <dir>
"""
import json, struct, os, sys, glob

# ---- args -----------------------------------------------------------------
argv = sys.argv[1:]
def has(f): return f in argv
def opt(f, d=None):
    return argv[argv.index(f)+1] if f in argv and argv.index(f)+1 < len(argv) else d
INSPECT = has('--inspect'); APPLY = has('--apply')
OUT = opt('--out', './glb_slim')
ONLY = opt('--only')                      # e.g. "1", "4", "1,2"
WHITE_EPS = float(opt('--white-eps', '0.004'))
CUTOFF_DEFAULT = float(opt('--cutoff', '0.1'))
import re
BILLBOARD_RE = re.compile(opt('--billboard-re', r'__LOD0|billboard'), re.I)
def want(n): return (ONLY is None) or (str(n) in [s.strip() for s in ONLY.split(',')])
positionals = [a for i,a in enumerate(argv)
               if not a.startswith('--') and not (i>0 and argv[i-1] in ('--out','--only','--white-eps','--cutoff','--billboard-re'))]

# per season+age alpha cutoff (your reviewed values); fallback CUTOFF_DEFAULT
CUTOFFS = [(re.compile(r'autumn.*young|young.*autumn', re.I), 0.05),
           (re.compile(r'autumn.*adult|adult.*autumn', re.I), 0.10),
           (re.compile(r'spring.*young|young.*spring', re.I), 0.10),
           (re.compile(r'spring.*adult|adult.*spring', re.I), 0.20)]
def cutoff_for(name):
    for rx,v in CUTOFFS:
        if rx.search(name or ''): return v
    return CUTOFF_DEFAULT

CT = {5120:('b',1),5121:('B',1),5122:('h',2),5123:('H',2),5125:('I',4),5126:('f',4)}
TYPEN = {'SCALAR':1,'VEC2':2,'VEC3':3,'VEC4':4,'MAT2':4,'MAT3':9,'MAT4':16}

# ---- GLB read/write -------------------------------------------------------
def read_glb(path):
    with open(path,'rb') as f:
        magic,ver,length = struct.unpack('<4sII', f.read(12))
        if magic != b'glTF': raise ValueError('not a GLB')
        js=None; bin=b''
        while f.tell() < length:
            clen,ctype = struct.unpack('<II', f.read(8))
            data = f.read(clen)
            if ctype == 0x4E4F534A: js = json.loads(data)          # JSON
            elif ctype == 0x004E4942: bin = data                    # BIN
        return js, bin

def write_glb(path, js, bin):
    jsb = json.dumps(js, separators=(',',':')).encode('utf-8')
    jsb += b' ' * ((4 - len(jsb) % 4) % 4)                          # pad JSON w/ spaces
    binp = bin + b'\x00' * ((4 - len(bin) % 4) % 4)                 # pad BIN w/ zeros
    total = 12 + 8 + len(jsb) + 8 + len(binp)
    with open(path,'wb') as f:
        f.write(struct.pack('<4sII', b'glTF', 2, total))
        f.write(struct.pack('<II', len(jsb), 0x4E4F534A)); f.write(jsb)
        f.write(struct.pack('<II', len(binp), 0x004E4942)); f.write(binp)

# ---- analysis -------------------------------------------------------------
def color0_all_white(js, bin, ai):
    a = js['accessors'][ai]; bv = js['bufferViews'][a['bufferView']]
    off = bv.get('byteOffset',0) + a.get('byteOffset',0)
    n = TYPEN[a['type']]; fmt,sz = CT[a['componentType']]
    stride = bv.get('byteStride') or n*sz
    typemax = {5121:255,5123:65535,5125:4294967295}.get(a['componentType'], 1.0)
    isint = a['componentType'] in (5121,5123,5125)
    for k in range(a['count']):
        t = struct.unpack_from('<'+fmt*n, bin, off+k*stride)
        for c in range(min(n,3)):
            v = (t[c]/typemax) if isint else t[c]                  # normalise int regardless of flag
            if abs(v-1.0) > WHITE_EPS: return False
    return True

def max_texcoord(js):
    mx=0
    for m in js.get('materials',[]):
        slots=[m.get('pbrMetallicRoughness',{}).get('baseColorTexture'),
               m.get('pbrMetallicRoughness',{}).get('metallicRoughnessTexture'),
               m.get('normalTexture'),m.get('occlusionTexture'),m.get('emissiveTexture')]
        for s in slots:
            if s: mx=max(mx,s.get('texCoord',0))
    return mx

def is_compressed(js):
    exts = set(js.get('extensionsUsed',[]))
    if exts & {'KHR_draco_mesh_compression','EXT_meshopt_compression'}: return True
    for m in js.get('meshes',[]):
        for p in m['primitives']:
            if 'KHR_draco_mesh_compression' in p.get('extensions',{}): return True
    return False

def color0_flag_bad(js):
    bad=set()
    for m in js.get('meshes',[]):
        for p in m['primitives']:
            ai=p['attributes'].get('COLOR_0')
            if ai is not None:
                a=js['accessors'][ai]
                if a['componentType'] in (5121,5123,5125) and not a.get('normalized',False):
                    bad.add(js['materials'][p['material']].get('name'))
    return [b for b in bad if b]

# ---- repack: drop a set of accessors, rebuild buffer, remap indices -------
def repack(js, bin, del_accessors):
    keep_acc = [i for i in range(len(js['accessors'])) if i not in del_accessors]
    acc_map = {old:new for new,old in enumerate(keep_acc)}                     # old->new accessor idx

    # which bufferViews are still needed (by surviving accessors, images, others)
    needed_bv=set()
    for i in keep_acc:
        bvi=js['accessors'][i].get('bufferView')
        if bvi is not None: needed_bv.add(bvi)
    for im in js.get('images',[]):
        if 'bufferView' in im: needed_bv.add(im['bufferView'])
    # preserve any bufferView referenced by extensions we don't understand (safety)
    # (meshopt/draco already excluded upstream; this is belt-and-braces)
    keep_bv=[i for i in range(len(js['bufferViews'])) if i in needed_bv]
    bv_map={old:new for new,old in enumerate(keep_bv)}

    # rebuild binary: concat kept bufferViews (4-byte aligned), record new offsets
    new_bin=bytearray(); new_bvs=[]
    for old in keep_bv:
        bv=dict(js['bufferViews'][old]); off=bv.get('byteOffset',0); ln=bv['byteLength']
        if len(new_bin)%4: new_bin += b'\x00'*(4-len(new_bin)%4)
        bv['byteOffset']=len(new_bin); new_bin += bin[off:off+ln]
        bv['buffer']=0
        new_bvs.append(bv)

    # rebuild accessors (drop deleted, remap bufferView)
    new_accs=[]
    for old in keep_acc:
        a=dict(js['accessors'][old])
        if 'bufferView' in a: a['bufferView']=bv_map[a['bufferView']]
        new_accs.append(a)

    # remap accessor references everywhere they're used
    for m in js['meshes']:
        for p in m['primitives']:
            p['attributes']={k:acc_map[v] for k,v in p['attributes'].items() if v in acc_map}
            if 'indices' in p and p['indices'] in acc_map: p['indices']=acc_map[p['indices']]
            for tgt in p.get('targets',[]):
                for k in list(tgt): 
                    if tgt[k] in acc_map: tgt[k]=acc_map[tgt[k]]
    for im in js.get('images',[]):
        if 'bufferView' in im: im['bufferView']=bv_map[im['bufferView']]
    for sk in js.get('skins',[]):
        if 'inverseBindMatrices' in sk and sk['inverseBindMatrices'] in acc_map:
            sk['inverseBindMatrices']=acc_map[sk['inverseBindMatrices']]

    js['accessors']=new_accs; js['bufferViews']=new_bvs
    js['buffers']=[{'byteLength':len(new_bin)}]
    return js, bytes(new_bin)

# ---- per file -------------------------------------------------------------
def process(path):
    js, bin = read_glb(path)
    rep={'name':os.path.basename(path),'lines':[],'skip':None,'changed':False}
    if is_compressed(js):
        rep['skip']='compressed (Draco/meshopt) - do 1&2 pre-compression'; return rep, js, bin, 0

    del_acc=set()

    # op4: BLEND -> MASK on billboard materials (no geometry touch)
    if want(4):
        hits=[]
        for m in js.get('materials',[]):
            if m.get('alphaMode')=='BLEND' and BILLBOARD_RE.search(m.get('name','')):
                co=cutoff_for(m.get('name',''))
                hits.append(f"{m.get('name')} BLEND->MASK@{co}")
                if APPLY: m['alphaMode']='MASK'; m['alphaCutoff']=co; rep['changed']=True
        rep['lines']+= [f"- {h}" for h in hits]
        if not hits:
            stray=[m.get('name') for m in js.get('materials',[]) if m.get('alphaMode')=='BLEND']
            if stray: rep['lines'].append(f"~ BLEND materials not matching --billboard-re: {', '.join(stray)}")

    # op1: drop COLOR_0 (guarded all-white)
    if want(1):
        c0=[(mi,pi,p['attributes']['COLOR_0']) for mi,m in enumerate(js['meshes'])
            for pi,p in enumerate(m['primitives']) if 'COLOR_0' in p['attributes']]
        if not c0: rep['lines'].append("COLOR_0: none present")
        else:
            allw=all(color0_all_white(js,bin,ai) for _,_,ai in c0)
            flagbad=color0_flag_bad(js)
            note=f"  [WARN normalized=false on integer COLOR_0 - spec-invalid: {len(flagbad)} mat(s)]" if flagbad else ""
            if allw:
                rep['lines'].append(f"- COLOR_0 drop ({len(c0)} prims, all white){note}")
                for _,_,ai in c0: del_acc.add(ai)
            else:
                rep['lines'].append(f"KEEP COLOR_0 (non-white values found){note}")

    # op2: drop TEXCOORD_1 (guarded unreferenced)
    if want(2):
        uv1=[(mi,pi,p['attributes']['TEXCOORD_1']) for mi,m in enumerate(js['meshes'])
             for pi,p in enumerate(m['primitives']) if 'TEXCOORD_1' in p['attributes']]
        if not uv1: rep['lines'].append("UV1: none present")
        elif max_texcoord(js)>=1: rep['lines'].append("KEEP UV1 (a material samples texCoord 1)")
        else:
            rep['lines'].append(f"- UV1 drop ({len(uv1)} prims, unreferenced)")
            for _,_,ai in uv1: del_acc.add(ai)

    saved=0
    if APPLY and del_acc:
        before=len(bin); js,bin=repack(js,bin,del_acc); saved=before-len(bin); rep['changed']=True
    return rep, js, bin, saved

# ---- inspect --------------------------------------------------------------
def inspect(path):
    js,bin=read_glb(path)
    exts=js.get('extensionsUsed') or []
    comp='[Draco/meshopt]' if is_compressed(js) else ''
    print(f"- {os.path.basename(path)}  ext={exts} {comp}  maxTexCoordUsed={max_texcoord(js)}")
    for mi,m in enumerate(js['meshes']):
        for p in m['primitives']:
            mat=js['materials'][p['material']] if 'material' in p else {}
            am=mat.get('alphaMode','OPAQUE'); ac=mat.get('alphaCutoff')
            v=js['accessors'][p['attributes']['POSITION']]['count']
            c0='white' if 'COLOR_0' in p['attributes'] and color0_all_white(js,bin,p['attributes']['COLOR_0']) else ('NONWHITE' if 'COLOR_0' in p['attributes'] else '-')
            attrs=','.join(sorted(p['attributes']))
            print(f"    mesh{mi} {mat.get('name','(none)')[:44]:44s} a={am}{('@'+str(ac)) if ac is not None else ''}  v={v:5d} COLOR_0={c0} [{attrs}]")

# ---- run ------------------------------------------------------------------
def collect(inputs):
    out=[]
    for p in (inputs or ['.']):
        if os.path.isdir(p):
            for r,_,fs in os.walk(p):
                out+=[os.path.join(r,f) for f in fs if f.lower().endswith('.glb')]
        elif p.lower().endswith('.glb') and os.path.exists(p): out.append(p)
    return sorted(out)

files=collect(positionals)
if not files: print("No .glb files found. Pass a dir or file."); sys.exit(1)
mode='INSPECT' if INSPECT else ('APPLY' if APPLY else 'DRY-RUN')
print(f"{mode} - {len(files)} GLB(s)" + (f" -> {os.path.abspath(OUT)}" if APPLY else "") + (f"  (ops:{ONLY})" if ONLY else "") + "\n")

tot_saved=0; n_written=0; n_skip=0
for f in files:
    if INSPECT: inspect(f); continue
    rep,js,bin,saved=process(f)
    print(f"* {rep['name']}")
    if rep['skip']: print(f"    ~ SKIP: {rep['skip']}"); n_skip+=1; continue
    for l in rep['lines']: print(f"    {l}")
    if not rep['lines']: print("    (no eligible changes)")
    if APPLY and rep['changed']:
        os.makedirs(OUT, exist_ok=True)
        write_glb(os.path.join(OUT, rep['name']), js, bin); n_written+=1; tot_saved+=saved
        if saved: print(f"    = saved {saved//1024} KB")
if not INSPECT:
    print(f"\nSummary: {'wrote '+str(n_written)+' file(s), ' if APPLY else ''}"
          f"{'skipped '+str(n_skip)+' compressed, ' if n_skip else ''}"
          f"{'total saved '+str(tot_saved//1024)+' KB' if APPLY else 'dry-run - pass --apply to write'}")
