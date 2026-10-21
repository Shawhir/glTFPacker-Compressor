param(
    # Settings JSON written by the UI (or edited by hand). Everything else is optional override.
    [string]$Settings = (Join-Path $PSScriptRoot "ankara-pipeline.json"),
    [ValidateSet('full','compress')] [string]$Mode,
    [switch]$DryRun            # print the plan and the exact commands, run nothing
)

# ============================================================================
# Ankara tree pipeline - single entry point, NO interactive prompts.
#   full      : [reorder] pack each species (Pack-Tree) -> copy -> [ground] -> [slim]
#               -> [cutoffs] -> [verify gate] -> [compress]
#   compress  : (re)compress the raw packs only -> glb folder
# Every value comes from the settings JSON (see ankara-pipeline.json). Stage lines
# are prefixed "##STAGE <name> <start|ok|fail|skip>" so the UI can light up the rail.
# Keep next to compress_trees.py, slim_trees.py, ground_trunk.py, check_cutoffs.py,
# reorder_materials_glb.py, pipeline_server.py, pipeline_ui.html.
# ============================================================================

$ErrorActionPreference = 'Continue'
function Stage([string]$name,[string]$state,[string]$msg='') {
    $col = switch ($state) { 'start' {'Cyan'} 'ok' {'Green'} 'fail' {'Red'} default {'Yellow'} }
    Write-Host "##STAGE $name $state $msg" -ForegroundColor $col
}
function Fail([string]$msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# ---- Load settings ---------------------------------------------------------
if (-not (Test-Path $Settings)) { Fail "settings file not found: $Settings" }
$S = Get-Content $Settings -Raw | ConvertFrom-Json
if ($Mode) { $S.mode = $Mode }

# Relative paths (no drive/root) resolve against the folder this script lives in,
# so a self-contained pipeline folder needs no absolute paths for its own scripts.
function Rel([string]$p) { if (-not $p) { return $p }; if ([IO.Path]::IsPathRooted($p)) { return $p }; return (Join-Path $PSScriptRoot $p) }
$src          = Rel $S.paths.src
$glb          = Rel $S.paths.glb
$glbOut       = if ($S.paths.glbCompressed) { Rel $S.paths.glbCompressed } else { $glb }   # compressed set; blank = overwrite glb (old behaviour)
$stage        = Rel $S.paths.stage
$json         = Rel $S.paths.thresholdsJson
$packScript   = Rel $S.paths.packScript
$gltfTool     = Rel $S.paths.gltfTool
$alphaScript  = Rel $S.paths.alphaScript
$basisu       = if ($S.paths.basisu -and ($S.paths.basisu -match '[\\/]')) { Rel $S.paths.basisu } else { if ($S.paths.basisu) { $S.paths.basisu } else { 'basisu' } }
$ktx          = if ($S.paths.ktx -and ($S.paths.ktx -match '[\\/]')) { Rel $S.paths.ktx } else { if ($S.paths.ktx) { $S.paths.ktx } else { 'ktx' } }
$defaultThresh= if ($S.defaultThresholds) { $S.defaultThresholds } else { "128,62,25,2" }

$slimScript    = Join-Path $PSScriptRoot "slim_trees.py"
$compScript    = Join-Path $PSScriptRoot "compress_trees.py"
$groundScript  = Join-Path $PSScriptRoot "ground_trunk.py"
$checkScript   = Join-Path $PSScriptRoot "check_cutoffs.py"
$reorderScript = Join-Path $PSScriptRoot "reorder_materials_glb.py"

$C = $S.compression
$willCompress = ($S.mode -eq 'compress') -or ($S.mode -eq 'full' -and $C.enabled)
$preset   = if ($C.preset) { $C.preset } else { 'esri' }          # esri | colour
$codec    = if ($C.codec)  { $C.codec }  else { 'etc1s' }
$quality  = if ($C.quality -ne $null) { [int]$C.quality } else { if ($codec -eq 'uastc') { 3 } else { 255 } }
if ($codec -eq 'uastc' -and $quality -gt 4) { $quality = 4 }
$maxDim   = if ($C.maxDim) { [int]$C.maxDim } else { 0 }
$minDim   = if ($C.minDim) { [int]$C.minDim } else { 0 }

# ---- Plan summary ----------------------------------------------------------
$cutoffLabel = ($S.cutoffs | ForEach-Object { "$($_.filter)=$($_.cutoff)" }) -join ', '
$compLabel = if (-not $willCompress) { "RAW textures (no compression)" }
             elseif ($preset -eq 'colour') { "Colour-matched (ktx, sRGB mips, solidify) $($codec.ToUpper()) q$quality" }
             else { "Esri parity (basisu, maxed codebooks, basisu mips) $($codec.ToUpper()) q$quality" }
if ($maxDim) { $compLabel += ", max-dim $maxDim" }
Write-Host "`n=== Ankara pipeline ===" -ForegroundColor Cyan
Write-Host "  mode     : $($S.mode)$(if($DryRun){'  (DRY RUN)'})"
Write-Host "  src      : $src"
Write-Host "  glb      : $glb  (raw / uncompressed set)"
Write-Host "  glb out  : $glbOut$(if($glbOut -eq $glb){'  (same folder: compressed OVERWRITES raw)'}else{'  (compressed set)'})"
Write-Host "  stage    : $stage"
Write-Host "  cutoffs  : $cutoffLabel"
Write-Host "  compress : $compLabel"
Write-Host "  stages   : reorder=$($S.stages.reorder) ground=$($S.stages.ground) slim=$($S.stages.slim) cutoffs=$($S.stages.cutoffs) verify=$($S.stages.verify) qaCopy=$($S.stages.qaCopy)"

function Run([string]$label, [scriptblock]$block) {
    if ($DryRun) { Write-Host "  [dry] $label" -ForegroundColor DarkGray; $global:LASTEXITCODE = 0; return }
    & $block
}

# ---- Shared compress stage -------------------------------------------------
function Invoke-CompressStage([string]$source) {
    Stage compress start
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Stage compress fail "python missing"; return $false }
    if (-not (Test-Path $compScript)) { Stage compress fail "compress_trees.py missing next to this script"; return $false }
    if (-not (Select-String -Path $compScript -Pattern "encode_with_basisu" -Quiet)) { Stage compress fail "compress_trees.py is an OLD copy (no basisu encoder)"; return $false }
    if (-not (Test-Path $source)) { Stage compress fail "source missing: $source"; return $false }

    $encoderArgs = @()
    if ($preset -eq 'colour') {
        if (-not (Get-Command $ktx -ErrorAction SilentlyContinue) -and -not (Test-Path $ktx -PathType Leaf)) { Stage compress fail "ktx not found ($ktx) - install KTX-Software or set paths.ktx"; return $false }
        $encoderArgs = @('--encoder','ktx','--ktx',$ktx,'--solidify')
        if ($minDim) { $encoderArgs += @('--min-dim',$minDim) } else { $encoderArgs += @('--min-dim','128') }
        if (-not $DryRun) { python -m pip install --quiet pillow numpy scipy 2>$null }
    } else {
        if (-not (Get-Command $basisu -ErrorAction SilentlyContinue) -and -not (Test-Path $basisu -PathType Leaf)) { Stage compress fail "basisu.exe not found ($basisu)"; return $false }
        $encoderArgs = @('--encoder','basisu','--basisu',$basisu)
        if ($C.solidify) { $encoderArgs += '--solidify' }
        if ($minDim) { $encoderArgs += @('--min-dim',$minDim) }
        if ($maxDim -or $C.solidify) { if (-not $DryRun) { python -m pip install --quiet pillow numpy scipy 2>$null } }
    }
    $codecArgs = if ($codec -eq 'etc1s') { @('--codec','etc1s','--etc1s-q',$quality) } else { @('--codec','uastc','--uastc-quality',$quality) }
    $extra = @()
    if ($C.keepFallback) { $extra += '--keep-fallback' }

    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path $stage | Out-Null
        Remove-Item "$stage\*.glb" -Force -ErrorAction SilentlyContinue     # fresh staging: stale GLBs can't masquerade as success
    }
    Write-Host "Compressing from $source -> $stage" -ForegroundColor Cyan
    $cmd = @('--apply') + $encoderArgs + $codecArgs + @('--max-dim',$maxDim,'--out',$stage) + $extra + @($source)
    Write-Host "  python compress_trees.py $($cmd -join ' ')" -ForegroundColor DarkGray
    Run "compress" { python $compScript @cmd }
    if ($LASTEXITCODE -ne 0) { Stage compress fail "compress_trees.py exit $LASTEXITCODE - nothing copied back"; return $false }
    if ($DryRun) { Stage compress ok "(dry)"; return $true }

    $made = (Get-ChildItem $stage -Filter *.glb -File -ErrorAction SilentlyContinue).Count
    if ($made -lt 1) { Stage compress fail "nothing produced - nothing copied back"; return $false }
    New-Item -ItemType Directory -Force -Path $glbOut | Out-Null
    Copy-Item "$stage\*.glb" $glbOut -Force
    Stage compress ok "$made GLB(s) -> $glbOut (staging kept at $stage)"
    return $true
}

