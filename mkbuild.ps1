#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$NoInstaller,  # Пропустить сборку установщика
    [ValidateSet('z','y','x')]
    [string]$Bump = 'x'    # Какую часть версии поднять (z/y/x) — для неинтерактивного режима
)

$ErrorActionPreference = 'Stop'

$BuildDir   = './build'
$BundleDir  = './build-bundle'
$IgnoreFile = './.buildignore'

New-Item -ItemType Directory -Force -Path $BuildDir  | Out-Null
New-Item -ItemType Directory -Force -Path $BundleDir | Out-Null

# --- поиск последней версии в build/ и build-bundle/ ---
$versions = @()
foreach ($d in Get-ChildItem -LiteralPath $BuildDir -Directory -ErrorAction SilentlyContinue) {
    if ($d.Name -match '^release-v(\d+\.\d+\.\d+)$') { $versions += $Matches[1] }
}
foreach ($f in Get-ChildItem -LiteralPath $BundleDir -File -ErrorAction SilentlyContinue) {
    if ($f.Name -match '^obxod-box-(\d+\.\d+\.\d+)-windows\.zip$') { $versions += $Matches[1] }
}

$sorted = @($versions | Select-Object -Unique | Sort-Object { [version]$_ })
$latest = if ($sorted.Count -gt 0) { $sorted[-1] } else { '0.0.0' }

if ($sorted.Count -gt 0) {
    Write-Host "Последняя найденная версия: v$latest"
} else {
    Write-Host "Предыдущих релизов нет, стартуем с v$latest"
}

# --- выбор какой компонент поднять ---
$choice = $Bump.ToLower()

$p = $latest.Split('.')
$z = [int]$p[0]; $y = [int]$p[1]; $x = [int]$p[2]

switch ($choice) {
    'z' { $z++; $y = 0; $x = 0 }
    'y' { $y++; $x = 0 }
    'x' { $x++ }
    default { Write-Error "Неверный выбор: $choice"; exit 1 }
}

$newVersion  = "$z.$y.$x"
$releaseName = "release-v$newVersion"
$finalArchiveName = "obxod-box-$newVersion-windows.zip"
Write-Host "Новая версия: v$newVersion"

# --- разбор .buildignore (упрощённый gitignore) ---
function Convert-GitignorePattern {
    param([string]$Pattern)

    $negated = $false
    if ($Pattern.StartsWith('!')) {
        $negated = $true
        $Pattern = $Pattern.Substring(1)
    }

    $dirOnly = $false
    if ($Pattern.EndsWith('/')) { $dirOnly = $true; $Pattern = $Pattern.TrimEnd('/') }

    $anchored = $false
    if ($Pattern.StartsWith('/')) { $anchored = $true; $Pattern = $Pattern.Substring(1) }
    elseif ($Pattern.Contains('/')) { $anchored = $true }

    $escaped = [regex]::Escape($Pattern)
    $escaped = $escaped.Replace('\*\*', '.*').Replace('\*', '[^/]*').Replace('\?', '[^/]')

    $regex = if ($anchored) { "^$escaped(/|$)" } else { "(^|/)$escaped(/|$)" }
    [pscustomobject]@{ Regex = $regex; DirOnly = $dirOnly; Raw = $Pattern; Negated = $negated }
}

$patterns = @()
if (Test-Path -LiteralPath $IgnoreFile) {
    Write-Host "Использую $IgnoreFile"
    foreach ($line in (Get-Content -LiteralPath $IgnoreFile)) {
        $l = $line.Trim()
        if (-not $l) { continue }
        if ($l.StartsWith('#')) { continue }
        $patterns += Convert-GitignorePattern $l
    }
} else {
    Write-Host "$IgnoreFile не найден — копирую всё подряд"
}

# служебные каталоги всегда исключаем
$patterns += Convert-GitignorePattern '/build/'
$patterns += Convert-GitignorePattern '/build-bundle/'
$patterns += Convert-GitignorePattern '/.git/'

