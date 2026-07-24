<!-- DOCGEN:START -->
# claudecfg.py
<!-- DOCGEN:END -->

## Назначение

Поиск и правка конфигурации самого Claude Code: где лежит `claude.json`, где креды, как подменить identity, откуда взять кеш лимитов.

## Разрешение путей

Логика повторяет то, что делает claude v2.1.218. Из бинарника:

```js
// файл кредов
function KK(){ let e = process.env.CLAUDE_SECURESTORAGE_CONFIG_DIR;
  if (e !== void 0) return (e || join(homedir(), ".claude")).normalize("NFC");
  return cn(); }                        // -> join(KK(), ".credentials.json")

// claude.json
rv = () => { if (existsSync(join(cn(), ".config.json")))
               return join(cn(), ".config.json");
             return join(process.env.CLAUDE_CONFIG_DIR || homedir(),
                         `.claude${suffix}.json`) }
```

Две детали, на которых легко ошибиться:

* **`.config.json` внутри каталога конфига важнее `~/.claude.json`.** Старый PowerShell-профиль знал только про второй, поэтому очередная миграция Claude Code сломала бы его молча.
* **Ветка fallback склеивает `CLAUDE_CONFIG_DIR || homedir()`, а не `cn()`.** При незаданной переменной это `~/.claude.json`, а вовсе не `~/.claude/.claude.json`.

## Почему целей несколько

На машине, где всё это разрабатывалось, одновременно существовали:

* `~/.claude.json` — 1 МБ, живой, активный;
* `~/.claude/.claude.json` — 979 байт, инертный остаток незавершённой миграции, причём с **другим** аккаунтом внутри.

Поэтому `read_identity` читает из самой свежей по `mtime` цели, а `patch_identity` пишет во **все** существующие. Разъехаться снова они не могут.

## Экономия записи

`patch_identity` пропускает цель, в которой нужные значения уже стоят: ни бекапа, ни перезаписи. Без этого повторный запуск того же слота вхолостую перемалывал бы мегабайт истории проектов на каждый старт.

## Состояние токена

`token_state` возвращает одно из четырёх:

| Значение | Смысл |
|---|---|
| `ok` | всё в порядке |
| `expired` | access-токен истёк, но refresh жив — claude обновит сам |
| `stale` | истёк и refresh — нужен полноценный вход |
| `missing` | файла кредов нет |

## Кеш лимитов

`read_usage_cache` достаёт блок `cachedUsageUtilization`, который claude сам оставляет в конфиге:

```json
{"fetchedAtMs": 1784889548738, "accountUuid": "...",
 "utilization": {"five_hour": {"utilization": 46,
                               "resets_at": "2026-07-24T13:50:00+00:00"},
                 "seven_day": {...}}}
```

Блок помечен `accountUuid`, поэтому снимок, принадлежащий другому слоту, легко отбросить.
