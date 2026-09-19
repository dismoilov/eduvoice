# Как проверить систему за 10 минут

Каждая строка — команда и то, что должно получиться. Все примеры вывода ниже сняты с
живого сервера `10.103.10.86` 18.09.2026, а не написаны «по памяти».

Телефон для проверки не нужен: звонки генерирует сам Asterisk.

---

## 0. Проверка без сервера (полминуты)

```bash
make check
```

Ожидается: `All checks passed!` от линтера, `Success: no issues found` от проверки типов
и `227 passed` от тестов.

```bash
uv run pytest -q --cov=eduvoice --cov-report=term
```

Среди тестов есть сквозной
([tests/test_end_to_end.py](../tests/test_end_to_end.py)): он поднимает настоящие серверы
моста, подключается к ним TCP-сокетом, «проигрывает» звонок ровно так, как это делает
Asterisk, и затем спрашивает у control API, что делать со звонком.

---

## 1. Сервис жив

```bash
make status
```

```
active
LISTEN 0 100 127.0.0.1:9092  users:(("python",pid=…))
LISTEN 0 100 127.0.0.1:9093  users:(("python",pid=…))
3
```

Звук и control API слушаются **только на 127.0.0.1** — наружу мост не доступен; по сети
открыт лишь порт CRM 9095, и то под логином и паролем. Проверка снаружи:

```bash
curl --max-time 4 http://10.103.10.86:9093/health    # должно не ответить вовсе
ssh root@10.103.10.86 'curl -s http://127.0.0.1:9093/health'
```

```json
{"status": "ok", "active_calls": 0, "provider": "voicelab"}
```

---

## 2. Звонок целиком: вопрос → ответ

```bash
make say TEXT="stipendiya qanday olinadi"   # что «услышит» заглушка распознавания
make call-test                              # Asterisk сам звонит помощнику
make calls                                  # журнал звонков
```

В журнале (`logs/YYYYMMDD.jsonl`) появляется строка вида:

```json
{"call_id": "0db3e713-…", "duration_s": 38.4, "next_action": "operator",
 "ended_reason": "caller_hangup",
 "turns": [{"question": "stipendiya qanday olinadi",
            "answer": "Stipendiya davlat granti asosida oʻqiyotgan…",
            "intent": "faq_keyword",
            "latency_ms": {"utterance_ms": 1740, "stt_ms": 302, "total_ms": 302}}]}
```

Что это доказывает: вопрос распознан, ответ найден по ключевым словам в базе знаний CRM
(без обращения к модели, поэтому 302 мс), ответ проигран звонящему. Если база недоступна,
мост берёт те же ответы из `content/faq.yaml` — это резерв, а не источник правды.

Для звонка с настоящего телефона первая строка сессии обязана быть такой:

```
Asterisk sends 160-byte chunks, format ulaw
```

`160-byte` — это G.711 (один байт на отсчёт, 20 мс). Если формат другой, мост сам скажет
об этом строкой `ERROR` и отдаст звонок оператору — см. OPERATIONS.md, раздел про G.711.

Живой лог того же звонка:

```bash
make logs   # journalctl -f
```

```
call 0db3e713-…: Asterisk sends 320-byte chunks
call 0db3e713-…: heard 'stipendiya qanday olinadi'
call 0db3e713-…: decision=faq intent=faq_keyword (302 ms total)
call 0db3e713-… ended after 38.4 s, 1 turns (caller_hangup)
```

---

## 2a. Вопрос, которого нет в базе знаний: ответ по самому закону

База знаний покрывает шесть вопросов. Всё остальное раньше кончалось фразой «я вас не
понял» и переводом на оператора — даже когда ответ прямо написан в постановлении.
Теперь помощник ищет его в самих нормативных актах, скачанных с lex.uz.

Посмотреть, что вообще проиндексировано:

```bash
make lex-list
```

```
          59-son   155 band  Oliy taʼlim muassasalari talabalariga toʻlanadigan stipe…
         344-son    90 band  … akademik taʼtil berish toʻgʻrisidagi nizom
         578-son   673 band  … oʻqishga qabul qilish jarayonlari
        3807-son   217 band  Oliy taʼlim toʻgʻrisidagi nizom
    OʻRQ-637-son   659 band  Taʼlim toʻgʻrisida
         376-son   165 band  … talabalarni turar joy bilan taʼminlash
         605-son   146 band  … ijara toʻlovlarini qoplab berish
         836-son   231 band  … yakuniy davlat attestatsiyasi
        3781-son    41 band  … taʼlim yoʻnalishlari klassifikatori

  9 hujjat, 2377 band
```

