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
и `130 passed` от тестов.

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

## 3. Перебивание (barge-in)

```bash
make barge-test     # «звонящий» начинает говорить поверх приветствия
make logs
```

```
call f64feab6-…: barge-in
call f64feab6-…: heard 'stipendiya qanday olinadi'
call f64feab6-…: decision=faq intent=faq_keyword (301 ms total)
```

Бот замолкает примерно за 0.5 с и слышит фразу **целиком**: кадры, сказанные поверх бота,
переигрываются в детектор речи, поэтому начало перебившей фразы не теряется
(`SpeechDetector.seed`, тест `test_seed_replays_the_start_of_an_interrupted_phrase`).

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
