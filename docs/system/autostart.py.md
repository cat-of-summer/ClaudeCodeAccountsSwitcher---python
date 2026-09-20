<!-- DOCGEN:START -->
# autostart.py
<!-- DOCGEN:END -->

## Назначение

Запуск демона при входе в систему, одной записью на платформу: Run-ключ `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` (`ccas-daemon`), systemd user unit `~/.config/systemd/user/ccas-daemon.service` (или XDG autostart `.desktop`, если пользовательского systemd нет), launchd-агент `~/Library/LaunchAgents/dev.ccas.daemon.plist`. Команда везде одна: `ccas daemon run --hidden`.

Регистрируется и снимается вместе с настройкой `telegram-daemon` (`core/settings._set_daemon`) и при `ccas uninstall`.