Какие пункты найдёт вопрос — **без модели**, чистый поиск:

```bash
make lex-ask Q="Talabalar turar joyi uchun to'lov qancha?"
```

```
  376-son, 23-band  https://lex.uz/uz/docs/-6568679#-6571746
  23. Talabalar turar joyi uchun oylik toʻlov miqdori va muddatlari oliy taʼlim
  muassasasi tomonidan belgilanadi…
```

Ссылка открывает **этот пункт** на lex.uz, а не сто страниц постановления — цитату можно
проверить за десять секунд.

Теперь то же самое вопросом не по теме:

```bash
make lex-ask Q="Avtobus qachon keladi?"
```

```
  hech narsa topilmadi -> operator
```

Защита здесь двойная, и вторая половина важнее первой.

**Первая: поиск.** Пункт предлагается модели, только если делит с вопросом не меньше двух
значащих слов, и хотя бы одно из них **редкое** — встречается не более чем в десятой
части корпуса. Совпадение по «davom etadi» («длится») — это совпадение по грамматике, а
не по смыслу.

**Вторая: модель обязана назвать пункт.** Поиск не безупречен, и честнее это показать,
чем прятать. Вопрос про футбол редкого слова не имеет, но «davom etadi» в корпусе из 2377
пунктов встречается достаточно редко, чтобы пройти фильтр:

```bash
make lex-ask Q="Futbol o'yini necha daqiqa davom etadi?"
```

```
  3807-son, 13-band  https://lex.uz/uz/docs/-8117474#-8120411
  13. Bakalavriat taʼlim bosqichida oʻqish kamida uch yil davom etadi…
```

Пункт найден — про длительность бакалавриата. Но в звонке ответа не будет:

```
decision=clarify intent=out_of_scope
```

Модель получает выдержки и обязана вернуть номер использованного пункта. Если выдержки не
отвечают на вопрос, она возвращает ноль — и ответ отбрасывается целиком, даже если текст
написан. Ответ, который модель не может привязать к выданному ей пункту, до гражданина не
доходит: такой звонок уходит человеку.

В журнале звонка это видно так:

```
call …: answered from 376-son, 23-band for 'Talabalar turar joyi uchun to'lov qancha?'
call …: decision=answer intent=law (1929 ms total)
```

В CRM у такой реплики в поле «источник» стоит номер пункта и ссылка на него.

Обновить корпус (единственное место, которое ходит в интернет, и никогда во время
звонка):

```bash
make lex-fetch
```

---

## 3. Перебивание (barge-in)

Перебить можно **ответ** и фразу «одну минуту, проверяю». **Приветствие — нельзя**, это
решение: оно объясняет, что это за служба, и в шумном помещении обрывалось шумом за
полсекунды, так что звонящий не успевал ничего понять.

Проверка: позвонить, дождаться конца приветствия, задать вопрос и **заговорить поверх
ответа**. В журнале:

```
call f64feab6-…: barge-in
call f64feab6-…: heard 'akademik taʼtil qanday rasmiylashtiriladi'
```

Бот замолкает за 0,3–0,5 с и слышит новую фразу **целиком**: сказанное поверх него
переигрывается в детектор речи, поэтому начало не теряется (`SpeechDetector.seed`).

То же переигрывание работает и для приветствия: вопрос, заданный поверх него, не
пропадает — он доходит до распознавания в тот момент, когда приветствие заканчивается
(`test_a_question_asked_over_the_greeting_is_handed_back_not_dropped`).

Включить перебивание приветствия: `INTERRUPTIBLE_GREETING=1` в `.env` и перезапуск.

---

## 4. Перевод на живого оператора

```bash
make agent-on                                       # подставной оператор в очередь
make say TEXT="operator bilan gaplashmoqchiman"
make call-test
ssh root@10.103.10.86 'asterisk -rx "core show channels"'
```

Ожидается канал в очереди `eduvoice-operators` и ответивший агент:

```
Local/s@eduvoice-ai-…;2      s@eduvoice-operators:3   Up   Queue(eduvoice-operators,t,,,300)
Local/agent@eduvoice-testagent-…;1                 Up   AppQueue((Outgoing Line))
```

---

## 5. Самое важное: отказ не оставляет человека в тишине

