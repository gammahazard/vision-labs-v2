# build.ps1 — Sequential Docker Compose build for Windows.
#
# Docker on Windows often fails with "rpc error: code = Unavailable"
# when building multiple containers in parallel. This script builds
# each service one at a time with --progress=plain for readable logs.
#
# Usage:
#   .\build.ps1              Build all services
#   .\build.ps1 -Up          Build all services then start the stack
#   .\build.ps1 dashboard tracker   Build only specific services
#   .\build.ps1 dashboard -Up       Build specific services then start

param(
    [switch]$Up,
    [Parameter(ValueFromRemainingArguments)]
    [string[]]$Services
)

$AllServices = @(
    "camera-ingester",
    "pose-detector",
    "vehicle-detector",
    "tracker",
    "face-recognizer",
    "dashboard"
)

# Default to all services if none specified
if (-not $Services -or $Services.Count -eq 0) {
    $Targets = $AllServices
} else {
    $Targets = $Services
}

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Vision Labs — Sequential Build" -ForegroundColor Cyan
Write-Host "  $(Get-Date)" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# Pull Redis image first
Write-Host "▶ Pulling redis:7-alpine..." -ForegroundColor Yellow
docker compose pull redis
if ($LASTEXITCODE -ne 0) {
    Write-Host "✗ Redis pull failed" -ForegroundColor Red
    exit 1
}
Write-Host "✓ Redis image ready" -ForegroundColor Green
Write-Host ""

# Build each service sequentially
foreach ($svc in $Targets) {
    Write-Host "──────────────────────────────────────────" -ForegroundColor DarkGray
    Write-Host "▶ Building: $svc" -ForegroundColor Yellow
    Write-Host "──────────────────────────────────────────" -ForegroundColor DarkGray

    docker compose build --progress=plain $svc

    if ($LASTEXITCODE -ne 0) {
        Write-Host "✗ $svc FAILED" -ForegroundColor Red
        Write-Host "============================================" -ForegroundColor Red
        Write-Host "  BUILD FAILED — see errors above" -ForegroundColor Red
        Write-Host "============================================" -ForegroundColor Red
        exit 1
    }

    Write-Host "✓ $svc built successfully" -ForegroundColor Green
    Write-Host ""
}

Write-Host "============================================" -ForegroundColor Green
Write-Host "  All services built successfully! ✓" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green

if ($Up) {
    Write-Host ""
    Write-Host "▶ Starting stack..." -ForegroundColor Yellow
    docker compose up -d
    Write-Host "✓ Stack is running" -ForegroundColor Green
    Write-Host ""
    docker compose ps
}
