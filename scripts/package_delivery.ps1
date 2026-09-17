param(
    [string]$DestinationDirectory = [Environment]::GetFolderPath('Desktop')
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$destination = (Resolve-Path -LiteralPath $DestinationDirectory).Path
$tempRoot = [IO.Path]::GetTempPath().TrimEnd([IO.Path]::DirectorySeparatorChar)
$token = [Guid]::NewGuid().ToString('N')
$staging = Join-Path $tempRoot ("ai-copy-workbench-package-$token")
$payload = Join-Path $staging 'AI文案工作台'
$archive = Join-Path $destination ("AI文案工作台-最终交付-{0}-{1}.zip" -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $token.Substring(0, 6))
$archiveCreated = $false

New-Item -ItemType Directory -Path $payload -Force | Out-Null

try {
    foreach ($directory in @('backend', 'docs', 'plugins', 'scripts', 'skills', 'tests')) {
        Copy-Item -LiteralPath (Join-Path $projectRoot $directory) -Destination (Join-Path $payload $directory) -Recurse
    }

    $frontend = Join-Path $payload 'frontend'
    New-Item -ItemType Directory -Path $frontend -Force | Out-Null
    foreach ($directory in @('src', 'dist')) {
        Copy-Item -LiteralPath (Join-Path $projectRoot "frontend\$directory") -Destination (Join-Path $frontend $directory) -Recurse
    }
    foreach ($file in @('index.html', 'package.json', 'package-lock.json', 'tsconfig.json', 'tsconfig.app.json', 'tsconfig.node.json', 'vite.config.ts')) {
        Copy-Item -LiteralPath (Join-Path $projectRoot "frontend\$file") -Destination (Join-Path $frontend $file)
    }
    foreach ($file in @('README.md', 'requirements.txt', '.env.example', '.gitignore', '启动文案工作台.bat')) {
        Copy-Item -LiteralPath (Join-Path $projectRoot $file) -Destination (Join-Path $payload $file)
    }

    $stagingResolved = (Resolve-Path -LiteralPath $staging).Path
    $safePrefix = $stagingResolved + [IO.Path]::DirectorySeparatorChar
    $cacheDirectories = @(Get-ChildItem -LiteralPath $stagingResolved -Recurse -Directory -Force |
        Where-Object { $_.Name -in @('__pycache__', '.pytest_cache', 'node_modules', '.workbench') })
    foreach ($cache in $cacheDirectories) {
        $resolved = (Resolve-Path -LiteralPath $cache.FullName).Path
        if (-not $resolved.StartsWith($safePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "缓存目录越界：$resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
    Get-ChildItem -LiteralPath $stagingResolved -Recurse -File -Filter '*.pyc' -Force | Remove-Item -Force

    Compress-Archive -LiteralPath $payload -DestinationPath $archive -CompressionLevel Optimal
    $archiveCreated = $true

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($archive)
    try {
        $entries = @($zip.Entries | ForEach-Object FullName)
        $blocked = @($entries | Where-Object {
            $_ -match '(^|/)(\.env$|\.workbench/|node_modules/|__pycache__/|\.pytest_cache/)' -or $_ -match '\.pyc$'
        })
        if ($blocked.Count -gt 0) {
            throw "交付包包含缓存或私密文件：$($blocked -join ', ')"
        }
        foreach ($required in @(
            'AI文案工作台/README.md',
            'AI文案工作台/启动文案工作台.bat',
            'AI文案工作台/frontend/dist/index.html',
            'AI文案工作台/docs/生活案例朋友圈工作台说明.md'
        )) {
            if ($entries -notcontains $required) {
                throw "交付包缺少：$required"
            }
        }
    }
    finally {
        $zip.Dispose()
    }

    $item = Get-Item -LiteralPath $archive
    $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $archive
    [pscustomobject]@{
        Archive = $item.FullName
        SizeMB = [math]::Round($item.Length / 1MB, 2)
        Entries = $entries.Count
        SHA256 = $hash.Hash
        SensitiveDataExcluded = $true
    } | ConvertTo-Json -Depth 3
}
catch {
    if ($archiveCreated -and (Test-Path -LiteralPath $archive)) {
        Remove-Item -LiteralPath $archive -Force
    }
    throw
}
finally {
    if (Test-Path -LiteralPath $staging) {
        $resolvedStaging = (Resolve-Path -LiteralPath $staging).Path
        $expectedPrefix = $tempRoot + [IO.Path]::DirectorySeparatorChar + 'ai-copy-workbench-package-'
        if ($resolvedStaging.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
        }
    }
}
