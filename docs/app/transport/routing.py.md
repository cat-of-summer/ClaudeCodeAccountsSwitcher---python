<!-- DOCGEN:START -->
# routing.py
<!-- DOCGEN:END -->

## Назначение

Чистые функции разбора, общие для обёртки, сессии и демона.

- `split_launch_options(args)` — снимает флаги ccas с командной строки `claude …`: `-t/--transport`, `--tg-token`, `--tg-chat`, `--tg-thread`, `-C <каталог>`; `-n/--name` читается, но остаётся — это флаг самого claude, ccas лишь использует его как алиас. `-C` в верхнем регистре не конфликтует с `-c` claude: commander различает регистр.
- `tokenize(text)` — разбиение строки чата с кавычками, но без backslash-экранирования: здесь чаще всего Windows-пути, shlex съел бы каждый обратный слеш.
- `address(text, prefix, aliases)` — кому строка: известный алиас первым словом → той сессии; иначе с обязательным префиксом `None` без него, кроме слэш-команд и алиасов — они однозначны.
- `parse_claude_command(body)` — `/claude …` (и `/claude@бот …`) в `ClaudeCommand(options, args)`.
