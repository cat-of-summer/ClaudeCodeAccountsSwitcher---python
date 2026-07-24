<!-- DOCGEN:START -->
# base.py
<!-- DOCGEN:END -->

Общий базовый класс `TempHome` — от него наследуются все тесты.

`setUp` подменяет `Path.home` временным каталогом, выставляет `CCAS_HOME` и `CCAS_LANG=en`, создаёт раскладку стора. `tearDown` возвращает `Path.home` и окружение на место и удаляет каталог.

Язык фиксируется, чтобы проверки текста не зависели от локали машины.

Вспомогательные методы: `write_root_config` — готовый `~/.claude.json` с identity, `identity` — блок `oauthAccount` + `userID`, `write_credentials` — файл кредов с переопределяемыми полями.
