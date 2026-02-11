param(
    [Parameter(Mandatory = $false, HelpMessage = 'Specify IP or IP:PORT to force connecting to a specific device')]
    [string]$Device,

    [Parameter(Mandatory = $false, HelpMessage = 'Pair with the device before connecting, e.g. 10.1.1.242:39191')]
    [string]$PairingAddress,

    [Parameter(Mandatory = $false, HelpMessage = 'Pairing port when the IP is provided without a port')]
    [int]$PairingPort,

    [Parameter(Mandatory = $false, HelpMessage = 'Pairing code shown on the device (required when Pairing address is provided)')]
    [string]$PairingCode,

    [switch]$AllowMultiple,

    [switch]$Forget,

    [switch]$VerboseLog
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($Forget) {
    if (-not $Device) {
        throw 'You must specify -Device when using -Forget to indicate which host to remove.'
    }

    if ($PairingAddress -or $PairingPort -or $PairingCode) {
        throw 'The -Forget switch cannot be combined with pairing options.'
    }

    if ($AllowMultiple) {
        throw 'The -Forget switch cannot be combined with -AllowMultiple.'
    }
}

function Invoke-DockerCompose {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [switch]$VerboseLog
    )

    $composeCommand = $null
    $commandArguments = $Arguments

    $dockerCommand = Get-Command -Name 'docker' -ErrorAction SilentlyContinue
    if ($dockerCommand) {
        & docker @('compose', 'version') *> $null
        if ($LASTEXITCODE -eq 0) {
            $composeCommand = 'docker'
            $commandArguments = @('compose') + $Arguments
        }
    }

    if (-not $composeCommand) {
        throw 'Neither "docker compose" nor "docker-compose" is available on PATH. Install Docker with Compose support.'
    }

    if ($VerboseLog) {
        Write-Host "Using compose command: $composeCommand $($commandArguments -join ' ')" -ForegroundColor Yellow
    }

    & $composeCommand @commandArguments
}

$scriptRoot = $PSScriptRoot
if (-not $scriptRoot) {
    throw 'PSScriptRoot is not available. Please run this script from a file, not via stdin.'
}
$repoRoot = Split-Path -Parent $scriptRoot
if (-not (Test-Path -LiteralPath $repoRoot)) {
    throw "Repository root '$repoRoot' not found."
}

$pairTarget = $null
if (-not $Forget -and ($PairingAddress -or $PairingPort -or $PairingCode)) {
    if (-not $PairingAddress) {
        throw 'You must specify -PairingAddress when using pairing options (e.g. 10.1.1.242 or 10.1.1.242:39191).'
    }

    if (-not $PairingCode) {
        throw 'You must specify -PairingCode together with -PairingAddress to pair automatically.'
    }

    $pairTarget = $PairingAddress
    if ($PairingPort -and ($PairingAddress -notmatch ':')) {
        $pairTarget = "$PairingAddress`:$PairingPort"
    }

    if ($PairingPort -and ($PairingAddress -match ':')) {
        Write-Warning 'Ignoring -PairingPort because -PairingAddress already contains a port.'
    }
}

