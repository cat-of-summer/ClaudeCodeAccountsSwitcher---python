<!-- DOCGEN:START -->
# test_console.py
<!-- DOCGEN:END -->

## Что проверяет

Чистую часть [system/console.py](../system/console.py.md) — ту, что не требует Windows-консоли и
поэтому прогоняется в Docker на Linux:

* `sane_input_mode()` содержит `PROCESSED|LINE|ECHO`, а `INSERT`/`QUICK_EDIT` — только вместе с
  `EXTENDED_FLAGS`;
* `is_broken()` считает сломанным то, что оставляет ink (`0x200` — один VT-ввод), и здоровым
  обычную консоль (`0x1F7`);
* `RESET_SEQUENCES` выключает альтернативный экран, все пять режимов мыши и bracketed paste, и
  ничего не включает, кроме курсора и переноса строк;
* `sanitize()` пишет в tty и молчит в трубу;
* `repair()` не трогает здоровую консоль, на сломанной зовёт `restore(None)` и `sanitize()`, а
  без консоли просто возвращает `False`. Платформенные функции здесь замоканы.
