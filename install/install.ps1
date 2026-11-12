<#
hexiel installer for Windows (PowerShell 5.1 or 7).

    irm https://hexiel.tech/install.ps1 | iex

What it does, in this order, and nothing else:
  1. installs uv (https://astral.sh/uv) into %USERPROFILE%\.local\bin if there is no uv and no Python 3.11+ —
     a pinned release, downloaded from GitHub with its .sha256 file and verified (Get-FileHash) before use
  2. installs hexiel with `uv tool install` (uv brings its own Python when Windows has none)
  3. adds %USERPROFILE%\.local\bin to the user PATH for new terminals
  4. recommends PowerShell 7 (winget install Microsoft.PowerShell) when only Windows PowerShell 5.1 is present
  5. checks the built-in tools' dependencies (Pillow, ffmpeg … come with the install, no admin rights) and adds tesseract
     (screen OCR) with winget when winget is there ($env:HEXIEL_NO_SYSTEM_DEPS = "1" skips)
  6. runs `hexiel --setup`: finds the keys and agents already on this machine and ends in a working chat

Variables: $env:HEXIEL_VERSION = "0.9.3" (default latest) · $env:HEXIEL_SPEC = ".[serve]" (this checkout) · $env:HEXIEL_NO_SETUP = "1" · $env:UV_VERSION
#>
$ErrorActionPreference = "Stop"
$UvVersion = if ($env:UV_VERSION) { $env:UV_VERSION } else { "0.13.7" }
$Spec = if ($env:HEXIEL_VERSION) { "hexiel[serve]==$($env:HEXIEL_VERSION)" } elseif ($env:HEXIEL_SPEC) { $env:HEXIEL_SPEC } else { "hexiel[serve]" }
$Bin = Join-Path $env:USERPROFILE ".local\bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null

function Say($text) { Write-Host $text }
function Fail($text) { Write-Host "hexiel install: $text" -ForegroundColor Red; exit 1 }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin -and -not $env:HEXIEL_ALLOW_ADMIN) {
  Fail "run this from a normal (non-administrator) PowerShell — hexiel is a per-user tool (set HEXIEL_ALLOW_ADMIN=1 to override)"
}

# ---------------------------------------------------------------- uv, verified
function Install-Uv {
  $arch = if ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture -eq "Arm64") { "aarch64" } else { "x86_64" }
  $name = "uv-$arch-pc-windows-msvc.zip"
  $base = "https://github.com/astral-sh/uv/releases/download/$UvVersion"
  $tmp = Join-Path ([IO.Path]::GetTempPath()) ("hexiel-uv-" + [Guid]::NewGuid().ToString("n"))
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  Say "downloading uv $UvVersion ($name) …"
  Invoke-WebRequest -Uri "$base/$name" -OutFile (Join-Path $tmp $name)
  Invoke-WebRequest -Uri "$base/$name.sha256" -OutFile (Join-Path $tmp "$name.sha256")
  $expected = ((Get-Content (Join-Path $tmp "$name.sha256") -Raw) -split '\s+')[0].ToLower()
  $actual = (Get-FileHash -Algorithm SHA256 (Join-Path $tmp $name)).Hash.ToLower()
  if ($expected -ne $actual) { Fail "checksum mismatch for $name (expected $expected, got $actual) — not installing" }
  Expand-Archive -Path (Join-Path $tmp $name) -DestinationPath $tmp -Force
  Get-ChildItem -Path $tmp -Recurse -Include uv.exe, uvx.exe | ForEach-Object { Copy-Item $_.FullName (Join-Path $Bin $_.Name) -Force }
  Remove-Item -Recurse -Force $tmp
  Say "uv $UvVersion installed to $Bin (checksum verified)"
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
  if (Test-Path (Join-Path $Bin "uv.exe")) { $uv = Join-Path $Bin "uv.exe" } else { Install-Uv; $uv = Join-Path $Bin "uv.exe" }
} else { $uv = $uv.Source }

# ---------------------------------------------------------------- hexiel
Say "installing $Spec with uv …"
& $uv tool install --python ">=3.11" --force $Spec
if ($LASTEXITCODE -ne 0) { Fail "uv tool install failed (exit $LASTEXITCODE)" }
& $uv tool update-shell 2>$null | Out-Null

# ---------------------------------------------------------------- PATH for new terminals
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not ($userPath -split ";" | Where-Object { $_ -eq $Bin })) {
  [Environment]::SetEnvironmentVariable("Path", "$Bin;$userPath", "User")
  Say "added $Bin to your user PATH for new terminals"
}
$env:Path = "$Bin;$env:Path"

$hexiel = Get-Command hexiel -ErrorAction SilentlyContinue
if (-not $hexiel) { Fail "hexiel was installed but not found on PATH — open a new terminal and run: hexiel --doctor" }
Say ""
Say ("✓ " + (& hexiel --version) + " installed at " + $hexiel.Source)

if ($PSVersionTable.PSVersion.Major -lt 7 -and -not (Get-Command pwsh -ErrorAction SilentlyContinue)) {
  Say "tip: hexiel's shell tools work best with PowerShell 7 —  winget install Microsoft.PowerShell"
}

# ---------------------------------------------------------------- tools the built-ins use
# Pillow, segno, pillow-heif and a bundled ffmpeg are ordinary dependencies of hexiel, so they are already in.
$toolPy = Join-Path $env:APPDATA "uv\tools\hexiel\Scripts\python.exe"
if (-not (Test-Path $toolPy)) { $toolPy = Join-Path $env:USERPROFILE ".local\share\uv\tools\hexiel\Scripts\python.exe" }
if (Test-Path $toolPy) {
  & $toolPy -c "import PIL, segno, imageio_ffmpeg" 2>$null
  if ($LASTEXITCODE -ne 0) { Say "note: some built-in tool libraries are missing — run: hexiel --doctor" }
}
if (-not $env:HEXIEL_NO_SYSTEM_DEPS -and -not (Get-Command tesseract -ErrorAction SilentlyContinue)) {
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    winget install --id UB-Mannheim.TesseractOCR -e --silent --accept-package-agreements --accept-source-agreements 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Say "installed tesseract (screen OCR)" } else { Say "optional: winget install UB-Mannheim.TesseractOCR (screen OCR)" }
  } else { Say "optional: install tesseract for screen OCR (https://github.com/UB-Mannheim/tesseract/wiki)" }
}

# ---------------------------------------------------------------- first run
if (-not $env:HEXIEL_NO_SETUP) {
  & hexiel --setup
} else {
  Say "next: hexiel --setup"
}