Проверка «мост умер посреди разговора»:

```bash
make call-test
ssh root@10.103.10.86 'systemctl stop eduvoice-bridge'     # во время разговора
ssh root@10.103.10.86 'asterisk -rx "core show channels"'
ssh root@10.103.10.86 'systemctl start eduvoice-bridge'
```

Звонок не сбрасывается — он уходит в очередь операторов. В логе Asterisk при этом честно
видно, что мост недоступен, и что диалплан это пережил:

```
WARNING func_curl.c: Failed connect to 127.0.0.1:9093; Connection refused
WARNING res_audiosocket.c: Failed to write data to AudioSocket
→ канал в Queue(eduvoice-operators)
```

Почему так: диалплан использует `Dial(AudioSocket/…,,g)`, а не приложение `AudioSocket()`
(оно завершает звонок), а control API на любой непонятный запрос отвечает `operator`.

Те же гарантии закреплены тестами — [tests/test_session_failures.py](../tests/test_session_failures.py):

| Тест | Что доказывает |
|---|---|
| `test_a_broken_model_transfers_instead_of_leaving_the_caller_in_silence` | модель упала или зависла → оператор |
| `test_the_dialplan_is_told_what_to_do_even_if_the_closing_phrase_is_missing` | ошибка в текстах не отменяет перевод на оператора |
| `test_cleanup_runs_even_when_the_socket_dies_mid_answer` | оборванный звонок не оставляет утечек |
| `test_audio_output_survives_a_dead_socket` | мёртвый сокет не подвешивает звонок |
| `test_an_unknown_faq_id_is_never_spoken` | модель не может «придумать» норму |
| `test_the_model_never_decides_to_hang_up_by_inventing_an_intent` | модель не может сама положить трубку |

---

## 5a. CRM

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://10.103.10.86:9095/calls     # 303 — без входа никак
# вход теперь требует токен формы — так же, как из браузера
CSRF=$(curl -s -c jar http://10.103.10.86:9095/login | grep -oE 'name="csrf" value="[^"]+"' \
       | head -1 | sed 's/.*value="//; s/"//')
curl -s -b jar -c jar -o /dev/null --data-urlencode "login=nazorat" \
     --data-urlencode "password=ПАРОЛЬ" --data-urlencode "csrf=$CSRF" \
     http://10.103.10.86:9095/login
curl -s -b jar http://10.103.10.86:9095/calls | grep -c 99890                # звонки на месте
```

В браузере `http://10.103.10.86:9095`, вход супервайзером. Что проверить:

1. **Рабочий стол** — звонки за сегодня, доля без оператора, средний ответ, состояние помощника;
2. **Звонки** — поиск по тексту разговора, карточка с записью, метка «▶ 0:12» перематывает на вопрос;
3. **Создать обращение из звонка** → обращение появляется с номером вида `2026-0001`, гражданин привязан;
4. **Обращения** — смена статуса и комментарий попадают в ленту истории;
5. **База знаний** — изменить ответ, опубликовать, позвонить ещё раз: помощник говорит новое (проверено 18.09: звонок в 14:37 — старый текст, в 14:38 после правки — новый, мост работал с 14:36:37 и не перезапускался);
6. **Аналитика** — графики по дням и часам, темы, нагрузка, выгрузка CSV;
7. **Роли** — оператор получает 403 на `/admin` и `/analytics`, супервайзер — на `/admin`.

Подробности — [CRM.md](CRM.md).

---

## 6. Нагрузка

```bash
ssh root@10.103.10.86 'for i in 1 2 3; do asterisk -rx "channel originate Local/s@eduvoice-ai/n extension qhello-world@eduvoice-test"; done; uptime'
```

Три одновременных звонка обслуживаются, load average остаётся около нуля. Мост держит
столько звонков, сколько потянет VoiceLab (у аккаунта ограничение — 2 одновременные
генерации модели, см. [PLAN.md](../PLAN.md)).

---

## 7. Записи разговоров

```bash
ssh root@10.103.10.86 'ls -lh /var/spool/asterisk/monitor/eduvoice/$(date +%Y%m%d)/ | tail -3'
```

Каждый разговор записывается (`MixMonitor`) — это требование к call-центру и заодно
материал для настройки распознавания на реальной речи. О приватности и сроках хранения —
[OPERATIONS.md, раздел «Данные и приватность»](OPERATIONS.md#данные-и-приватность).