function Test-Ignored {
    param([string]$Rel, [bool]$IsDir)
    $ignored = $false
    foreach ($pt in $patterns) {
        if ($pt.DirOnly -and -not $IsDir) { continue }
        if ($Rel -match $pt.Regex) {
            $ignored = -not $pt.Negated
        }
    }
    return $ignored
}   

function Copy-Tree {
    param([string]$Source, [string]$Destination, [string]$BaseRel = '')
    foreach ($item in (Get-ChildItem -LiteralPath $Source -Force)) {
        $rel   = if ($BaseRel) { "$BaseRel/$($item.Name)" } else { $item.Name }
        $isDir = $item.PSIsContainer

        if (Test-Ignored -Rel $rel -IsDir $isDir) {
            Write-Host "  skip: $rel"
            continue
        }

        $destPath = Join-Path $Destination $item.Name
        if ($isDir) {
            New-Item -ItemType Directory -Force -Path $destPath | Out-Null
            Copy-Tree -Source $item.FullName -Destination $destPath -BaseRel $rel
        } else {
            Copy-Item -LiteralPath $item.FullName -Destination $destPath -Force
        }
    }
}

# ============================================================
# 1. СБОРКА SFX УСТАНОВЩИКА (Proper SFX via PyInstaller)
# ============================================================
$installerExe = $null
if (-not $NoInstaller) {
    Write-Host "`n========== СБОРКА УСТАНОВЩИКА (Proper SFX) =========="
    
    # Используем Python скрипт для сборки proper SFX
    $sfxBuilderScript = Join-Path (Get-Location).Path "installer\build_sfx_proper.py"
    if (-not (Test-Path -LiteralPath $sfxBuilderScript)) {
        Write-Error "SFX builder script not found: $sfxBuilderScript"
        exit 1
    }
    
    Write-Host "Запуск proper SFX builder..."
    $result = & python $sfxBuilderScript `
        --source (Get-Location).Path `
        --output (Resolve-Path -LiteralPath $BundleDir).Path `
        --version $newVersion 2>&1
    
    if ($LASTEXITCODE -ne 0) {
        Write-Error "SFX builder failed with exit code $LASTEXITCODE"
        Write-Host $result
        exit 1
    }
    
    $installerExe = Join-Path $BundleDir "Obxod-BOX-Setup-v$newVersion.exe"
    if (-not (Test-Path -LiteralPath $installerExe)) {
        Write-Error "SFX installer not found at expected path: $installerExe"
        exit 1
    }
    
    Write-Host "SFX установщик создан: $installerExe ($([math]::Round((Get-Item $installerExe).Length/1MB, 2)) MB)"
}

# ============================================================
# 2. СБОРКА БАНДЛА ПРИЛОЖЕНИЯ (исходники для установщика)
# ============================================================
Write-Host "`n========== СБОРКА БАНДЛА ПРИЛОЖЕНИЯ =========="

$dest = Join-Path $BuildDir $releaseName
if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Recurse -Force }
New-Item -ItemType Directory -Force -Path $dest | Out-Null

Write-Host "Копирую файлы приложения в $dest ..."
Copy-Tree -Source (Get-Location).Path -Destination $dest

# ============================================================
# 3. СОЗДАНИЕ ИТОГОВОГО АРХИВА (установщик + бандл)
# ============================================================
Write-Host "`n========== СОЗДАНИЕ ИТОГОВОГО АРХИВА =========="

$finalArchivePath = Join-Path $BundleDir $finalArchiveName
if (Test-Path -LiteralPath $finalArchivePath) { Remove-Item -LiteralPath $finalArchivePath -Force }

