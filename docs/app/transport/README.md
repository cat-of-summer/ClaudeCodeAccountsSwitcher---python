<!-- DOCGEN:START -->
# transport

## Файлы

- [conversation.py](conversation.py.md)
- [daemonlink.py](daemonlink.py.md)
- [driver.py](driver.py.md)
- [poller.py](poller.py.md)
- [profiles.py](profiles.py.md)
- [prompter.py](prompter.py.md)
- [routing.py](routing.py.md)
- [transport.py](transport.py.md)

<!-- DOCGEN:END -->

## Папка

Telegram-транспорт: сессия claude, живущая в чате.

- [profiles.py](profiles.py.md) — профиль: чаты, каталог, слот, флаги
- [transport.py](transport.py.md) — процесс профиля и разговоры внутри него
- [conversation.py](conversation.py.md) — один claude и один собеседник
- [driver.py](driver.py.md) — headless claude по stream-json, control-протокол SDK
- [prompter.py](prompter.py.md) — вопросы и разрешения как кнопки
- [routing.py](routing.py.md) — кому строка: адрес `/алиас`, разбор `/claude` и команд сессии
- [poller.py](poller.py.md) — поллер токена и его лок
- [../../system/childjob.py](../../system/childjob.py.md) — дети, умирающие вместе с окном
- [daemonlink.py](daemonlink.py.md) — как сессия и CLI говорят с демоном
- [__init__.py](__init__.py.md) — вход из обёртки: `claude -t telegram`