# ---- Cutoff verify gate ----------------------------------------------------
function Invoke-CutoffCheck {
    Stage verify start
    if (-not (Test-Path $checkScript)) { Stage verify fail "check_cutoffs.py missing"; return $false }
    $expect = @()
    foreach ($c in $S.cutoffs) { $expect += @('--expect', "$($c.filter)=$($c.cutoff)") }
    Run "verify" { python $checkScript $glb @expect }
    if ($LASTEXITCODE -ne 0) { Stage verify fail "cutoffs off-spec - stopping BEFORE compression"; return $false }
    Stage verify ok
    return $true
}

# ---- COMPRESS-ONLY ---------------------------------------------------------
if ($S.mode -eq 'compress') {
    $n = (Get-ChildItem $src -Recurse -Filter *.glb -File -ErrorAction SilentlyContinue | Where-Object { $_.Directory.Name -eq 'packed' }).Count
    Write-Host "Compressing $n packed GLB(s) under $src -> $glbOut"
    if (Invoke-CompressStage $src) { Write-Host "`n=== DONE ===" -ForegroundColor Green; exit 0 } else { exit 1 }
}

# ---- FULL PIPELINE ---------------------------------------------------------
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force | Out-Null

$required = @($src,$json,$packScript,$gltfTool,$alphaScript,$slimScript,$checkScript,$glb)
if ($S.stages.ground)  { $required += $groundScript }
if ($S.stages.reorder) { $required += $reorderScript }
if ($willCompress)     { $required += $compScript }
foreach ($p in $required) { if (-not (Test-Path $p)) { Fail "MISSING: $p" } }

