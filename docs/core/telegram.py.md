<!-- DOCGEN:START -->
# telegram.py
<!-- DOCGEN:END -->

## Назначение

Bot API Telegram на `urllib`, без зависимостей: `getMe`, `getUpdates`, `sendMessage`, `editMessageText`, `editMessageReplyMarkup`, `answerCallbackQuery`, `sendChatAction`, `sendDocument` и `sendMediaGroup` (multipart собирается вручную в `upload`, словари и списки в полях уходят JSON), `getFile` и скачивание файла (`download`, GET `/file/bot<token>/<path>` потоком в открытый файл). Пределы Bot API: боту отдают файлы до 20 МБ (`DOWNLOAD_LIMIT_BYTES`), принимают от него до 50 МБ (`UPLOAD_LIMIT_BYTES`), в альбоме до 10 элементов. Файлы идут с таймаутом 300 с, а не 30, как сообщения.

## Правило одного поллера

Telegram отдаёт 409 второму `getUpdates` с тем же токеном. Это не помеха, а гарантия, на которой стоит транспорт: `TokenBusy` — отдельное исключение, `poll_once` пробрасывает его сразу, а сетевые ошибки и прочие ответы API гасит с экспоненциальной паузой (1 → 60 с).

## Входящие

`parse_update` сплющивает сообщение или нажатие кнопки в `Incoming`: чат, топик (`message_thread_id`), пользователь, текст (или подпись к файлу), для кнопки — `callback_id` и `callback_data`. Всё, что не сообщение и не кнопка, отбрасывается.

Вложения — `files`, кортеж `Attachment(file_id, name, size)`: `document` со своим именем, самый крупный размер `photo` (`photo_<file_unique_id>.jpg`), `video`, `audio`, `voice`, `video_note`, `animation` (имя из `file_name`, иначе из типа и `file_unique_id`). `media_group` — `media_group_id` альбома; склейка частей в [poller.py](../app/transport/poller.py.md). Демон передаёт `Incoming` транспорту как `dataclasses.asdict` через JSON, и вложения по дороге становятся словарями — обратно их собирает `Incoming.from_dict`; незнакомые ключи он пропускает, отсутствующие берёт по умолчанию.

## Текст

`markdown_to_html` переводит markdown claude в HTML-подмножество Telegram: код защищается первым, остальное экранируется, потом заголовки/жирный/курсив/списки/ссылки. Блоки кода — голый `<pre>` без вложенного `<code class>`: `split_message` режет длинный текст по 4096 и закрывает/открывает `<pre>` на стыке, вложенный тег это сломал бы. Если Telegram не принял разметку (400), тот же кусок уходит без `parse_mode` через `strip_html`.
