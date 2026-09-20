<!-- DOCGEN:START -->
# daemonlink.py
<!-- DOCGEN:END -->

## Назначение

Клиентская сторона локального API демона: `register` (POST /route), `poll` (GET /poll, long-poll), `unregister`, `status`, `stop`, плюс `read_daemon()` — живая запись `daemon.json`. Вынесено из `app/daemon.py`, потому что сессия импортирует это, а демон импортирует сессию.
