<!-- DOCGEN:START -->
# transport
<!-- DOCGEN:END -->

## Папка

Telegram-транспорт: сессия claude, живущая в чате.

- [driver.py](driver.py.md) — headless claude по stream-json, control-протокол SDK
- [prompter.py](prompter.py.md) — вопросы и разрешения как кнопки
- [session.py](session.py.md) — одна сессия в одном чате: рендер, команды, локальная консоль
- [routing.py](routing.py.md) — разбор `/claude`, префикса и алиасов
- [poller.py](poller.py.md) — поллер токена и его лок
- [daemonlink.py](daemonlink.py.md) — как сессия и CLI говорят с демоном
- [__init__.py](__init__.py.md) — вход из обёртки: `claude -t telegram`
