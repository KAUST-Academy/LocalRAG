<#
.SYNOPSIS
  Builds LocalRAG.exe with PyInstaller, tests the frozen app, and zips it for sharing.
.EXAMPLE
  .\build.ps1              # CUDA build: runs on any 64-bit Windows 10/11 PC, uses an NVIDIA GPU when there is one
.EXAMPLE
  .\build.ps1 -Torch cpu   # CPU-only build: a much smaller download that never uses the GPU
#>
param(
    [ValidateSet("cuda", "cpu")] [string] $Torch = "cuda",
    [string] $PythonVersion = "3.14"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Run([string] $What, [scriptblock] $Command) {
    # pip and PyInstaller log to stderr. Windows PowerShell 5.1 turns every stderr line into an error record, and
    # with ErrorActionPreference=Stop the first INFO line would end the build. Print them as text; judge by exit code.
    $ErrorActionPreference = "Continue"
    & $Command 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

$version = (Select-String -Path rag_engine.py -Pattern '^APP_VERSION = "(.+)"').Matches[0].Groups[1].Value
$venv = @{ cuda = ".venv"; cpu = ".venv-cpu" }[$Torch]
$python = Join-Path $venv "Scripts\python.exe"

# 1. A clean virtual environment with only what the app needs: PyInstaller bundles what it finds there.
if (-not (Test-Path $python)) {
    $index = @{ cuda = "https://download.pytorch.org/whl/cu130"; cpu = "https://download.pytorch.org/whl/cpu" }[$Torch]
    Run "venv" { py "-$PythonVersion" -m venv $venv }
    Run "pip upgrade" { & $python -m pip install --upgrade pip }
    Run "torch install" { & $python -m pip install "torch==2.14.1" --index-url $index }
    Run "requirements install" { & $python -m pip install -r requirements.txt }
}

# 2. Freeze. LocalRAG.spec says what goes in.
Write-Host "== PyInstaller ($Torch build)"
Run "PyInstaller" { & $python -m PyInstaller LocalRAG.spec --noconfirm --clean --distpath "dist\$Torch" --workpath "build\$Torch" }
$app = "dist\$Torch\LocalRAG"

# 3. What people should see next to LocalRAG.exe: the sample documents, the README, and the lab's embedding model
#    (90 MB), so a first DB can be built without internet.
Copy-Item sample_docs "$app\sample_docs" -Recurse -Force
Copy-Item README.txt $app -Force
$minilm = Run "MiniLM download" { & $python -c "import rag_engine as r; print(r.fetch_model(r.EMBEDDINGS[0].id))" } |
    Select-Object -Last 1
New-Item -ItemType Directory -Force "$app\models" | Out-Null
Copy-Item $minilm "$app\models\all-MiniLM-L6-v2" -Recurse -Force

# 4. Run the frozen app without its window: build a DB from the samples, reopen it, load the smallest Qwen, answer.
#    A windowed .exe returns to the prompt at once, so wait for it explicitly.
Write-Host "== Self-test of the frozen app"
$report = Join-Path $PWD "build\$Torch\selftest.txt"
if (Test-Path $report) { Remove-Item $report }          # never show an older run's report for this one
$test = Start-Process "$app\LocalRAG.exe" -ArgumentList "--selftest", "--llm", "Qwen/Qwen3.5-0.8B", "--out", "`"$report`"" -Wait -PassThru
if (-not (Test-Path $report)) {
    throw "The frozen app crashed before writing its report (exit code $($test.ExitCode)): see Windows Event Viewer > Application"
}
Get-Content -Encoding UTF8 $report
if ($test.ExitCode -ne 0) { throw "The frozen app failed its self-test (report above)" }

# 5. One zip to share.
$zip = "dist\LocalRAG-$version-win64-$Torch.zip"
if (Test-Path $zip) { Remove-Item $zip }
Run "zip" { tar.exe -a -c -f $zip -C "dist\$Torch" LocalRAG }
Write-Host ("== Done: {0} ({1:N2} GB)" -f $zip, ((Get-Item $zip).Length / 1GB))
