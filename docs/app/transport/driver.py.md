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

## Запрос MCP-сервера (`elicitation`)

Сервер MCP может спросить человека сам — так реестр спрашивает про доступ к проекту. Приходит это отдельным субтипом:

```json
{"type":"control_request","request_id":"…","request":{"subtype":"elicitation","mcp_server_name":"registry","message":"Дать агенту доступ к проекту «adzhubey» до конца сессии?","mode":"form","requested_schema":{"type":"object","properties":{"approve":{"type":"boolean","title":"Разрешить?"}}}}}
```

Ответ — `{"action":"accept"|"decline"|"cancel","content":{…}}`, где `content` заполняет `requested_schema`. Поля `mode: "url"`, `url`, `title`, `display_name`, `description` необязательны.

> [!warning]
> Ответ вида `{"subtype":"error"}` claude **отбрасывает** и продолжает ждать («not a human choice; dialog stays parked»). Пока запрос не отвечен, вызов инструмента не возвращается и ход стоит намертво — именно так сессия и зависала, пока драйвер отвечал на неизвестный субтип ошибкой. Поэтому неизвестные субтипы теперь ещё и пишутся в журнал.

Есть и субтип `request_user_dialog`, но он приходит, только если клиент объявил `supportedDialogKinds` в `initialize`; ccas не объявляет ничего, поэтому такие запросы не приходят.

## События

Поток stdout разбирается построчно в `Event(kind, data)`: `init`, `text`, `tool_use`, `tool_result`, `result` (с `api_error_status`, `terminal_reason`), `ask`, `elicit`, `cancel`, `rate_limit` (`rate_limit_event` claude: статус, окно, `resetsAt`), `exit`. Слушатель получает их из потока чтения; сессия складывает в свою очередь.

## Управление

`send_user(text)` — ход; `interrupt()`, `set_model()`, `set_permission_mode()` — control-запросы с ожиданием ответа; `close()` закрывает stdin и ждёт, потом `wrapper.terminate`.