$data    = Get-Content $json -Raw | ConvertFrom-Json
$species = Get-ChildItem -Path $src -Directory | Where-Object { $_.Name -notin @('GLBs','TempFiles','Other','packed') }
if ($species.Count -lt 1) { Fail "No species folders under $src" }
Write-Host "  species  : $($species.Count)"

# 1. Pack each species with its thresholds, copy packs to the glb folder
Stage pack start "$($species.Count) species"
# Only pass parameters this copy of Pack-Tree.ps1 actually declares (older copies lack -Tool / -SkipQaCopy)
# NOTE: must be a HASHTABLE for splatting named parameters into a script; an array would pass "-Tool" as a literal value.
$packParams = (Get-Command $packScript).Parameters.Keys
$packArgs = @{}
if ($packParams -contains 'Tool') { $packArgs['Tool'] = $gltfTool }
else { Write-Host "  NOTE: this Pack-Tree.ps1 has no -Tool parameter; it will use its own hardcoded gltf-tool.py path, not $gltfTool" -ForegroundColor Yellow }
if (-not $S.stages.qaCopy -and ($packParams -contains 'SkipQaCopy')) { $packArgs['SkipQaCopy'] = $true }
$packArgsLabel = ($packArgs.GetEnumerator() | ForEach-Object { "-$($_.Key) $($_.Value)" }) -join ' '
if ($packArgsLabel) { Write-Host "  Pack-Tree extra args: $packArgsLabel" -ForegroundColor DarkGray }
$i = 0; $packedTotal = 0; $packFailed = 0
foreach ($sp in $species) {
    $i++
    $key   = $sp.Name -replace '_',''
    $entry = $data.PSObject.Properties | Where-Object { $_.Name -eq $key }
    $t     = if ($entry) { $entry.Value.thresholds -join ',' } else { $defaultThresh }
    Write-Host "`n--- [$i/$($species.Count)] $($sp.Name)  thresholds=$t ---" -ForegroundColor Cyan
    if ($DryRun) { Write-Host "  [dry] Pack-Tree $($sp.Name) -Thresholds $t $packArgsLabel" -ForegroundColor DarkGray; continue }
    Remove-Item "$($sp.FullName)\packed" -Recurse -Force -ErrorAction SilentlyContinue
    & $packScript $sp.FullName -Thresholds $t @packArgs
    $packed = Get-ChildItem "$($sp.FullName)\packed\*.glb" -ErrorAction SilentlyContinue
    if ($packed) { $packed | ForEach-Object { Copy-Item $_.FullName $glb -Force }; $packedTotal += $packed.Count }
    else { Write-Host "  (no packed output for $($sp.Name))" -ForegroundColor Yellow; $packFailed++ }
    if ($i -ge 3 -and $packedTotal -eq 0) { Stage pack fail "first $i species produced nothing - stopping early (fix the error above, e.g. gltf-tool imports)"; exit 1 }
}
if (-not $DryRun) {
    if ($packedTotal -lt 1) { Stage pack fail "no GLBs produced for any species - stopping (see Pack-Tree errors above)"; exit 1 }
    if ($packFailed) { Stage pack ok "$packedTotal GLB(s); $packFailed species produced nothing" } else { Stage pack ok "$packedTotal GLB(s)" }
} else { Stage pack ok "(dry)" }

