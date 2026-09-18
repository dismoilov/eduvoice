# Эксплуатация

## Сервер

| | |
|---|---|
| Адрес | `10.103.10.86` (Sangoma Linux 7, FreePBX 16, Asterisk 20.4) |
| Часовой пояс | Asia/Tashkent |
| Мост | `/opt/eduvoice/app`, пользователь `eduvoice`, сервис `eduvoice-bridge` |
| CRM | тот же каталог, сервис `eduvoice-crm` |
| Порты моста | `127.0.0.1:9092` (звук), `127.0.0.1:9093` (control API) — только локально |
| CRM | `9095` по сети, вход по логину и паролю из `/root/eduvoice-credentials.txt` |
| Записи разговоров | `/var/spool/asterisk/monitor/eduvoice/<дата>/<uuid>.wav` |
| Журнал звонков | `/opt/eduvoice/app/logs/<дата>.jsonl` |
| Секреты | `/root/eduvoice-credentials.txt` (права `600`), `.env` (права `600`) |

Внутренние номера: **7000** — помощник (через IVR), **300** — операторы, **7777** — эхо-тест
для проверки звука софтфона. Очередь операторов — `eduvoice-operators`.

## Деплой

```bash
make deploy            # линтер + тесты → rsync → uv sync → перезапуск сервиса
make deploy-dialplan   # диалплан и глобальные переменные → Asterisk → reload
```

`make deploy` не выкатит код, у которого не проходят тесты, — это сделано намеренно.
Секреты не выкатываются: `.env` исключён из синхронизации и живёт только на сервере.

Файлы на сервере, которые являются копией репозитория (менять их надо в репозитории):

- `/etc/asterisk/extensions_custom.conf` ← `asterisk/extensions_custom.conf`
- `/etc/asterisk/globals_custom.conf` ← `asterisk/globals_custom.conf`
- `/etc/asterisk/queues_custom.conf` ← `asterisk/queues_custom.conf`
- `/etc/systemd/system/eduvoice-bridge.service` ← `deploy/eduvoice-bridge.service`
- `/etc/systemd/system/eduvoice-crm.service` ← `deploy/eduvoice-crm.service`

Проверить, что сервер не разошёлся с репозиторием, можно построчным сравнением — на
18.09.2026 все пять файлов совпадали байт в байт.

## Повседневные команды

```bash
make status        # сервисы, порты, внутренние номера
make crm-logs      # живой лог CRM
make crm-restart
make backup        # согласованная копия базы
make sql Q="select count(*) from calls"   # заглянуть в базу
make crm-user USER_LOGIN=… USER_NAME="…" USER_ROLE=operator USER_PASSWORD=… USER_EXT=101
make logs          # живой лог моста
make calls         # последние звонки с задержками
make restart
make hangup-all    # сбросить все звонки (после экспериментов)

make say TEXT="…"  # что «услышит» заглушка распознавания
make call-test     # смоделировать звонок с вопросом
make barge-test    # смоделировать перебивание
make agent-on/off  # подставной оператор в очереди

make voice-prewarm # озвучить все фразы и ответы один раз, в кэш
```

### Почему в базу нельзя ходить системным `sqlite3`

На сервере стоит Sangoma Linux 7, и `sqlite3` в нём — версии 3.7.17 от 2013 года. Он не
знает ни полнотекстового поиска, ни индексов по выражениям, и на совершенно исправную
базу отвечает так:

```
Error: malformed database schema (calls_day) - near "(": syntax error
```

База при этом цела: приложение работает с той же базой через SQLite 3.53, который идёт
вместе с Python проекта. Поэтому смотреть в базу нужно через `make sql` — он использует
правильную библиотеку и разрешает только чтение. Резервная копия (`make backup`) делается
тем же способом и от старой утилиты не зависит.

### Озвученные фразы — это оплаченные данные

Служебные фразы и ответы синтезируются один раз и лежат в `audio/tts-cache` и
`audio/prompts` на сервере. Синтез стоит кредитов VoiceLab, а тексты не меняются, поэтому
кэш переживает выкладку (`make deploy` каталог `audio` не трогает) и **удалять его
нельзя** — восстановить можно только заново заплатив. Посмотреть, чего не хватает, не
тратя ничего:

```bash
make voice-prewarm ARGS=--dry-run
```

## Настройки