Push-Location -LiteralPath $repoRoot
try {
    $innerScript = @'
set -eu
set -o pipefail 2>/dev/null || true

device_filter="${DEVICE_FILTER:-}"
allow_multiple="${ALLOW_MULTIPLE:-0}"
pair_target="${PAIR_TARGET:-}"
pair_code="${PAIR_CODE:-}"
pair_success_target=""
forget_mode="${FORGET_MODE:-0}"
forget_target="${FORGET_TARGET:-}"

find_known_hosts_file() {
  local fallback="$HOME/.android/adb_known_hosts"
  local candidates=("$HOME/.android/adb_known_hosts" "$HOME/.android/adb/adb_known_hosts")
  local candidate=""

  for candidate in "${candidates[@]}"; do
    if [ -f "$candidate" ]; then
      printf '%s' "$candidate"
      return 0
    fi
  done

  printf '%s' "$fallback"
  return 1
}

if [ "$forget_mode" = "1" ]; then
  if [ -z "$forget_target" ]; then
    echo "[error] Missing target to forget." >&2
    exit 1
  fi

  echo "[info] Forgetting $forget_target" >&2
  if ! adb disconnect "$forget_target" >/dev/null 2>&1; then
    echo "[warn] Unable to disconnect $forget_target (it may already be disconnected)." >&2
  fi

  if ! known_hosts_file=$(find_known_hosts_file); then
    echo "[warn] adb_known_hosts file not found. Nothing to remove." >&2
    exit 0
  fi


  tmp_file=$(mktemp "${TMPDIR:-/tmp}/adb_known_hosts.XXXXXX")
  cleanup_tmp() {
    [ -f "$tmp_file" ] && rm -f "$tmp_file"
  }
  trap cleanup_tmp EXIT

  if printf '%s' "$forget_target" | grep -q ':'; then
    awk -v target="$forget_target" '$1 != target {print $0}' "$known_hosts_file" >"$tmp_file"
  else
    awk -v host="$forget_target" '$1 !~ ("^" host ":[0-9]+$") {print $0}' "$known_hosts_file" >"$tmp_file"
  fi

  if cmp -s "$tmp_file" "$known_hosts_file"; then
    echo "[warn] No known host entries matched $forget_target" >&2
    exit 0
  fi

  mv "$tmp_file" "$known_hosts_file"
  trap - EXIT
  rm -f "$tmp_file" 2>/dev/null || true
  echo "[info] Removed known host entries for $forget_target" >&2
  exit 0
fi

add_fallback() {
  local value="$1"
  [ -n "$value" ] || return 0
  if [ -z "$fallback_candidates" ]; then
    fallback_candidates="$value"
  else
    fallback_candidates=$(printf '%s\n%s\n' "$fallback_candidates" "$value")
  fi
}

if [ -n "$pair_target" ]; then
  if [ -z "$pair_code" ]; then
    echo "[error] Pairing code is required when using a Pairing address" >&2
    exit 1
  fi

  echo "[info] Pairing with $pair_target" >&2
  if ! pair_output=$(adb pair "$pair_target" "$pair_code" 2>&1); then
    printf '%s\n' "$pair_output" >&2
    echo "[error] Pairing failed. Check the code and port and try again" >&2
    exit 1
  fi
  printf '%s\n' "$pair_output" >&2

  pair_success_target=$(printf '%s\n' "$pair_output" | awk '/Successfully paired to / {print $4}' | tail -n 1)
  if [ -z "$pair_success_target" ]; then
    pair_success_target="$pair_target"
  fi
fi

  mdns_raw=$(adb mdns services 2>/dev/null | awk '/_adb-tls-connect._tcp\./ {print $NF}' || true)

  if [ -n "$mdns_raw" ]; then
    # Normalise and deduplicate host:port values discovered via mDNS
    mdns_candidates=$(printf '%s\n' "$mdns_raw" | grep -E ':[0-9]+$' | sort -u || true)
  else
    mdns_candidates=""
  fi

  if [ -n "$device_filter" ]; then
    if printf '%s' "$device_filter" | grep -q ':'; then
      # Filter to the exact host:port when provided
      mdns_candidates=$(printf '%s\n' "$mdns_candidates" | grep -F "$device_filter" || true)
    else
      # Filter to any host that matches the provided IP
      mdns_candidates=$(printf '%s\n' "$mdns_candidates" | grep -F "$device_filter:" || true)
    fi
  fi

if [ -z "$mdns_candidates" ]; then
  fallback_candidates=""

  if [ -n "$device_filter" ]; then
    add_fallback "$device_filter"
  fi

  if [ -n "$pair_success_target" ]; then
    echo "[warn] Falling back to the most recently paired target ($pair_success_target)." >&2
    add_fallback "$pair_success_target"
  fi

  if known_hosts_file=$(find_known_hosts_file); then
    if [ -n "$device_filter" ]; then
      if printf '%s' "$device_filter" | grep -q ':'; then
        known_host_candidates=$(awk '{print $1}' "$known_hosts_file" | grep -F "$device_filter" || true)
      else
        known_host_candidates=$(awk '{print $1}' "$known_hosts_file" | grep -E "^$device_filter:[0-9]+$" || true)
      fi
    else
      known_host_candidates=$(awk '{print $1}' "$known_hosts_file" | grep -E ':[0-9]+$' || true)
    fi

    if [ -n "$known_host_candidates" ]; then
      echo "[warn] Using adb_known_hosts history instead of mDNS." >&2
      add_fallback "$known_host_candidates"
    fi
  fi

  if [ -n "$fallback_candidates" ]; then
    fallback_candidates=$(printf '%s\n' "$fallback_candidates" | grep -v '^$' | sort -u)
    mdns_candidates="$fallback_candidates"
  fi
fi

if [ -z "$mdns_candidates" ] && [ -n "$device_filter" ]; then
  # Fall back to the provided device if mDNS lookup did not find it
  mdns_candidates="$device_filter"
fi

if [ -z "$mdns_candidates" ]; then
  echo "[error] No wireless devices found via mDNS (_adb-tls-connect)." >&2
  echo "[hint] Ensure Wireless debugging is enabled or provide -Device <IP:PORT>" >&2
  exit 1
fi

connected=0
for target in $mdns_candidates; do
  [ -n "$target" ] || continue
  if retry_connect "$target"; then
    connected=$((connected + 1))
    if [ "$allow_multiple" != "1" ]; then
      break
    fi
  else
    echo "[warn] Failed to connect to $target" >&2
  fi
done

if [ "$connected" -eq 0 ]; then
  echo "[error] Unable to connect to any devices" >&2
  exit 1
fi

echo "[info] Current device list:" >&2
adb devices -l
'@

    $innerScript = $innerScript.Replace("`r`n", "`n").Replace("`r", "`n")

function Get-RetryShellScript {
    $retryScript = @'
retry_connect() {
  local candidate="$1"
  local attempt=1
  local host=""
  while [ $attempt -le 3 ]; do
    if [ $attempt -eq 1 ]; then
      echo "[info] Attempting to connect to $candidate" >&2
    else
      echo "[info] Attempting to connect to $candidate (retry $attempt)" >&2
    fi

    if adb connect "$candidate"; then
      return 0
    fi

    if [ $attempt -lt 3 ]; then
      echo "[warn] Failed to connect to $candidate (attempt $attempt). Cleaning up and retrying..." >&2
      adb disconnect "$candidate" >/dev/null 2>&1 || true

      if printf '%s' "$candidate" | grep -q ':'; then
        host="${candidate%%:*}"
        adb disconnect "$host" >/dev/null 2>&1 || true
      else
        host=""
      fi

      if [ -n "$host" ]; then
        adb devices | awk '$2 == "offline" {print $1}' | while IFS= read -r offline_dev; do
          if [ "$offline_dev" = "$candidate" ] || printf '%s\n' "$offline_dev" | grep -q "^$host:"; then
            adb disconnect "$offline_dev" >/dev/null 2>&1 || true
          fi
        done
      else
        adb devices | awk '$2 == "offline" {print $1}' | while IFS= read -r offline_dev; do
          if [ "$offline_dev" = "$candidate" ]; then
            adb disconnect "$offline_dev" >/dev/null 2>&1 || true
          fi
        done
      fi

      sleep 1
    fi

    attempt=$((attempt + 1))
  done

  echo "[error] Failed to connect to $candidate after multiple attempts" >&2
  return 1
}
'@

    return $retryScript.Replace("`r`n", "`n").Replace("`r", "`n")
}

$retryFunction = Get-RetryShellScript
$innerScript = $retryFunction + "`n" + $innerScript
$innerScriptBase64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($innerScript))

    $envArguments = @('--env', "ADB_INNER_SCRIPT=$innerScriptBase64")

    if ($Device -and -not $Forget) {
        $envArguments += @('--env', "DEVICE_FILTER=$Device")
    }

    if ($pairTarget) {
        $envArguments += @('--env', "PAIR_TARGET=$pairTarget")
        $envArguments += @('--env', "PAIR_CODE=$PairingCode")
    }

    if ($AllowMultiple) {
        $envArguments += @('--env', 'ALLOW_MULTIPLE=1')
    }

    if ($Forget) {
        $envArguments += @('--env', 'FORGET_MODE=1')
        $envArguments += @('--env', "FORGET_TARGET=$Device")
    }

    $composeArgs = @('exec', '-T') + $envArguments + @('controller', 'bash', '-lc', 'printf ''%s'' "$ADB_INNER_SCRIPT" | base64 -d | bash')

    Invoke-DockerCompose -Arguments $composeArgs -VerboseLog:$VerboseLog
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Docker Compose command exited with code $exitCode"
    }
}
finally {
    Pop-Location
}