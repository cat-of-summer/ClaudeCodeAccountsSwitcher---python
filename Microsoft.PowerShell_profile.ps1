# ===== Claude multi-account =====
$script:ClaudeExe       = (Get-Command "claude.exe" -CommandType Application).Source
$script:ClaudeConfigDir = Join-Path $HOME ".claude"
$script:ClaudeJson      = Join-Path $HOME ".claude.json"
$script:ClaudeCredFile  = Join-Path $script:ClaudeConfigDir ".credentials.json"
$script:ClaudeStore     = Join-Path $HOME ".claude-accounts"

function script:CC-CredPath([int]$n)     { Join-Path $script:ClaudeStore "account-$n.credentials.json" }
function script:CC-IdentityPath([int]$n) { Join-Path $script:ClaudeStore "account-$n.identity.json" }
function script:CC-ActivePath()          { Join-Path $script:ClaudeStore ".active" }

function script:CC-WriteJsonNoBom($path, $obj) {
    $json = $obj | ConvertTo-Json -Depth 100
    $enc  = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, $json, $enc)
}

function script:CC-EnsureStore {
    if (-not (Test-Path $script:ClaudeStore)) {
        New-Item -ItemType Directory -Path $script:ClaudeStore -Force | Out-Null
    }
}

function script:CC-GetActive {
    $p = script:CC-ActivePath
    if (Test-Path $p) {
        $v = (Get-Content $p -Raw).Trim()
        if ($v -match '^\d+$') { return [int]$v }
    }
    return 0
}

function script:CC-SetActive([int]$n) {
    script:CC-EnsureStore
    Set-Content -Path (script:CC-ActivePath) -Value "$n" -Encoding ascii
}

function script:CC-Save([int]$n) {
    script:CC-EnsureStore
    if (Test-Path $script:ClaudeCredFile) {
        Copy-Item $script:ClaudeCredFile (script:CC-CredPath $n) -Force
    }
    if (Test-Path $script:ClaudeJson) {
        $j = Get-Content $script:ClaudeJson -Raw | ConvertFrom-Json
        $identity = [ordered]@{ oauthAccount = $j.oauthAccount; userID = $j.userID }
        script:CC-WriteJsonNoBom (script:CC-IdentityPath $n) $identity
    }
}

function script:CC-Restore([int]$n) {
    $cred = script:CC-CredPath $n
    $idn  = script:CC-IdentityPath $n
    if (Test-Path $cred) { Copy-Item $cred $script:ClaudeCredFile -Force }
    if ((Test-Path $idn) -and (Test-Path $script:ClaudeJson)) {
        $bak = "$($script:ClaudeJson).bak"
        if (-not (Test-Path $bak)) { Copy-Item $script:ClaudeJson $bak -Force }
        $j  = Get-Content $script:ClaudeJson -Raw | ConvertFrom-Json
        $id = Get-Content $idn -Raw | ConvertFrom-Json
        $j.oauthAccount = $id.oauthAccount
        if ($j.PSObject.Properties.Name -contains 'userID') { $j.userID = $id.userID }
        else { $j | Add-Member -NotePropertyName userID -NotePropertyValue $id.userID }
        script:CC-WriteJsonNoBom $script:ClaudeJson $j
    }
}

function script:CC-Clear {
    if (Test-Path $script:ClaudeCredFile) { Remove-Item $script:ClaudeCredFile -Force }
    if (Test-Path $script:ClaudeJson) {
        $bak = "$($script:ClaudeJson).bak"
        if (-not (Test-Path $bak)) { Copy-Item $script:ClaudeJson $bak -Force }
        $j = Get-Content $script:ClaudeJson -Raw | ConvertFrom-Json
        if ($j.PSObject.Properties.Name -contains 'oauthAccount') { $j.oauthAccount = $null }
        script:CC-WriteJsonNoBom $script:ClaudeJson $j
    }
}

function claude {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    if ($null -eq $Arguments) { $Arguments = @() }
    $active = script:CC-GetActive

    if ($active -eq 0 -and (Test-Path $script:ClaudeCredFile)) {
        script:CC-Save 1
        script:CC-SetActive 1
        $active = 1
    }

    $target = $active
    if ($Arguments.Count -ge 1 -and $Arguments[0] -match '^\d+$') {
        $target = [int]$Arguments[0]
        if ($Arguments.Count -gt 1) { $Arguments = $Arguments[1..($Arguments.Count - 1)] }
        else { $Arguments = @() }
    }
    if ($target -le 0) { $target = 1 }

    if ($target -ne $active) {
        if ($active -gt 0) { script:CC-Save $active }

        if (Test-Path (script:CC-CredPath $target)) {
            script:CC-Restore $target
            script:CC-SetActive $target
        }
        else {
            Write-Host "Аккаунт $target ещё не сохранён — выполни вход." -ForegroundColor Yellow
            script:CC-Clear
            script:CC-SetActive $target
            & $script:ClaudeExe --dangerously-skip-permissions @Arguments
            script:CC-Save $target
            Write-Host "Аккаунт $target сохранён." -ForegroundColor Green
            return
        }
    }

    & $script:ClaudeExe --dangerously-skip-permissions @Arguments
    script:CC-Save $target
}

function claude-accounts {
    $active = script:CC-GetActive
    if (-not (Test-Path $script:ClaudeStore)) { Write-Host "Слотов пока нет."; return }
    Get-ChildItem $script:ClaudeStore -Filter "account-*.identity.json" | ForEach-Object {
        if ($_.Name -match 'account-(\d+)\.identity\.json') {
            $n = [int]$Matches[1]
            $email = "?"
            try { $email = (Get-Content $_.FullName -Raw | ConvertFrom-Json).oauthAccount.emailAddress } catch {}
            $mark = if ($n -eq $active) { "* " } else { "  " }
            Write-Host ("{0}[{1}] {2}" -f $mark, $n, $email)
        }
    }
}
