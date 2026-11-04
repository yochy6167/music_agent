<#
.SYNOPSIS
  Builds the SoundOps SD image (Raspberry Pi OS Lite 64-bit + music_agent + Tailscale).

.DESCRIPTION
  Needs Docker Desktop (Linux containers). Runs about 30-60 minutes; the result is
  image\build\soundops-img-v<ImageVersion>-agent-<AgentRef>.img.xz (+ .sha256),
  ready for Raspberry Pi Imager ("Use custom").

.EXAMPLE
  .\image\build.ps1                       # latest agent tag, image 1.0.0
  .\image\build.ps1 -AgentRef v2.2.3 -ImageVersion 1.0.1
#>
param(
  # A released agent tag (vX.Y.Z). Empty: the newest tag on GitHub.
  [string]$AgentRef = "",
  [string]$ImageVersion = "1.0.0",
  # The support SSH key (public, not a secret). Empty string: no SSH key in the image.
  [string]$SshPublicKey = "$env:USERPROFILE\.ssh\id_ed25519.pub",
  # Test build: this working copy's agent code (uncommitted too) over the tag.
  # Never for customers: release a tag and build without it.
  [switch]$LocalAgent
)
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$repo = "https://github.com/yochy6167/music_agent.git"

if (-not $AgentRef) {
  $AgentRef = git ls-remote --tags --refs $repo 'v*' |
    ForEach-Object { ($_ -split "refs/tags/")[1] } |
    Sort-Object { [version]($_.TrimStart('v') -replace '[^0-9.].*$', '') } |
    Select-Object -Last 1
  if (-not $AgentRef) { throw "No vX.Y.Z tag found on $repo" }
}
if ($AgentRef -notmatch '^v\d+\.\d+\.\d+') { throw "AgentRef must be a released tag like v2.2.3 (got '$AgentRef')" }

$key = ""
if ($SshPublicKey) {
  if (-not (Test-Path $SshPublicKey)) { throw "SSH public key not found: $SshPublicKey" }
  $key = (Get-Content $SshPublicKey -Raw).Trim()
  if ($key -notmatch '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-\S+) ') { throw "$SshPublicKey is not an SSH public key" }
}

Write-Host "==> SoundOps image $ImageVersion with agent $AgentRef"
New-Item -ItemType Directory -Force "$here\build" | Out-Null

Write-Host "==> arm64 emulation for Docker"
docker run --privileged --rm tonistiigi/binfmt --install arm64 | Out-Null
docker volume create soundops-image-work | Out-Null

$extra = @()
if ($LocalAgent) {
  $ImageVersion = "$ImageVersion-dev"
  $extra = @("-v", "$(Split-Path $here -Parent):/agent:ro")
  Write-Host "==> TEST BUILD with the local agent code (image $ImageVersion)"
}

docker run --rm --privileged `
  -v soundops-image-work:/work `
  -v "${here}:/src:ro" `
  -v "${here}\build:/out" `
  @extra `
  -e AGENT_REF=$AgentRef -e IMAGE_VERSION=$ImageVersion -e SSH_PUBKEY="$key" `
  debian:trixie bash -c "sed 's/\r$//' /src/build-image.sh > /tmp/build-image.sh && bash /tmp/build-image.sh"
if ($LASTEXITCODE -ne 0) { throw "Image build failed (exit $LASTEXITCODE)" }
Write-Host "==> Ready in $here\build"
