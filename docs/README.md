<!-- DOCGEN:START -->
# ClaudeCodeAccountsSwitcher---python

## Папки

- [app](app/)
- [build](build/)
- [core](core/)
- [system](system/)
- [tests](tests/)
- [ui](ui/)

## Файлы

- [main.py](main.py.md)

<!-- DOCGEN:END -->

# ccas — Claude Code Accounts Switcher

Переключение между несколькими аккаунтами Claude Code. Один самодостаточный бинарник для Windows, Linux и macOS: ставит себя сам, перехватывает вызов `claude` через PATH и подменяет **только учётные данные**.

История, проекты, настройки, плагины и всё остальное в `~/.claude` остаются общими для всех аккаунтов.

## Как это работает

Claude Code читает путь к файлу кредов из отдельной переменной окружения:

```js
// claude v2.1.218
function KK(){ let e = process.env.CLAUDE_SECURESTORAGE_CONFIG_DIR;
  if (e !== void 0) return (e || join(homedir(), ".claude")).normalize("NFC");
  return cn(); }                        // -> join(KK(), ".credentials.json")
```

`CLAUDE_SECURESTORAGE_CONFIG_DIR` переносит **только** `.credentials.json` — в отличие от `CLAUDE_CONFIG_DIR`, который утащил бы за собой историю и проекты. Поэтому каждый слот получает свой каталог кредов, а всё остальное остаётся общим:

```
~/.claude/                        ← общее: projects/, history.jsonl, settings.json, plugins/
~/.claude.json                    ← общее, патчится только oauthAccount + userID

~/.claude-switcher/creds/1/.credentials.json   ← слот 1
~/.claude-switcher/creds/2/.credentials.json   ← слот 2
```

Побочный, но важный эффект: копировать креды туда-сюда больше не нужно, поэтому **два терминала на разных аккаунтах работают одновременно** и не затирают токены друг друга.

Если установленная сборка claude эту переменную не понимает, ccas автоматически переходит в режим копирования с файл-локом и проверкой владельца кредов перед сохранением.

## Структура проекта

| Каталог | Содержимое |
|---|---|
| [core/](core/) | состояние, конфиги claude, детект возможностей, журнал |
| [app/](app/) | обёртка, установщик, команды менеджера |
| [ui/](ui/) | меню, лимиты, мультиязычность |
| [system/](system/) | права доступа и правка PATH под конкретную ОС |
| [build/](build/) | spec PyInstaller и скрипты сборки |
| [tests/](tests/) | тесты |
| `lang/` | каталоги переводов; контракт описан в [ui/i18n.py](ui/i18n.py.md) |

Папка называется `system/`, а не `platform/`, потому что пакет с именем `platform` в корне репозитория затенил бы одноимённый модуль стандартной библиотеки — и `platform.machine()`, на котором держится определение архитектуры при сборке, перестал бы работать.

## Установка

Требуется уже установленный Claude Code. Права администратора не нужны — всё кладётся в домашний каталог, PATH правится только на уровне пользователя.

```bash
# Windows
.\ccas-windows-x64.exe install

# Linux
./ccas-linux-x64 install
```

Установщик найдёт оригинальный `claude`, определит режим кредов и язык интерфейса, спросит про `--dangerously-skip-permissions`, перенесёт существующий стор из `~/.claude-accounts` и подхватит текущий вход как слот.

После установки **открой новый терминал**, чтобы PATH обновился.

## Использование

```bash
claude                  # меню выбора аккаунта (только в интерактивном терминале)
claude 2                # запустить на слоте 2
claude 2 -p "привет"    # слот 2 с аргументами
claude @work            # по алиасу
claude -- 2             # промпт "2", а не слот
claude mcp list         # подкоманды пробрасываются как есть
```

Алиасы требуют префикса `@` намеренно: иначе `claude work` съел бы однословный промпт вместо того, чтобы отправить его в claude.

Меню показывается только когда `claude` вызван без аргументов **и** stdin/stdout — терминал. В скриптах и пайпах обёртка молча уходит на активный слот.

### Управление

