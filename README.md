# glTFPacker-Compressor

Post-pack tooling for the Ankara tree GLBs: slimming, alpha-cutoff checks,
ESRI LOD thresholds and texture compression, driven by one PowerShell entry point.

| File | Purpose |
|---|---|
| `Ankara-Pipeline.ps1` | Single entry point, no prompts. `full` = pack → reorder → ground → slim → cutoffs → verify → compress; `compress` = recompress raw packs only. `-DryRun` prints the plan. |
| `ankara-pipeline.json` | Settings read by the pipeline (paths, stages, per-filter cutoffs, compression preset). Relative paths resolve against the script folder. |
| `slim_trees.py` | Surgical GLB edit: drop all-white `COLOR_0`, drop unreferenced `TEXCOORD_1`, LOD0 billboard `BLEND`→`MASK`. Preserves `MSFT_lod` and extras. Dry-run by default. |
| `check_cutoffs.py` | Verify gate: LOD3/2/1 crown/reproductive materials must be `MASK` at the expected cutoff for their filename filter. Exits 1 on mismatch. |
| `glb_viewer.html` | Browser viewer: drop .glb (or .gltf + .bin + textures). Shows GLB byte layout, buffer views, materials/alpha, and walks the `MSFT_lod` chain with live `ESRI_lod` switching by screen radius. Packs .gltf → .glb. Hosted via GitHub Pages (`.github/workflows/pages.yml`). |
| `add-esri-lod.py` | Writes `extras.ESRI_lod` screen-space thresholds onto the top `MSFT_lod` node so ArcGIS switches LODs. Standalone (not called by the pipeline). |

## Usage

```powershell
.\Ankara-Pipeline.ps1 -DryRun            # show plan + commands
.\Ankara-Pipeline.ps1 -Mode full
.\Ankara-Pipeline.ps1 -Mode compress
```

```sh
python slim_trees.py --inspect <dir>
python slim_trees.py --apply --out ./glb_slim <dir>
python check_cutoffs.py <dir> --expect Autumn_Adult=0.05 --expect Spring_Adult=0.1
python add-esri-lod.py <dir> --thresholds 128,62,25,2 --in-place
```

## Companion files expected next to the pipeline (not in this repo yet)

`compress_trees.py`, `ground_trunk.py`, `reorder_materials_glb.py`,
`set_alpha_cutoff.py`, `Pack-Tree.ps1`, `lod_thresholds_v2.json`,
`build-scripts/gltf-tool.py` (from New-Packer), `pipeline_server.py`, `pipeline_ui.html`.
