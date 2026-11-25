# Agent Mesh Windows Installer (Desktop GUI + CLI)
# Downloads a checksum-verified portable release archive for the current user.
# Compatible with Windows PowerShell 5.1+; requires no administrator privileges,
# no pre-installed Python runtime, and no local compiler.
#
# Quick run:
#   irm https://raw.githubusercontent.com/JamesHu6657/agent-mesh-kiroween/main/scripts/install.ps1 | iex
#
# Configurable environment variables:
#   AGENT_MESH_VERSION          Pin a specific release version (defaults to latest stable GitHub release, or latest prerelease when no stable release exists)
#   AGENT_MESH_REPO             GitHub owner/repo slug (defaults to JamesHu6657/agent-mesh-kiroween)
#   AGENT_MESH_RELEASE_BASE_URL Custom mirror base URL for release assets
#   AGENT_MESH_INSTALL_DIR      Root directory for versioned installs (defaults to %LOCALAPPDATA%\AgentMesh)
#   AGENT_MESH_BIN_DIR          Directory where mesh.cmd shim is placed (defaults to %USERPROFILE%\.local\bin)
#   AGENT_MESH_NO_PATH          Set to "1" to leave the user PATH unchanged
#   AGENT_MESH_NO_SHORTCUT      Set to "1" to omit creating a Start Menu shortcut
#
# License: MIT License (Agent Mesh)
#
# Operational guarantees:
#   - Enforces TLS 1.2 and checks SHA-256 against SHA256SUMS.txt before archive extraction.
#   - Installs per-user only (no elevation, never edits machine/system PATH).
#   - Uses temporary staging plus a completion marker so partial installs are never activated.
#   - Refuses to replace any existing mesh.exe or mesh.cmd shim not managed by this installer.
#   - Never reads, modifies, or removes user state in %USERPROFILE%\.agent-mesh.

Set-Variable -Name ErrorActionPreference -Value 'Stop'
$tlsProtocols = [System.Net.SecurityProtocolType]::Tls12
if ([System.Enum]::IsDefined([System.Net.SecurityProtocolType], 'Tls13')) {
  $tlsProtocols = $tlsProtocols -bor [System.Enum]::Parse([System.Net.SecurityProtocolType], 'Tls13')
}
[System.Net.ServicePointManager]::SecurityProtocol = $tlsProtocols

function Write-InstallLog {
  param([string]$Text)
  [System.Console]::Error.WriteLine("[agent-mesh] $Text")
}

function Stop-InstallFailure {
  param([string]$Reason)
  Write-Error "Agent Mesh installer failed: $Reason"
  exit 1
}

function Resolve-InstallerPath {
  param([string]$RawPath)
  return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($RawPath)
}

function Format-PathEntry {
  param([string]$Entry)
  if ([string]::IsNullOrWhiteSpace($Entry)) { return '' }
  return $Entry.Trim().TrimEnd([char[]]'\/')
}

