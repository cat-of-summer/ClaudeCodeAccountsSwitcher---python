<!-- DOCGEN:START -->
# shell.py
<!-- DOCGEN:END -->

## Назначение

Выполнить строку `!cmd` из чата в той оболочке, которую ожидает человек за этой машиной. `subprocess.run(shell=True)` на Windows означает cmd.exe: `pwd` — «не является внутренней или внешней командой», `ls -la` не работает, а сообщения приходят в OEM-кодировке (cp866 на русской системе) и в чате превращаются в кракозябры.

## Как

| Платформа | Оболочка | Вызов |
|---|---|---|
| Windows | `pwsh`, если стоит, иначе `powershell.exe` | `-NoProfile -NonInteractive -Command "[Console]::OutputEncoding = UTF8; <cmd>"` |
| POSIX | `bash` | `bash -c "<cmd>"` |
| ни того, ни другого нет | оболочка ОС по умолчанию (`shell=True`) | как было |

Преамбула про `OutputEncoding` нужна Windows PowerShell 5.1: без неё вывод cmdlet'ов в канал идёт в кодировке консоли. Нативные программы, вызванные из PowerShell, пишут что хотят, поэтому `decode()` читает байты как UTF-8, а если они не UTF-8 — как OEM-кодовую страницу (`GetOEMCP`), и только на совсем чужие байты ставит `U+FFFD`.

`run(command, cwd=…, timeout=…)` возвращает `Completed(stdout, stderr, returncode)` уже строками; `TimeoutExpired` и `OSError` наружу — вызывающий (`Conversation._shell`) сам решает, что показать.

> [!note]
> `pwd` в PowerShell печатает таблицу с заголовком `Path`, а не голую строку — это его формат, не ошибка. Голый путь — `(Get-Location).Path`.
