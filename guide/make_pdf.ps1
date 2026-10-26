# Prints guide.html to ..\Python_to_EXE_Guide.pdf with the Microsoft Edge that ships with Windows.
#   powershell -File make_pdf.ps1
$edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
          "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $edge) { throw "Microsoft Edge not found" }
$html = Join-Path $PSScriptRoot "guide.html"
$pdf = Join-Path (Split-Path $PSScriptRoot) "Python_to_EXE_Guide.pdf"
$url = "file:///" + ($html -replace "\\", "/")
& $edge --headless=new --disable-gpu --no-pdf-header-footer --virtual-time-budget=10000 "--print-to-pdf=$pdf" $url 2>&1 |
    ForEach-Object { "$_" }
Write-Host ("{0} ({1:N0} KB)" -f $pdf, ((Get-Item $pdf).Length / 1KB))