function ConvertTo-MeshDirPrefix {
  param([string]$Directory)
  $resolved = Resolve-InstallerPath -RawPath $Directory
  $trimmed = $resolved.TrimEnd([char[]]'\/')
  return ($trimmed + '\')
}

function Read-InstallerConfig {
  $slug = 'JamesHu6657/agent-mesh-kiroween'
  if (-not [string]::IsNullOrWhiteSpace($env:AGENT_MESH_REPO)) {
    $slug = $env:AGENT_MESH_REPO.Trim().Trim('/')
  }
  if (($slug -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') -or ($slug -match '[\x00-\x1F\x7F]')) {
    Stop-InstallFailure "invalid AGENT_MESH_REPO '$slug' (expected 'owner/repo')"
  }
  $slugParts = $slug.Split([char[]]'/')
  if ($slugParts[0] -eq '.' -or $slugParts[0] -eq '..' -or $slugParts[1] -eq '.' -or $slugParts[1] -eq '..') {
    Stop-InstallFailure "invalid AGENT_MESH_REPO '$slug' (dot path segments are not allowed)"
  }

  $releaseRootUrl = "https://github.com/$slug/releases/download"
  if (-not [string]::IsNullOrWhiteSpace($env:AGENT_MESH_RELEASE_BASE_URL)) {
    $releaseRootUrl = $env:AGENT_MESH_RELEASE_BASE_URL.Trim().TrimEnd('/')
  }
  if (($releaseRootUrl -notmatch '^https?://[^\s''"\\<>?#]+$') -or ($releaseRootUrl -match '[\x00-\x1F\x7F]')) {
    Stop-InstallFailure "invalid AGENT_MESH_RELEASE_BASE_URL '$releaseRootUrl'"
  }

  $appDataLocal = if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    $env:LOCALAPPDATA
  } else {
    [System.IO.Path]::Combine($HOME, 'AppData', 'Local')
  }

  $rawInstallRoot = if (-not [string]::IsNullOrWhiteSpace($env:AGENT_MESH_INSTALL_DIR)) {
    $env:AGENT_MESH_INSTALL_DIR
  } else {
    [System.IO.Path]::Combine($appDataLocal, 'AgentMesh')
  }

  $rawShimRoot = if (-not [string]::IsNullOrWhiteSpace($env:AGENT_MESH_BIN_DIR)) {
    $env:AGENT_MESH_BIN_DIR
  } else {
    [System.IO.Path]::Combine($HOME, '.local', 'bin')
  }

  return @{
    Repo           = $slug
    ReleaseBaseUrl = $releaseRootUrl
    PinnedVersion  = if ($null -ne $env:AGENT_MESH_VERSION) { $env:AGENT_MESH_VERSION.Trim() } else { '' }
    InstallRoot    = Resolve-InstallerPath -RawPath $rawInstallRoot
    ShimRoot       = Resolve-InstallerPath -RawPath $rawShimRoot
    SkipUserPath   = ($env:AGENT_MESH_NO_PATH -eq '1')
    SkipShortcut   = ($env:AGENT_MESH_NO_SHORTCUT -eq '1')
  }
}

function Get-HostArchTag {
  # Account for 32-bit PowerShell running under WOW64 on 64-bit Windows hosts.
  $nativeArch = $env:PROCESSOR_ARCHITECTURE
  if (-not [string]::IsNullOrWhiteSpace($env:PROCESSOR_ARCHITEW6432)) {
    $nativeArch = $env:PROCESSOR_ARCHITEW6432
  }
  $normalizedArch = if ($nativeArch) { $nativeArch.Trim().ToUpperInvariant() } else { '' }

  if ($normalizedArch -eq 'AMD64') {
    return 'x64'
  }
  if ($normalizedArch -eq 'ARM64') {
    Stop-InstallFailure 'detected Windows arm64 host; only x64 (AMD64) portable archives are currently published'
  }
  Stop-InstallFailure "unsupported Windows architecture '$normalizedArch' (only x64 / AMD64 is supported)"
}

function Get-HttpStatusCodeFromError {
  param($ErrorRecord)
  $ex = $ErrorRecord.Exception
  if ($null -eq $ex) { return 0 }
  if (-not $ex.PSObject.Properties['Response']) { return 0 }
  $resp = $ex.Response
  if ($null -eq $resp -or (-not $resp.PSObject.Properties['StatusCode'])) { return 0 }
  return [int]$resp.StatusCode
}

function Invoke-WithoutProgress {
  param([scriptblock]$Action)
  $priorPrefVar = Get-Variable -Name 'ProgressPreference' -Scope Global -ErrorAction SilentlyContinue
  $hasPriorPref = ($null -ne $priorPrefVar)
  $priorPrefValue = if ($hasPriorPref) { $priorPrefVar.Value } else { $null }
  Set-Variable -Name 'ProgressPreference' -Scope Global -Value 'SilentlyContinue'
  try {
    & $Action
  } finally {
    if ($hasPriorPref) {
      Set-Variable -Name 'ProgressPreference' -Scope Global -Value $priorPrefValue
    } else {
      Remove-Variable -Name 'ProgressPreference' -Scope Global -ErrorAction SilentlyContinue
    }
  }
}

function Invoke-HttpWithBackoff {
  param([scriptblock]$Operation)
  # Exponential backoff delays (1s, 2s, 4s) for transient network errors and HTTP 408 / 429 / 5xx.
  $backoffSeconds = @(1, 2, 4)
  $retryIdx = 0

  while ($true) {
    try {
      return (& $Operation)
    } catch {
      $httpStatus = Get-HttpStatusCodeFromError -ErrorRecord $_
      $isClientFatal = ($httpStatus -ge 400 -and $httpStatus -lt 500 -and $httpStatus -ne 408 -and $httpStatus -ne 429)
      if ($isClientFatal -or ($retryIdx -ge $backoffSeconds.Count)) {
        throw $_
      }
      $delaySec = $backoffSeconds[$retryIdx]
      $retryIdx++
      Start-Sleep -Seconds $delaySec
    }
  }
}

function Save-RemoteAsset {
  param(
    [string]$SourceUrl,
    [string]$DestinationPath
  )
  $reqHeaders = @{ 'User-Agent' = 'agent-mesh-installer' }
  Invoke-WithoutProgress -Action {
    [void](Invoke-HttpWithBackoff -Operation {
      Invoke-WebRequest -Uri $SourceUrl -OutFile $DestinationPath -UseBasicParsing -Headers $reqHeaders | Out-Null
    })
  }
}

function Resolve-TargetVersion {
  param([hashtable]$Config)
  $version = $Config.PinnedVersion
  if ([string]::IsNullOrWhiteSpace($version)) {
    $apiHeaders = @{
      'User-Agent' = 'agent-mesh-installer'
      'Accept'     = 'application/vnd.github+json'
    }
    $releaseMeta = $null
    $latestApiUrl = "https://api.github.com/repos/$($Config.Repo)/releases/latest"
    try {
      $releaseMeta = Invoke-HttpWithBackoff -Operation {
        Invoke-RestMethod -Uri $latestApiUrl -UseBasicParsing -Headers $apiHeaders
      }
    } catch {
      $httpStatus = Get-HttpStatusCodeFromError -ErrorRecord $_
      if ($httpStatus -ne 404) { throw }

      # /releases/latest excludes prereleases and returns 404 when none exist.
      # In that case, use the newest public release, which may be a prerelease.
      $releasesApiUrl = "https://api.github.com/repos/$($Config.Repo)/releases?per_page=1"
      $releaseList = Invoke-HttpWithBackoff -Operation {
        Invoke-RestMethod -Uri $releasesApiUrl -UseBasicParsing -Headers $apiHeaders
      }
      foreach ($candidate in @($releaseList)) {
        if ($null -eq $candidate) { continue }
        if ($candidate.PSObject.Properties['draft'] -and $candidate.draft) { continue }
        if ((-not $candidate.PSObject.Properties['tag_name']) -or [string]::IsNullOrWhiteSpace($candidate.tag_name)) { continue }
        $releaseMeta = $candidate
        break
      }
    }
    if (-not $releaseMeta -or [string]::IsNullOrWhiteSpace($releaseMeta.tag_name)) {
      Stop-InstallFailure 'unable to read latest release tag from GitHub API; set AGENT_MESH_VERSION manually'
    }
    $version = $releaseMeta.tag_name.Trim().TrimStart('v')
  }

  if ($version -notmatch '^(?!\.+$)[0-9A-Za-z][0-9A-Za-z._+-]*$') {
    Stop-InstallFailure "invalid AGENT_MESH_VERSION '$version'"
  }
  return $version
}

function Confirm-ArchiveChecksum {
  param(
    [string]$ManifestFile,
    [string]$ArchiveFile,
    [string]$ArchiveFileName
  )
  $matchedRow = $null
  foreach ($row in (Get-Content -LiteralPath $ManifestFile)) {
    if ($null -ne $matchedRow) { break }
    $cols = $row.Trim() -split '\s+'
    if ($cols.Count -ne 2) { continue }
    $listedName = $cols[1]
    if ($listedName.StartsWith('*')) { $listedName = $listedName.Substring(1) }
    if ([string]::Equals($listedName, $ArchiveFileName, [System.StringComparison]::Ordinal)) {
      $matchedRow = $row
    }
  }
  if (-not $matchedRow) {
    Stop-InstallFailure "$ArchiveFileName was not found in SHA256SUMS.txt; aborting before extraction"
  }

  $digestParts = $matchedRow.Trim() -split '\s+'
  $expectedDigest = $digestParts[0].ToLowerInvariant()
  if ($expectedDigest -notmatch '^[0-9a-f]{64}$') {
    Stop-InstallFailure "malformed SHA-256 entry for $ArchiveFileName in SHA256SUMS.txt"
  }

  $computedDigest = (Get-FileHash -Algorithm SHA256 -LiteralPath $ArchiveFile).Hash.ToLowerInvariant()
  if ($computedDigest -ne $expectedDigest) {
    Stop-InstallFailure "SHA-256 verification failed for $ArchiveFileName (expected $expectedDigest, got $computedDigest)"
  }
}

function Remove-DirectorySafe([string]$path) {
  # Inspect with Get-Item -Force rather than Test-Path so broken symlinks/junctions are caught.
  $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
  if ($null -eq $item) { return }

  $fullTarget = $item.FullName
  $isReparse = [bool]($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
  if ($isReparse) {
    # Never recurse through a junction or symlink in PowerShell 5.1.
    if ($item.PSIsContainer) { [System.IO.Directory]::Delete($fullTarget, $false); return }
    [System.IO.File]::Delete($fullTarget)
    return
  }

  if (-not $item.PSIsContainer) {
    $roFlag = [System.IO.FileAttributes]::ReadOnly
    if (($item.Attributes -band $roFlag) -ne 0) {
      [System.IO.File]::SetAttributes($fullTarget, ($item.Attributes -band (-bnot $roFlag)))
    }
    [System.IO.File]::Delete($fullTarget)
    return
  }

  foreach ($entry in (Get-ChildItem -LiteralPath $path -Force)) {
    Remove-DirectorySafe $entry.FullName
  }
  [System.IO.Directory]::Delete($fullTarget, $false)
}

function Test-PathListContains {
  param(
    [string]$RawPathValue,
    [string]$NormalizedDir
  )
  if ([string]::IsNullOrWhiteSpace($RawPathValue)) { return $false }
  foreach ($part in ($RawPathValue -split ';')) {
    $cleanPart = Format-PathEntry -Entry $part
    if ([string]::IsNullOrEmpty($cleanPart)) { continue }
    $expandedPart = Format-PathEntry -Entry ([System.Environment]::ExpandEnvironmentVariables($cleanPart))
    if ($cleanPart.Equals($NormalizedDir, [System.StringComparison]::OrdinalIgnoreCase) -or
        $expandedPart.Equals($NormalizedDir, [System.StringComparison]::OrdinalIgnoreCase)) {
      return $true
    }
  }
  return $false
}

function Send-EnvironmentChangeBroadcast {
  try {
    if (-not ('AgentMeshInstallerWin32.Broadcast' -as [type])) {
      $pinvokeDef = '[DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Auto)] ' +
        'public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);'
      Add-Type -Namespace 'AgentMeshInstallerWin32' -Name 'Broadcast' -MemberDefinition $pinvokeDef -ErrorAction SilentlyContinue
    }
    $broadcastOut = [UIntPtr]::Zero
    [void][AgentMeshInstallerWin32.Broadcast]::SendMessageTimeout([IntPtr]0xffff, 0x001A, [UIntPtr]::Zero, 'Environment', 0x0002, 1000, [ref]$broadcastOut)
  } catch {
    # Best-effort environment change notification; ignore in non-interactive shells.
  }
}

function Add-UserPathEntry {
  param(
    [string]$EntryDir,
    [bool]$Skip = $false
  )
  if ($Skip) { return $false }
  if ([string]::IsNullOrWhiteSpace($EntryDir) -or $EntryDir.Contains(';')) { return $false }

  $trimmedEntry = Format-PathEntry -Entry $EntryDir
  # Manipulate HKCU\Environment directly with DoNotExpandEnvironmentNames so REG_EXPAND_SZ stays intact.
  $envRegKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
  if ($null -eq $envRegKey) {
    $envRegKey = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
  }

  $entryPresent = $false
  try {
    $storedPath = $envRegKey.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    $currentPath = if ($null -ne $storedPath) { [string]$storedPath } else { '' }

    $targetKind = [Microsoft.Win32.RegistryValueKind]::ExpandString
    try {
      $detectedKind = $envRegKey.GetValueKind('Path')
      $canKeepString = ($detectedKind -eq [Microsoft.Win32.RegistryValueKind]::String) -and ($currentPath -notmatch '%[^%]+%')
      if ($detectedKind -eq [Microsoft.Win32.RegistryValueKind]::ExpandString -or $canKeepString) {
        $targetKind = $detectedKind
      }
    } catch {
      # Value does not exist yet; keep ExpandString (REG_EXPAND_SZ).
    }

    $entryPresent = Test-PathListContains -RawPathValue $currentPath -NormalizedDir $trimmedEntry
    if (-not $entryPresent) {
      $currentPathNoTrailingSep = $currentPath.TrimEnd([char[]]';')
      $updatedPath = if ([string]::IsNullOrWhiteSpace($currentPathNoTrailingSep)) { $trimmedEntry } else { "$currentPathNoTrailingSep;$trimmedEntry" }
      $envRegKey.SetValue('Path', $updatedPath, $targetKind)
      Send-EnvironmentChangeBroadcast
    }
  } finally {
    if ($null -ne $envRegKey) { $envRegKey.Dispose() }
  }

  $inSessionPath = $false
  if (-not [string]::IsNullOrWhiteSpace($env:PATH)) {
    foreach ($existing in ($env:PATH -split ';')) {
      $normalizedExisting = Format-PathEntry -Entry $existing
      if ($normalizedExisting.Equals($trimmedEntry, [System.StringComparison]::OrdinalIgnoreCase)) {
        $inSessionPath = $true
        break
      }
    }
  }
  if (-not $inSessionPath) {
    $sessionPathNoTrailingSep = if ($env:Path) { $env:Path.TrimEnd([char[]]';') } else { '' }
    $env:Path = if ($sessionPathNoTrailingSep) { "$sessionPathNoTrailingSep;$trimmedEntry" } else { $trimmedEntry }
  }
  return (-not $entryPresent)
}

function Expand-PortableZip {
  param(
    [string]$ZipPath,
    [string]$OutputDir
  )
  Invoke-WithoutProgress -Action {
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $OutputDir -Force | Out-Null
  }
}

function Test-VersionInstallComplete {
  param(
    [string]$TargetDir,
    [string]$Version
  )
  $completeStamp = [System.IO.Path]::Combine($TargetDir, '.install-complete')
  $existingInstallDir = Get-Item -LiteralPath $TargetDir -Force -ErrorAction SilentlyContinue
  $isRegularInstallDir = ($null -ne $existingInstallDir) -and
    $existingInstallDir.PSIsContainer -and
    (-not [bool]($existingInstallDir.Attributes -band [System.IO.FileAttributes]::ReparsePoint))
  if (-not ($isRegularInstallDir -and (Test-Path -LiteralPath $completeStamp -PathType Leaf))) {
    return $false
  }
  $stampText = Get-Content -LiteralPath $completeStamp -Raw -ErrorAction SilentlyContinue
  return ($null -ne $stampText -and $stampText.Trim() -eq $Version)
}

function Install-VersionedRelease {
  param(
    [hashtable]$Config,
    [string]$Version,
    [string]$ArchTag
  )
  $packageStem = "AgentMesh-$Version-win-$ArchTag-portable"
  $zipFileName = "$packageStem.zip"

  $releasesDir = Join-Path -Path $Config.InstallRoot -ChildPath 'versions'
  $targetDir = Join-Path -Path $releasesDir -ChildPath $Version
  $versionsRoot = ConvertTo-MeshDirPrefix -Directory $releasesDir
  $resolvedTarget = [System.IO.Path]::GetFullPath($targetDir)
  $targetPrefix = ConvertTo-MeshDirPrefix -Directory $resolvedTarget
  $sameAsRoot = $targetPrefix.Equals($versionsRoot, [System.StringComparison]::OrdinalIgnoreCase)
  if ((-not $resolvedTarget.StartsWith($versionsRoot, [System.StringComparison]::OrdinalIgnoreCase)) -or $sameAsRoot) {
    Stop-InstallFailure "target path '$resolvedTarget' is outside versions directory '$releasesDir'"
  }

  if (Test-VersionInstallComplete -TargetDir $targetDir -Version $Version) {
    Write-InstallLog "Agent Mesh $Version is already installed at $targetDir."
    return $targetDir
  }

  [void][System.IO.Directory]::CreateDirectory($releasesDir)
  $staging = [System.IO.Path]::Combine($releasesDir, ('.tmp-' + [System.Guid]::NewGuid().ToString('N')))
  [void][System.IO.Directory]::CreateDirectory($staging)

  try {
    Write-InstallLog "Downloading SHA256SUMS.txt and $zipFileName..."
    $manifestPath = [System.IO.Path]::Combine($staging, 'SHA256SUMS.txt')
    $archivePath = [System.IO.Path]::Combine($staging, $zipFileName)
    $tagPrefix = "$($Config.ReleaseBaseUrl)/v$Version"
    Save-RemoteAsset -SourceUrl "$tagPrefix/SHA256SUMS.txt" -DestinationPath $manifestPath
    Save-RemoteAsset -SourceUrl "$tagPrefix/$zipFileName" -DestinationPath $archivePath

    Write-InstallLog 'Checking SHA-256 digest before extracting archive...'
    Confirm-ArchiveChecksum -ManifestFile $manifestPath -ArchiveFile $archivePath -ArchiveFileName $zipFileName

    Write-InstallLog "Unpacking Agent Mesh $Version..."
    Expand-PortableZip -ZipPath $archivePath -OutputDir $staging

    $nestedFolder = [System.IO.Path]::Combine($staging, $packageStem)
    if (Test-Path -LiteralPath $nestedFolder) {
      Get-ChildItem -LiteralPath $nestedFolder -Force | Move-Item -Destination $staging -Force
      Remove-DirectorySafe $nestedFolder
    }
    Remove-Item -LiteralPath $archivePath, $manifestPath -Force

    $candidateExe = [System.IO.Path]::Combine($staging, 'mesh.exe')
    $candidateCmd = [System.IO.Path]::Combine($staging, 'mesh.cmd')
    $stagedCli = $null
    if (Test-Path -LiteralPath $candidateExe) {
      $stagedCli = $candidateExe
    } elseif (Test-Path -LiteralPath $candidateCmd) {
      $stagedCli = $candidateCmd
    }
    if (-not $stagedCli) {
      Stop-InstallFailure 'archive did not contain mesh.exe or mesh.cmd'
    }

    $cliEntryInfo = Get-Item -LiteralPath $stagedCli -Force
    if ($cliEntryInfo.PSIsContainer -or ($cliEntryInfo.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
      Stop-InstallFailure "staged CLI entry '$stagedCli' must be a regular file"
    }

    & $stagedCli --version | Out-Null
    if ($LASTEXITCODE -ne 0) {
      Stop-InstallFailure "downloaded CLI failed --version smoke test (exit code $LASTEXITCODE)"
    }

    Set-Content -LiteralPath ([System.IO.Path]::Combine($staging, '.install-complete')) -Value $Version -NoNewline

    $existingTarget = Get-Item -LiteralPath $targetDir -Force -ErrorAction SilentlyContinue
    if ($null -ne $existingTarget) { Remove-DirectorySafe $targetDir }
    Move-Item -LiteralPath $staging -Destination $targetDir
    return $targetDir
  } catch {
    $installError = $_
    $leftoverStage = Get-Item -LiteralPath $staging -Force -ErrorAction SilentlyContinue
    if ($null -ne $leftoverStage) { Remove-DirectorySafe $staging }
    throw $installError
  }
}

function ConvertTo-Utf8Hex {
  param([string]$Text)
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
  $hex = New-Object System.Text.StringBuilder
  foreach ($byte in $bytes) {
    [void]$hex.AppendFormat('{0:x2}', $byte)
  }
  return $hex.ToString()
}

function ConvertFrom-Utf8Hex {
  param([string]$Hex)
  if ([string]::IsNullOrWhiteSpace($Hex) -or (($Hex.Length % 2) -ne 0) -or ($Hex -notmatch '^[0-9A-Fa-f]+$')) {
    return $null
  }
  $byteCount = [int]($Hex.Length / 2)
  $bytes = New-Object byte[] $byteCount
  for ($idx = 0; $idx -lt $byteCount; $idx++) {
    $bytes[$idx] = [System.Convert]::ToByte($Hex.Substring($idx * 2, 2), 16)
  }
  return [System.Text.Encoding]::UTF8.GetString($bytes)
}

function ConvertTo-CmdWildcardText {
  param([string]$Text)
  # cmd.exe reads batch files in the console code page, so non-ASCII literals in
  # a UTF-8 shim are mojibake before command execution. Keeping the shim ASCII
  # and using '?' wildcards lets the Unicode-aware Win32 file matcher resolve
  # those path characters without writing their bytes into the batch file.
  $escaped = New-Object System.Text.StringBuilder
  foreach ($ch in $Text.ToCharArray()) {
    $code = [int][char]$ch
    if ($ch -eq '%') {
      [void]$escaped.Append('%%')
    } elseif ($code -ge 32 -and $code -le 126) {
      [void]$escaped.Append($ch)
    } else {
      [void]$escaped.Append('?')
    }
  }
  return $escaped.ToString()
}

function Get-ExistingShimTokenId {
  param([string]$InstalledDir)
  foreach ($token in (Get-ChildItem -LiteralPath $InstalledDir -Force -Filter '.shim-id-*' -ErrorAction SilentlyContinue)) {
    if ($token.PSIsContainer -or ($token.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { continue }
    if ($token.Name -notmatch '^\.shim-id-[0-9a-f]{32}$') { continue }
    $tokenId = $token.Name.Substring('.shim-id-'.Length)
    $storedId = Get-Content -LiteralPath $token.FullName -Raw -ErrorAction SilentlyContinue
    if ($null -ne $storedId -and $storedId.Trim() -eq $tokenId) { return $tokenId }
  }
  return $null
}

function Get-MeshCliShimLines {
  param(
    [string]$ResolvedCli,
    [string]$ShimId
  )
  if ($ShimId -notmatch '^[0-9a-f]{32}$') {
    throw "invalid shim id '$ShimId'"
  }
  $targetHex = ConvertTo-Utf8Hex -Text $ResolvedCli
  $lines = @(
    '@echo off',
    'rem agent-mesh shim',
    "rem agent-mesh-target-hex: $targetHex",
    "rem agent-mesh-shim-id: $ShimId",
    'setlocal EnableExtensions DisableDelayedExpansion'
  )

  $fullCli = [System.IO.Path]::GetFullPath($ResolvedCli)
  $root = [System.IO.Path]::GetPathRoot($fullCli)
  $relativeCli = $fullCli.Substring($root.Length)
  $segments = $relativeCli.Split([char[]]'\')
  $fileIndex = $segments.Length - 1

  # cmd/Win32 wildcard matching supports wildcards only in the leaf component,
  # and a trailing '?' may match zero characters. Nest one leaf wildcard search
  # per non-ASCII directory segment, but count a candidate only when the full
  # remaining path down to mesh.exe exists. Runtime variables carry the exact
  # Unicode path forward while every literal written into this file stays ASCII.
  $rootText = ConvertTo-CmdWildcardText -Text $root
  $baseExpression = $rootText.TrimEnd([char[]]'\')
  $pendingExact = ''
  $loopIndex = 0

  $lines += 'set "_MESH_N=0"'
  $lines += 'set "_MESH_CLI="'

  for ($idx = 0; $idx -lt $fileIndex; $idx++) {
    $segment = $segments[$idx]
    $segmentPattern = ConvertTo-CmdWildcardText -Text $segment
    $needsWildcard = $false
    foreach ($ch in $segment.ToCharArray()) {
      $code = [int][char]$ch
      if (($code -lt 32) -or ($code -gt 126)) {
        $needsWildcard = $true
        break
      }
    }

    if ($needsWildcard) {
      if ($loopIndex -ge 52) {
        throw "install path '$fullCli' is too deeply nested for the cmd shim"
      }
      $letterCode = if ($loopIndex -lt 26) { 65 + $loopIndex } else { 97 + ($loopIndex - 26) }
      $loopVar = [string]([char]$letterCode)
      $indent = '  ' * $loopIndex
      $pattern = $baseExpression + $pendingExact + '\' + $segmentPattern
      $lines += $indent + 'for /d %%' + $loopVar + ' in ("' + $pattern + '") do ('
      $baseExpression = '%%~f' + $loopVar
      $pendingExact = ''
      $loopIndex++
    } else {
      $pendingExact += '\' + $segmentPattern
    }
  }

  # The per-install random token is generated after any pre-existing decoys,
  # so only a payload staged by this installer can satisfy both existence checks.
  $filePattern = ConvertTo-CmdWildcardText -Text $segments[$fileIndex]
  $cliExpression = $baseExpression + $pendingExact + '\' + $filePattern
  $innerIndent = '  ' * $loopIndex
  $tokenExpression = $baseExpression + $pendingExact + '\.shim-id-' + $ShimId
  $lines += $innerIndent + 'if exist "' + $cliExpression + '" if exist "' + $tokenExpression + '" (set /a _MESH_N+=1 & set "_MESH_CLI=' + $cliExpression + '")'
  for ($closeIndex = $loopIndex - 1; $closeIndex -ge 0; $closeIndex--) {
    $lines += ('  ' * $closeIndex) + ')'
  }

  $lines += 'if not defined _MESH_CLI (echo agent-mesh: mesh.exe not found 1>&2& exit /b 1)'
  $lines += 'if %_MESH_N% GTR 1 (echo agent-mesh: ambiguous mesh.exe path 1>&2& exit /b 1)'
  $lines += '"%_MESH_CLI%" %*'
  $lines += 'exit /b %ERRORLEVEL%'
  return $lines
}

function Get-MeshCliShimContent {
  param(
    [string]$ResolvedCli,
    [string]$ShimId
  )
  $shimLines = Get-MeshCliShimLines -ResolvedCli $ResolvedCli -ShimId $ShimId
  return ($shimLines -join "`r`n") + "`r`n"
}

function Test-ManagedShim {
  param(
    [string]$ShimFilePath,
    [string]$VersionsBoundary
  )
  $shimBody = Get-Content -LiteralPath $ShimFilePath -Raw -Encoding UTF8
  if ([string]::IsNullOrWhiteSpace($shimBody)) { return $false }

  $rawLines = $shimBody.Replace("`r`n", "`n").Replace("`r", "`n").Split([char[]]"`n")
  $lastIndex = $rawLines.Length - 1
  while ($lastIndex -ge 0 -and [string]::IsNullOrWhiteSpace($rawLines[$lastIndex])) {
    $lastIndex--
  }
  if ($lastIndex -lt 1) { return $false }

  $lines = @()
  $untrimmedLines = @()
  for ($idx = 0; $idx -le $lastIndex; $idx++) {
    $lines += $rawLines[$idx].Trim()
    $untrimmedLines += $rawLines[$idx]
  }

  if (-not $lines[0].Equals('@echo off', [System.StringComparison]::OrdinalIgnoreCase)) {
    return $false
  }

  $rawTarget = $null
  if (($lines.Length -ge 9) -and $lines[1].Equals('rem agent-mesh shim', [System.StringComparison]::OrdinalIgnoreCase) -and $lines[2].StartsWith('rem agent-mesh-target-hex: ', [System.StringComparison]::OrdinalIgnoreCase)) {
    $targetPrefix = 'rem agent-mesh-target-hex: '
    if (-not $lines[2].StartsWith($targetPrefix, [System.StringComparison]::OrdinalIgnoreCase)) { return $false }
    $rawTarget = ConvertFrom-Utf8Hex -Hex $lines[2].Substring($targetPrefix.Length).Trim()
    if ($null -eq $rawTarget) { return $false }

    $idPrefix = 'rem agent-mesh-shim-id: '
    if (($lines.Length -lt 5) -or (-not $lines[3].StartsWith($idPrefix, [System.StringComparison]::OrdinalIgnoreCase))) { return $false }
    $shimId = $lines[3].Substring($idPrefix.Length).Trim()
    if ($shimId -notmatch '^[0-9a-f]{32}$') { return $false }

    try {
      $expectedLines = Get-MeshCliShimLines -ResolvedCli $rawTarget -ShimId $shimId
    } catch {
      return $false
    }
    if ($lines.Length -ne $expectedLines.Length) { return $false }
    for ($lineIdx = 0; $lineIdx -lt $lines.Length; $lineIdx++) {
      if (-not $untrimmedLines[$lineIdx].Equals($expectedLines[$lineIdx], [System.StringComparison]::OrdinalIgnoreCase)) { return $false }
    }
  } else {
    $commandLine = $null
    if ($lines.Length -eq 3 -and $lines[1].Equals('rem agent-mesh shim', [System.StringComparison]::OrdinalIgnoreCase)) {
      $commandLine = $lines[2]
    } elseif ($lines.Length -eq 2) {
      # Still recognize legacy Agent Mesh shims written before the marker line was added.
      $commandLine = $lines[1]
    } else {
      return $false
    }

    if (-not $commandLine.StartsWith('"')) { return $false }
    $closeQuote = $commandLine.IndexOf('"', 1)
    if ($closeQuote -le 1) { return $false }
    $rawTarget = $commandLine.Substring(1, $closeQuote - 1)
    $trailingArgs = $commandLine.Substring($closeQuote + 1).Trim()
    if ($trailingArgs -ne '%*') { return $false }
  }

  if ([string]::IsNullOrWhiteSpace($rawTarget)) {
    return $false
  }
  if (-not ($rawTarget -match '^[A-Za-z]:[\\/]' -or $rawTarget.StartsWith('\\'))) {
    return $false
  }

  try {
    $pointedCli = [System.IO.Path]::GetFullPath($rawTarget)
  } catch {
    return $false
  }
  if (-not $pointedCli.StartsWith($VersionsBoundary, [System.StringComparison]::OrdinalIgnoreCase)) {
    return $false
  }

  $relativeCli = $pointedCli.Substring($VersionsBoundary.Length)
  $segments = $relativeCli.Split([char[]]'\/')
  if ($segments.Length -ne 2) { return $false }
  $versionSeg = $segments[0]
  $leafName = $segments[1]
  if ([string]::IsNullOrWhiteSpace($versionSeg) -or $versionSeg -eq '.' -or $versionSeg -eq '..') {
    return $false
  }
  return ($leafName.Equals('mesh.exe', [System.StringComparison]::OrdinalIgnoreCase) -or
          $leafName.Equals('mesh.cmd', [System.StringComparison]::OrdinalIgnoreCase))
}

function Install-CliShim {
  param(
    [hashtable]$Config,
    [string]$InstalledDir,
    [string]$ShimContent
  )
  Write-InstallLog 'Installing mesh CLI shim...'
  [void][System.IO.Directory]::CreateDirectory($Config.ShimRoot)

  $existingExePath = [System.IO.Path]::Combine($Config.ShimRoot, 'mesh.exe')
  $existingExeInfo = Get-Item -LiteralPath $existingExePath -Force -ErrorAction SilentlyContinue
  if ($null -ne $existingExeInfo -or (Test-Path -LiteralPath $existingExePath)) {
    Stop-InstallFailure "a foreign mesh.exe already exists at $existingExePath; refusing to shadow or overwrite it"
  }

  $shim = [System.IO.Path]::Combine($Config.ShimRoot, 'mesh.cmd')
  $versionsBoundary = ConvertTo-MeshDirPrefix -Directory (Join-Path -Path $Config.InstallRoot -ChildPath 'versions')

  # Use Get-Item -Force so dangling symlinks or junctions are detected prior to writing.
  $shimItem = Get-Item -LiteralPath $shim -Force -ErrorAction SilentlyContinue
  $hasShimItem = ($null -ne $shimItem)
  if ($hasShimItem) {
    $isUnsafeTarget = $shimItem.PSIsContainer -or ($shimItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
    if ($isUnsafeTarget) {
      Stop-InstallFailure "a symlink, reparse point, or directory already exists at $shim; refusing to overwrite it"
    }
    if (-not (Test-ManagedShim -ShimFilePath $shim -VersionsBoundary $versionsBoundary)) {
      Stop-InstallFailure "another mesh command already exists at $shim; refusing to overwrite it"
    }
  } elseif (Test-Path -LiteralPath $shim) {
    Stop-InstallFailure "another mesh command already exists at $shim; refusing to overwrite it"
  }

  $asciiEncoding = New-Object System.Text.ASCIIEncoding
  [System.IO.File]::WriteAllText($shim, $ShimContent, $asciiEncoding)
  return $shim
}

function New-StartMenuShortcut {
  param(
    [hashtable]$Config,
    [string]$InstalledDir
  )
  if ($Config.SkipShortcut) { return }
  $desktopExe = [System.IO.Path]::Combine($InstalledDir, 'Agent Mesh.exe')
  if (-not (Test-Path -LiteralPath $desktopExe)) { return }

  try {
    if ([string]::IsNullOrWhiteSpace($env:APPDATA)) { return }
    $programsDir = [System.IO.Path]::Combine($env:APPDATA, 'Microsoft', 'Windows', 'Start Menu', 'Programs')
    if (-not (Test-Path -LiteralPath $programsDir)) { return }
    $wsh = New-Object -ComObject WScript.Shell
    $shortcut = $wsh.CreateShortcut([System.IO.Path]::Combine($programsDir, 'Agent Mesh.lnk'))
    $shortcut.TargetPath = $desktopExe
    $shortcut.WorkingDirectory = $InstalledDir
    $shortcut.IconLocation = "$desktopExe,0"
    $shortcut.Description = 'Agent Mesh Desktop'
    $shortcut.Save()
  } catch {
    # Non-fatal on headless or restricted environments.
  }
}

function Test-EdgeWebView2Present {
  $clientId = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
  $regLocations = @(
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$clientId",
    "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$clientId",
    "HKCU:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$clientId",
    "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$clientId"
  )
  $foundRuntime = $false
  foreach ($loc in $regLocations) {
    if (-not (Test-Path -LiteralPath $loc)) { continue }
    $props = Get-ItemProperty -LiteralPath $loc -Name 'pv' -ErrorAction SilentlyContinue
    if ($props -and -not [string]::IsNullOrWhiteSpace($props.pv) -and ($props.pv.Trim() -ne '0.0.0.0')) {
      $foundRuntime = $true
    }
  }
  return $foundRuntime
}

function Invoke-AgentMeshInstall {
  $archTag = Get-HostArchTag
  $cfg = Read-InstallerConfig

  Write-InstallLog 'Resolving Agent Mesh release version...'
  $version = Resolve-TargetVersion -Config $cfg
  $expectedInstallDir = [System.IO.Path]::Combine((Join-Path -Path $cfg.InstallRoot -ChildPath 'versions'), $version)
  $hadCompleteInstall = Test-VersionInstallComplete -TargetDir $expectedInstallDir -Version $version
  $installedDir = Install-VersionedRelease -Config $cfg -Version $version -ArchTag $archTag

  Write-InstallLog 'Verifying CLI shim...'
  $shimProbe = [System.IO.Path]::Combine($installedDir, 'mesh-shim-test.cmd')
  $shimProbeExit = 1
  try {
    $targetExe = [System.IO.Path]::Combine($installedDir, 'mesh.exe')
    $targetCmd = [System.IO.Path]::Combine($installedDir, 'mesh.cmd')
    $resolvedCli = if (Test-Path -LiteralPath $targetExe) { $targetExe } else { $targetCmd }
    $asciiEncoding = New-Object System.Text.ASCIIEncoding
    $shimId = Get-ExistingShimTokenId -InstalledDir $installedDir
    $createdShimToken = $false
    if ($null -eq $shimId) {
      $shimId = [System.Guid]::NewGuid().ToString('N')
      $createdShimToken = $true
    }
    $shimTokenPath = [System.IO.Path]::Combine($installedDir, '.shim-id-' + $shimId)
    if ($createdShimToken) {
      [System.IO.File]::WriteAllText($shimTokenPath, $shimId, $asciiEncoding)
    }
    $shimContent = Get-MeshCliShimContent -ResolvedCli $resolvedCli -ShimId $shimId
    [System.IO.File]::WriteAllText($shimProbe, $shimContent, $asciiEncoding)
    & $shimProbe --version | Out-Null
    $shimProbeExit = $LASTEXITCODE
  } catch {
    $shimProbeExit = 1
  }
  Remove-Item -LiteralPath $shimProbe -Force -ErrorAction SilentlyContinue
  if ($shimProbeExit -ne 0) {
    if ($createdShimToken) {
      Remove-Item -LiteralPath $shimTokenPath -Force -ErrorAction SilentlyContinue
    }
    if (-not $hadCompleteInstall) {
      Remove-DirectorySafe $installedDir
    }
    Stop-InstallFailure "installed CLI shim failed --version smoke test (exit code $shimProbeExit)"
  }

  $shimPath = Install-CliShim -Config $cfg -InstalledDir $installedDir -ShimContent $shimContent

  $pathUpdated = Add-UserPathEntry -EntryDir $cfg.ShimRoot -Skip $cfg.SkipUserPath
  New-StartMenuShortcut -Config $cfg -InstalledDir $installedDir

  if (-not (Test-EdgeWebView2Present)) {
    Write-Warning 'Microsoft Edge WebView2 Runtime was not detected. Agent Mesh Desktop requires WebView2: https://go.microsoft.com/fwlink/p/?LinkId=2124703'
  }

  Write-Host ''
  Write-Host "  Agent Mesh $version installed to: $installedDir"
  Write-Host "  CLI shim: $shimPath"
  if ($pathUpdated) {
    Write-Host "  Added $($cfg.ShimRoot) to your user PATH (open a new shell or run 'mesh --version' now)."
  }
  Write-Host "  Launch Desktop UI: & '$([System.IO.Path]::Combine($installedDir, 'Agent Mesh.exe'))'"
  Write-Host '  User data directory (%USERPROFILE%\.agent-mesh) is untouched.'
  Write-Host ''
}

Invoke-AgentMeshInstall
