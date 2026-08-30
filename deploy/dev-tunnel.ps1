<#
.SYNOPSIS
    Пробрасывает дев-стенд с app-01 на localhost этой машины.

.DESCRIPTION
    Дев-стенд опубликован на сервере как 127.0.0.1:8002 и наружу не смотрит:
    домена, TLS и записи в хостовом nginx у него нет. Единственный вход —
    SSH-туннель; пускает внутрь ваш обычный ключ, поэтому смена внешнего IP
    (в том числе включённый VPN) ничего не ломает.

    Открывает http://localhost:8002 — DEV-пульт лежит на /dev.

.EXAMPLE
    ./deploy/dev-tunnel.ps1
    ./deploy/dev-tunnel.ps1 -LocalPort 8010
#>
param(
    # Хост из ~/.ssh/config либо root@201.34.132.125.
    [string]$Server = "app01",
    # Локальный порт. Меняйте, если 8002 уже занят чем-то своим.
    [int]$LocalPort = 8002
)

Write-Host "Туннель: localhost:$LocalPort -> ${Server}:8002 (127.0.0.1 на сервере)"
Write-Host "Пульт:   http://localhost:$LocalPort/dev"
Write-Host "Ctrl+C   закрывает туннель."
Write-Host ""

# ExitOnForwardFailure: без него ssh при занятом локальном порте молча
# подключается без проброса, и браузер открывает что угодно, кроме стенда.
ssh -N -o ExitOnForwardFailure=yes -L "${LocalPort}:127.0.0.1:8002" $Server
