# Atalho: roda o agente DENTRO do container, no mesmo banco da tela web.
#   .\agente.ps1 run                 (ensaio, nada e enviado)
#   .\agente.ps1 run --enviar        (envio real)
#   .\agente.ps1 aprovar
#   .\agente.ps1 testar-email --para voce@exemplo.com
# Sem terminal interativo (Agendador de Tarefas), o -T e adicionado sozinho.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (docker compose ps --status running --services 2>$null | Select-String "recebiveis")) {
    docker compose up -d | Out-Null
    Start-Sleep -Seconds 6
}
[string[]]$tty = @()
if ([Console]::IsInputRedirected) { $tty = @("-T") }
docker compose exec @tty recebiveis python -W ignore -m recebiveis @args
exit $LASTEXITCODE
