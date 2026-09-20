<!-- DOCGEN:START -->
# tests

## Файлы

- [base.py](base.py.md)
- [test_arguments.py](test_arguments.py.md)
- [test_autoswitch.py](test_autoswitch.py.md)
- [test_claude_config.py](test_claude_config.py.md)
- [test_console.py](test_console.py.md)
- [test_concurrency.py](test_concurrency.py.md)
- [test_credentials.py](test_credentials.py.md)
- [test_i18n.py](test_i18n.py.md)
- [test_migration.py](test_migration.py.md)
- [test_oauth.py](test_oauth.py.md)
- [test_pathenv.py](test_pathenv.py.md)
- [test_settings.py](test_settings.py.md)
- [test_store.py](test_store.py.md)
- [test_supervise.py](test_supervise.py.md)
- [test_usage.py](test_usage.py.md)

<!-- DOCGEN:END -->

## Запуск

```bash
python -m unittest discover -s tests        # всё
python -m unittest discover -s tests -v     # с именами тестов
python -m unittest tests.test_i18n          # один модуль
```

Своего раннера нет намеренно: штатное обнаружение `unittest` подхватывает любой новый `tests/test_*.py`, а команда целиком помещается в переменную `CI_COMMAND` — файла ради запуска тестов в репозитории не нужно.

Зависимостей нет, только стандартная библиотека. CI поэтому ничего не устанавливает перед прогоном.

## Изоляция

Все тесты наследуются от [base.TempHome](base.py.md): на время каждого теста подменяется `Path.home`, выставляются `CCAS_HOME` и `CCAS_LANG=en`. Реальные `~/.claude` и `~/.claude.json` не читаются и не пишутся ни при каких обстоятельствах.

## Зафиксированные регрессии

* `claude work` обязан остаться промптом, а не выбором слота по алиасу.
* Повторное переключение на тот же слот не должно переписывать мегабайтный конфиг.
* Миграция старого стора не должна терять аккаунт, под которым пользователь залогинен сейчас.
* Установка на системе без rc-файлов и без `$SHELL` обязана прописать PATH, а не сделать вид.
* Истёкший access-токен не должен заслонять собой последние известные проценты: он ничего не значит и чинится сам.
* Пустой `defaultArgs` — это «ничего не добавлять», а не «значение не задано». Иначе отказ от `--dangerously-skip-permissions` отменялся сам собой при следующей загрузке конфига.
* `maxSwitches: 0` — это «не переключаться никогда». Тот же класс ошибки, что и выше: `value or default` не отличает ноль от отсутствия.
* Обмен refresh-токена обязан выбросить свой ответ, если файл кредов изменился, пока висел запрос.
* Два терминала, упёршиеся в лимит одного аккаунта, обязаны уехать на один и тот же слот.
* Маркер лимита из **прошлого** прогона (после `--resume`) не должен вызывать переключение.
