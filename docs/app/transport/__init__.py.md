<!-- DOCGEN:START -->
# __init__.py
<!-- DOCGEN:END -->

## Назначение

Вход из обёртки. `wrapper.main` снимает флаги ccas (`routing.split_launch_options`) и при `-t telegram` зовёт `transport.run`: токен и чат — из флагов, иначе из `config.telegram`; каталог — `-C` или текущий; окружение `CCAS_TELEGRAM_FEED`/`CCAS_TELEGRAM_TAG` от демона говорит сессии не опрашивать бота самой.