# 1b. Reorder materials + primitives (Crown, Trunk, Reproductive) on the PACKED glb
if ($S.stages.reorder) {
    Stage reorder start
    Run "reorder" { python $reorderScript $glb }
    if ($LASTEXITCODE -ne 0) { Stage reorder fail "reported an error - continuing, check it" } else { Stage reorder ok }
    if (-not $S.stages.keepBak -and -not $DryRun) {
        $baks = Get-ChildItem $glb -Filter *.bak -File -ErrorAction SilentlyContinue
        if ($baks) { $baks | Remove-Item -Force; Write-Host "  removed $($baks.Count) .bak backup(s) written by the reorder step" -ForegroundColor DarkGray }
    }
} else { Stage reorder skip }

# 2. Ground trunks
if ($S.stages.ground) {
    Stage ground start
    $groundSkip = @()
    if ($S.groundSkip) { $groundSkip = @($S.groundSkip | ForEach-Object { "$_".Trim() } | Where-Object { $_ }) }
    if ($groundSkip.Count) { Write-Host "  ground: leaving untouched -> $($groundSkip -join ', ')" -ForegroundColor DarkGray
                             $skipArg = $groundSkip -join ","
                             Run "ground" { python $groundScript $glb --skip $skipArg } }
    else                   { Run "ground" { python $groundScript $glb } }
    if ($LASTEXITCODE -ne 0) { Stage ground fail "exit $LASTEXITCODE - continuing" } else { Stage ground ok }
} else { Stage ground skip }

# 3. Slim
if ($S.stages.slim) {
    Stage slim start
    Run "slim" { python $slimScript --apply --out $glb $glb }
    if ($LASTEXITCODE -ne 0) { Stage slim fail "exit $LASTEXITCODE - continuing" } else { Stage slim ok }
} else { Stage slim skip }

# 4. Alpha cutoffs per filter (substring match on filename, e.g. Autumn_Adult)
if ($S.stages.cutoffs) {
    Stage cutoffs start $cutoffLabel
    foreach ($c in $S.cutoffs) {
        Write-Host "`n--- alpha $($c.filter) = $($c.cutoff) ---" -ForegroundColor Cyan
        Run "cutoff $($c.filter)" { python $alphaScript $glb --filter $c.filter --cutoff $c.cutoff }
    }
    Stage cutoffs ok
} else { Stage cutoffs skip }

# 4b. VERIFY cutoffs - gate before the expensive compress stage
if ($S.stages.verify) {
    if (-not (Invoke-CutoffCheck)) { exit 1 }
} else { Stage verify skip }

# 5. Compress or stop
if (-not $willCompress) {
    Stage compress skip "textures left raw in $glb"
    Write-Host "`n=== ALL DONE (no compression) ===" -ForegroundColor Green
    exit 0
}
if (Invoke-CompressStage $glb) { Write-Host "`n=== ALL DONE === raw set in $glb, compressed set in $glbOut" -ForegroundColor Green; exit 0 } else { exit 1 }