```bash
ccas list [--refresh]       # аккаунты; --refresh опрашивает лимиты по сети
ccas switch 2 | @work       # сменить активный слот
ccas rename 2 work          # задать алиас
ccas add                    # подсказать свободный слот
ccas remove 2               # удалить слот вместе с токенами
ccas usage [--cached]       # лимиты по всем аккаунтам
ccas doctor                 # диагностика установки
ccas install                # накат новой версии поверх текущей
ccas uninstall [--purge]    # снять перехват; --purge удаляет и токены
```

## Язык интерфейса

Определяется автоматически при установке: по `LANGUAGE`/`LC_ALL`/`LC_MESSAGES`/`LANG` на Linux и по `GetUserDefaultUILanguage()` на Windows. Поддерживаются `en` и `ru`.

```bash
ccas install --lang ru      # зафиксировать в конфиге
CCAS_LANG=ru ccas list      # разово
```

## Хранение токенов

Креды лежат открытым текстом, ровно как их хранит сам Claude Code: файл должен оставаться читаемым для процесса claude, поэтому зашифровать его на месте нельзя. Защита — права доступа: `chmod 600` на Linux, ACL только для текущего пользователя на Windows.

## Сборка

Зависимостей нет — только стандартная библиотека Python 3.10+ и PyInstaller на время сборки.

```powershell
powershell -ExecutionPolicy Bypass -File build\build.ps1
```

```bash
bash build/build.sh
```

В `build/` лежат три файла: `ccas.spec` — рецепт сборки для PyInstaller (вшивает `lang/*.json`, объявляет лениво импортируемые модули, отсекает лишнее и вычисляет имя артефакта), плюс два тонких скрипта-обёртки под bash и PowerShell. Расширение `.spec` обязательно: по нему PyInstaller отличает рецепт сборки от входного скрипта программы.

Всё временное складывается в один каталог `build/__pycache__/`: туда направлены и байт-код (`PYTHONPYCACHEPREFIX`), и рабочие файлы PyInstaller (`--workpath`). Рядом с исходниками не остаётся ни одного `__pycache__`, а сам каталог в `.gitignore` и удаляется безболезненно.

Имя артефакта — `ccas-<ос>-<архитектура>[.exe]`, результат кладётся в `dist/`:

```
dist/ccas-windows-x64.exe
dist/ccas-linux-x64
dist/ccas-macos-arm64
```

Бинарник PyInstaller линкуется с glibc сборочной машины, поэтому Linux-релиз лучше собирать в контейнере на старой базе.

## Тесты

```bash
python -m unittest discover -s tests        # всё
python -m unittest discover -s tests -v     # с именами тестов
python -m unittest tests.test_i18n          # один модуль
python -m unittest tests.test_i18n.TestLookup.test_substitution
```

Своего раннера нет намеренно: штатное обнаружение `unittest` само подхватывает любой новый `tests/test_*.py`, а команда целиком помещается в переменную `CI_COMMAND` — в репозитории не нужен файл ради запуска тестов.

Общий базовый класс `tests.base.TempHome` на время каждого теста подменяет `Path.home` временным каталогом и выставляет `CCAS_HOME` и `CCAS_LANG=en`. Реальные `~/.claude` и `~/.claude.json` не читаются и не пишутся ни при каких обстоятельствах.

| Модуль | Что проверяет |
|---|---|
| `test_arguments` | разбор `2`, `@work`, `--`, подкоманды, голый промпт; подмешивание флагов; условия показа меню |
| `test_claude_config` | приоритет `.config.json`, запись во все цели, сохранность чужих ключей, отсутствие BOM |
| `test_credentials` | четыре состояния токена |
| `test_store` | round-trip слотов и конфига, атомарная запись, ротация бекапов |
| `test_migration` | импорт стора PowerShell-профиля и подхват живого входа |
| `test_pathenv` | правка rc-файлов, фолбэк на `~/.profile`, идемпотентность, синтаксис fish |
| `test_i18n` | совпадение ключей и плейсхолдеров между языками, определение языка |
| `test_usage` | разбор `resets_at`, вывод процентов, устойчивость к битым данным |

## CI/CD и релизы

Релиз выпускается пушем тега `vX.Y.Z`. Порядок такой:

```
ci-cd.yml : get-branch -> gate -> ci (тесты)
                                   |
release.yml : await-ci ------------+
                 |
              prepare -> build (матрица ОС) -> release (создаёт GitHub Release)
                                                  |
ci-cd.yml :                                       +-> cd (деплой, если настроен)
```

`release.yml` ждёт проверку с именем `ci`, а job `cd` в `ci-cd.yml` ждёт `release` — цикла нет.

### Где задавать переменные

Оба workflow используют `environment: <ветка>`. Поэтому переменные нужно завести **либо** на уровне репозитория (`Settings → Secrets and variables → Actions → Variables`), **либо** в Environment с именем ветки, от которой ставится тег — обычно `main`.

### Переменные для `ci-cd.yml`

| Переменная | Значение | Зачем |
|---|---|---|
| `ACTION_TRIGGER` | `RELEASE` | **обязательно.** Только при этом значении job `ci` запускается на пуше тега |
| `CI_COMMAND` | `python3 -m unittest discover -s tests` | **обязательно.** Пустое значение — тесты не запустятся никогда, а релиз всё равно соберётся |
| `BUILD_COMMAND` | не задавать | сборку делает матрица в `release.yml` |
| `DEPLOY_*`, секрет `DEPLOY_KEY` | не задавать | деплоя на сервер нет, job `cd` останется выключенным |

`python3`, а не `python`: job `ci` не содержит шага `actions/setup-python` и пользуется тем интерпретатором, что есть на раннере.

Побочный эффект `ACTION_TRIGGER=RELEASE`: пуши в ветку CI не запускают (значение одно, `PUSH` и `RELEASE` взаимоисключающи). Pull request'ы тесты прогоняют всегда.

### Переменные для `release.yml`

| Переменная | Значение | Зачем |
|---|---|---|
| `BUILD_MATRIX` | `["ubuntu-latest","windows-latest","macos-latest"]` | включает матричную сборку; без неё `.exe` не появится |
| `RELEASE_FILES` | `dist/*` | что прикладывать к релизу |
| `PYTHON_VERSION` | `3.11` | версия для сборки (по умолчанию `3.11`) |
| `BUILD_ARTIFACT_COMMAND` | по умолчанию `bash build/build.sh` | можно не задавать |
| `BUILD_ARTIFACT_PATH` | по умолчанию `dist` | можно не задавать |
| `BUILD_COMMAND` | **не задавать** | иначе сборка выполнится ещё раз на ubuntu и перетрёт артефакты матрицы |
| `PUBLISH_METHOD` | не задавать | публикации в npm/docker/packagist нет |
| `MULTIPLE_PACKAGES` | не задавать | тег имеет вид `vX.Y.Z` без префикса ветки |

Секреты не нужны: хватает автоматического `GITHUB_TOKEN`.

### Что попадёт в релиз

```
ccas-windows-x64.exe
ccas-linux-x64
ccas-macos-arm64
```

Имя вычисляет `build/ccas.spec` по `sys.platform` и `platform.machine()`, а не workflow; перебить можно переменной `CCAS_ARTIFACT_NAME`. `macos-latest` сейчас arm64 — если понадобится x64-сборка под Intel, добавьте в матрицу `macos-13`.

### Известное ограничение

`await-ci` использует `fail-on-no-checks: false`. Если `release.yml` стартует раньше, чем `ci-cd.yml` успевает зарегистрировать проверку `ci`, ожидание завершится сразу и релиз соберётся, не дождавшись тестов. Жёсткий гейт даёт `fail-on-no-checks: true`, но тогда релиз будет падать в конфигурациях, где `CI_COMMAND` не задан вовсе.

## Лицензия

[MIT](../LICENSE) © 2026 cat-of-summer

Проект не аффилирован с Anthropic. `ccas` не входит в состав Claude Code, не модифицирует и не переименовывает его исполняемый файл: перехват работает исключительно за счёт приоритета в `PATH`, а оригинальный `claude` запускается как есть. Взаимодействие ограничено штатными механизмами самого Claude Code — переменной окружения `CLAUDE_SECURESTORAGE_CONFIG_DIR` и его же файлами конфигурации.