Все параметры — в `.env` на сервере (шаблон — `.env.example`), читаются в
[`eduvoice/config.py`](../eduvoice/config.py). Менять их можно без правки кода.

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `EDUVOICE_PROVIDER` | `fake` | `fake` — заглушки, `voicelab` — реальная речь |
| `VOICELAB_API_KEY` | — | ключ VoiceLab (только на сервере) |
| `SILENCE_END_FRAMES` | 35 | сколько тишины (×20 мс) означает конец фразы |
| `BARGE_IN_FRAMES` | 15 из 20 | насколько уверенно человек должен заговорить, чтобы прервать бота |
| `BARGE_IN_GUARD_MS` | 500 | не считать эхом собственный голос сразу после начала фразы |
| `FILLER_AFTER_S` | 1.2 | через сколько звучит «минуту, проверяю» |
| `STT_TIMEOUT_S` / `LLM_TIMEOUT_S` | 5 / 5 | после таймаута — оператор |
| `MAX_TURNS` | 8 | длиннее разговор — оператор |
| `MAX_CALL_S` | 360 | предел длительности звонка |
| `CRM_HOST` / `CRM_PORT` | `127.0.0.1` / 9095 | где слушает CRM (`0.0.0.0` — по сети) |
| `CRM_SLA_HOURS` | 24 | срок, после которого обращение считается просроченным |
| `EDUVOICE_DB` | `data/eduvoice.db` | файл базы |
| `ARI_USER` / `ARI_PASSWORD` | — | нужны для кнопки «Позвонить» в карточке гражданина |
| `RECORDINGS_DIR` | `/var/spool/asterisk/monitor` | откуда CRM берёт записи |

После изменения `.env` нужен `make restart`.

## Диагностика

| Симптом | Куда смотреть |
|---|---|
| Звонящий слышит тишину после приветствия | `make logs` — есть ли `heard '…'`; если нет, смотреть на детектор речи и `SILENCE_END_FRAMES` |
| Все звонки уходят на оператора | `systemctl is-active eduvoice-bridge`, `curl 127.0.0.1:9093/health`; при недоступном мосте это штатное поведение |
| Бот перебивает сам себя | увеличить `BARGE_IN_GUARD_MS` и `BARGE_IN_FRAMES` (эхо линии) |
| Бот не даёт себя перебить | уменьшить `BARGE_IN_FRAMES` |
| Не играет узбекское меню IVR | нет файла `/var/lib/asterisk/sounds/en/custom/eduvoice-ivr-menu.wav` — диалплан временно играет встроенный звук |
| В логе Asterisk `Failed to write data to AudioSocket` | мост перезапускался; звонок при этом уходит на оператора |

Полезные команды Asterisk:

```bash
asterisk -rx "dialplan show eduvoice-ai"
asterisk -rx "core show channels"
asterisk -rx "queue show eduvoice-operators"
asterisk -rx "module show like audiosocket"
```

## Данные и приватность

Голос — биометрические данные, поэтому вся обработка остаётся внутри страны: телефония на
своём сервере, речь и модель — VoiceLab.uz (Узбекистан). Внешние облачные сервисы
(например, зарубежные голосовые API) сознательно не используются.

Что где хранится:

| Данные | Где | Доступ |
|---|---|---|
| CRM со всем перечисленным | `http://сервер:9095` | логин, пароль, роль |
| База данных (звонки, обращения, граждане) | `/opt/eduvoice/data/eduvoice.db` | `eduvoice`, `root` |
| Запись разговора | `/var/spool/asterisk/monitor/eduvoice/` | `asterisk`, `root` |
| Расшифровка вопросов и ответы | `logs/<дата>.jsonl` | `eduvoice`, `root` |
| Номер звонящего | там же | `eduvoice`, `root` |
| Ключи (VoiceLab, ARI, база) | `/root/eduvoice-credentials.txt`, `.env` (права `600`) | только `root`/`eduvoice` |
| Озвученные фразы (кэш синтеза) | `/opt/eduvoice/app/audio/` | `eduvoice`, `root` |

Принятые меры: control API слушает только `127.0.0.1`; порты моста наружу не открыты;
сервис работает не от `root`, с `NoNewPrivileges`, `ProtectSystem=full`, `ProtectHome`;
в репозитории нет ни одного секрета (`.env` в `.gitignore`).

Что нужно сделать до промышленной эксплуатации (сознательно не сделано на хакатоне и
честно указывается проверяющим):

- срок хранения записей и расшифровок и автоматическая очистка;
- предупреждение звонящему о записи — уже есть в тексте приветствия (`content/prompts.yaml`);
- шифрование архива записей;
- аутентификация на control API — сейчас защита строится на том, что порт доступен только
  локально, этого достаточно для демо, но не для продуктива;
- для CRM: HTTPS и запись в журнал того, кто какую запись слушал (сейчас журнал фиксирует
  входы и изменения, но не прослушивание);
- регулярная выгрузка резервной копии базы за пределы сервера (`make backup` делает копию
  рядом, этого мало для настоящей эксплуатации).

## Если всё сломалось прямо перед показом

```bash
ssh root@10.103.10.86 'systemctl restart eduvoice-bridge eduvoice-crm && sleep 3 && curl -s 127.0.0.1:9093/health'
ssh root@10.103.10.86 'fwconsole reload'
make hangup-all
```

Резервная копия конфигурации сервера до начала работ:
`/root/backup-before-eduvoice-20260917-160257`.

Крайний случай: остановить мост совсем — тогда номер 7000 продолжит работать, но все
звонки будут уходить сразу на живых операторов. Это и есть штатный режим деградации.