# Временная директория для финального архива
$finalTempDir = Join-Path $BundleDir "final_$newVersion"
if (Test-Path -LiteralPath $finalTempDir) { Remove-Item -LiteralPath $finalTempDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $finalTempDir | Out-Null

# Копируем установщик
if ($installerExe -and (Test-Path -LiteralPath $installerExe)) {
    $installerDest = Join-Path $finalTempDir "Obxod-BOX-Setup-v$newVersion.exe"
    Copy-Item -LiteralPath $installerExe -Destination $installerDest -Force
    Write-Host "  Добавлен установщик: Obxod-BOX-Setup-v$newVersion.exe"
}

# Копируем бандл приложения (как папку)
$appBundleDir = Join-Path $finalTempDir "obxod-box-v$newVersion"
Copy-Item -LiteralPath $dest -Destination $appBundleDir -Recurse -Force
Write-Host "  Добавлен бандл приложения: obxod-box-v$newVersion/"

# Упаковываем финальный архив
Write-Host "Упаковываю финальный архив: $finalArchivePath"
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $finalTempDir,
    $finalArchivePath,
    [System.IO.Compression.CompressionLevel]::Optimal,
    $false
)

Write-Host "Финальный архив: $finalArchivePath ($([math]::Round((Get-Item $finalArchivePath).Length/1MB, 2)) MB)"

# ============================================================
# КОПИРУЕМ УСТАНОВЩИК И БАНДЛ В build-bundle ДЛЯ РЕЛИЗА
# ============================================================
Write-Host "`n========== КОПИРОВАНИЕ АРТЕФАКТОВ ДЛЯ РЕЛИЗА =========="

# Копируем установщик в build-bundle
if ($installerExe -and (Test-Path -LiteralPath $installerExe)) {
    $installerReleasePath = Join-Path $BundleDir "Obxod-BOX-Setup-v$newVersion.exe"
    Copy-Item -LiteralPath $installerExe -Destination $installerReleasePath -Force
    Write-Host "Установщик скопирован: $installerReleasePath"
}

# Копируем бандл в build-bundle
$bundleReleasePath = Join-Path $BundleDir "obxod-box-v$newVersion"
if (Test-Path -LiteralPath $bundleReleasePath) { Remove-Item -LiteralPath $bundleReleasePath -Recurse -Force }
Copy-Item -LiteralPath $dest -Destination $bundleReleasePath -Recurse -Force
Write-Host "Бандл скопирован: $bundleReleasePath"

# ============================================================
# 4. ОЧИСТКА ТОЛЬКО ВРЕМЕННЫХ ФАЙЛОВ ТЕКУЩЕЙ СБОРКИ
# ============================================================
Write-Host "`n========== ОЧИСТКА ВРЕМЕННЫХ ФАЙЛОВ =========="

# Удаляем временные директории текущей сборки
Remove-Item -LiteralPath $dest -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $finalTempDir -Recurse -Force -ErrorAction SilentlyContinue

# Удаляем временные файлы сборки установщика из build-bundle (sfx_build_*, spec файлы)
# НЕ трогаем никакие релизы (obxod-box-*-windows.zip, release-v*.zip) - они остаются навсегда
# НЕ трогаем артефакты релиза (Obxod-BOX-Setup-v*.exe, obxod-box-v*/) - они нужны для GitHub Release
$tempPatterns = @(
    "sfx_build_*",
    "final_*",
    "*.spec",
    "payload.zip",
    "payload-v*.zip",
    "Obxod-BOX-Setup-v*.bat"
)

foreach ($pattern in $tempPatterns) {
    Get-ChildItem -LiteralPath $BundleDir -Filter $pattern -ErrorAction SilentlyContinue | 
    Remove-Item -Force -Recurse -ErrorAction SilentlyContinue
}

Write-Host "`n========== ГОТОВО =========="
Write-Host "Итоговый артефакт: $finalArchivePath"
Write-Host "Содержимое архива:"
Write-Host "  - Obxod-BOX-Setup-v$newVersion.exe  (установщик)"
Write-Host "  - obxod-box-v$newVersion/           (бандл приложения)"
Write-Host "`nВ build-bundle: $finalArchiveName + все предыдущие релизы (не трогаем)"