<!-- DOCGEN:START -->
# telegram.py
<!-- DOCGEN:END -->

## Назначение

Bot API Telegram на `urllib`, без зависимостей: `getMe`, `getUpdates`, `sendMessage`, `editMessageText`, `editMessageReplyMarkup`, `answerCallbackQuery`, `sendChatAction`, `sendDocument` (multipart собирается вручную).

## Правило одного поллера

Telegram отдаёт 409 второму `getUpdates` с тем же токеном. Это не помеха, а гарантия, на которой стоит транспорт: `TokenBusy` — отдельное исключение, `poll_once` пробрасывает его сразу, а сетевые ошибки и прочие ответы API гасит с экспоненциальной паузой (1 → 60 с).

## Входящие

`parse_update` сплющивает сообщение или нажатие кнопки в `Incoming`: чат, топик (`message_thread_id`), пользователь, текст, для кнопки — `callback_id` и `callback_data`. Всё, что не сообщение и не кнопка, отбрасывается.

## Текст

`markdown_to_html` переводит markdown claude в HTML-подмножество Telegram: код защищается первым, остальное экранируется, потом заголовки/жирный/курсив/списки/ссылки. Блоки кода — голый `<pre>` без вложенного `<code class>`: `split_message` режет длинный текст по 4096 и закрывает/открывает `<pre>` на стыке, вложенный тег это сломал бы. Если Telegram не принял разметку (400), тот же кусок уходит без `parse_mode` через `strip_html`.
