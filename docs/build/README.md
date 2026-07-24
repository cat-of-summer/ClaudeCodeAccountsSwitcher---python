<!-- DOCGEN:START -->
# build

<!-- DOCGEN:END -->

## Назначение

Сборка одного самодостаточного бинарника из исходников. Сами файлы внутри `build/` из документации исключены — они меняются вместе с рецептом сборки, и описывать каждый отдельно смысла нет.

## Состав

| Файл | Роль |
|---|---|
| `ccas.spec` | рецепт для PyInstaller |
| `build.sh` | обёртка для bash (Linux, macOS, Git Bash на Windows) |
| `build.ps1` | обёртка для PowerShell |
| `__pycache__/` | всё временное, в `.gitignore` |

## Почему расширение `.spec`, а не `.py`

Файл написан на Python, но расширение здесь — переключатель режима PyInstaller:

* `pyinstaller build/ccas.spec` — «это рецепт»: PyInstaller **исполняет** файл, впрыскивая в его globals `Analysis`, `PYZ`, `EXE`, `SPEC`, `DISTPATH`. Поэтому в файле нет ни одного `import` для этих имён.
* `pyinstaller build/ccas.py` — «это входной скрипт программы»: PyInstaller попытался бы **заморозить сам этот файл**, и сборка упала бы, потому что при обычном запуске `Analysis` не существует.

Переименование меняет смысл команды на противоположный.

## Что делает spec

* вшивает `lang/*.json` внутрь бинарника — иначе интерфейс останется без переводов;
* объявляет `hiddenimports`: модули вроде `system.pathenv_win` импортируются лениво, статический анализ их не видит;
* отсекает ненужное (`tkinter`, `numpy`, `PIL`), удерживая размер;
* вычисляет имя артефакта `ccas-<ос>-<архитектура>` по `sys.platform` и `platform.machine()`. Перебивается переменной `CCAS_ARTIFACT_NAME`.

## Временные файлы

Всё промежуточное складывается в один каталог `build/__pycache__/`: туда направлены и байт-код через `PYTHONPYCACHEPREFIX`, и рабочие файлы PyInstaller через `--workpath`. Рядом с исходниками не остаётся ни одного `__pycache__`.

Без явного `--workpath` PyInstaller сложил бы свой мусор прямо в `build/`, вперемешку со скриптами. Подкаталог `__pycache__/ccas/` называется по имени spec-файла — это соглашение самого PyInstaller, к имени артефакта отношения не имеет.

## Сборка в контейнере

Бинарник PyInstaller линкуется с glibc сборочной машины, поэтому Linux-релиз лучше собирать на старой базе. Обязателен `binutils` — без него PyInstaller падает с `On Linux, objdump is required`:

```bash
docker run --rm -v "$PWD":/src -w /src python:3.11-slim bash -c \
  'apt-get update && apt-get install -y binutils && bash build/build.sh'
```
