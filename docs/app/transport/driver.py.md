<!-- DOCGEN:START -->
# driver.py
<!-- DOCGEN:END -->

## Назначение

Один claude, управляемый по stream-json так же, как его управляет Agent SDK. Интерактивный TUI официально недоступен для автоматизации; это единственная поддерживаемая дорога, и она проверена на claude 2.1.278.

## Командная строка

`claude -p --input-format stream-json --output-format stream-json --verbose --permission-prompt-tool stdio --session-id <uuid> [--name <алиас>] <defaultArgs> <args>`; при перезапуске — `--resume <uuid>` вместо `--session-id`/`--name`. Шина хуков подключается тем же `hookbus.prepare_args`, что и у интерактивной сессии.

Ключевое — `--permission-prompt-tool stdio`: без него в `-p` инструмент `AskUserQuestion` модели не выдаётся вовсе. С ним claude присылает `control_request` с `subtype: can_use_tool` на каждый запрос разрешения и каждый `AskUserQuestion` (`requires_user_interaction: true`), даже при `--dangerously-skip-permissions`, и ждёт `control_response`:

```json
{"type":"control_response","response":{"subtype":"success","request_id":"…","response":{"behavior":"allow","updatedInput":{"questions":[…],"answers":{"Вопрос?":"Ответ"}}}}}
```

Отказ — `{"behavior":"deny","message":"…"}`. После старта драйвер шлёт `initialize` — в ответе список slash-команд сессии.

## События

Поток stdout разбирается построчно в `Event(kind, data)`: `init`, `text`, `tool_use`, `tool_result`, `result` (с `api_error_status`, `terminal_reason`), `ask`, `cancel`, `rate_limit` (`rate_limit_event` claude: статус, окно, `resetsAt`), `exit`. Слушатель получает их из потока чтения; сессия складывает в свою очередь.

## Управление

`send_user(text)` — ход; `interrupt()`, `set_model()`, `set_permission_mode()` — control-запросы с ожиданием ответа; `close()` закрывает stdin и ждёт, потом `wrapper.terminate`.
