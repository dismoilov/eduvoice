# EduVoice — план разработки

> **Проект:** голосовой ИИ-оператор колл-центра Министерства высшего образования (задачи №5 и №3 из `xorazm_dolzarb_muammolar.pdf`).
> **Для кого:** команда. **Человек A** — телефония, мост, логика диалога. **Человек B** — всё, что связано с VoiceLab (раздел 7, отдельное ТЗ).
> **Статус:** версия 1, 17.09.2026. Факты о VoiceLab взяты из docs.voicelab.uz, факты о сервере проверены на 10.103.10.86.

---

## Содержание

1. [Цель и критерий «готово»](#1-цель-и-критерий-готово)
2. [Что уже сделано на сервере](#2-что-уже-сделано-на-сервере)
3. [Архитектура](#3-архитектура)
4. [Контракты между частями (чтобы A и B работали параллельно)](#4-контракты-между-частями)
5. [Логика диалога](#5-логика-диалога)
6. [План работ человека A](#6-план-работ-человека-a)
7. [ТЗ: VoiceLab (человек B)](#7-тз-voicelab-человек-b)
8. [Контент: частые вопросы и промпт](#8-контент-частые-вопросы-и-промпт)
9. [Тестирование без телефонов](#9-тестирование-без-телефонов)
10. [Расписание по дням](#10-расписание-по-дням)
11. [Риски и запасные варианты](#11-риски-и-запасные-варианты)
12. [Демо и чек-лист](#12-демо-и-чек-лист)
13. [Что нужно от команды](#13-что-нужно-от-команды)

---

## 1. Цель и критерий «готово»

**MVP (обязательно):** человек звонит на номер, задаёт вопрос голосом на узбекском, бот отвечает голосом по теме высшего образования, по просьбе «оператор» переводит звонок живому оператору.

| # | Критерий | Как проверяем |
|---|---|---|
| 1 | Звонок с софтфона на 7000 → приветствие голосом | Слышно приветствие |
| 2 | Вопрос → ответ голосом по одному из частых вопросов | 8 из 10 тестовых вопросов получают правильный ответ |
| 3 | Перебивание: человек начал говорить → бот замолкает | Бот молчит не позже чем через 0,5 с |
| 4 | «Operator» / «оператор» → звонит телефон 200 | Оператор поднимает трубку |
| 5 | Мост упал или VoiceLab недоступен → звонок уходит оператору, не висит в тишине | Остановить службу во время звонка |
| 6 | Задержка «замолчал → бот заговорил» | Типично ≤ 2,5 с, с фразой-заполнителем тишины нет |

**Не делаем в MVP (только если останется время):** поиск по нормативным актам (RAG), веб-панель оператора, реальный городской номер, аналитика.

---

## 2. Что уже сделано на сервере

**Сервер:** `10.103.10.86`, Sangoma Linux 7, FreePBX 16.0.40.7, Asterisk 20.4.0, 4 ядра, 7,8 ГБ RAM. Пароли и ключи — только в `/root/eduvoice-credentials.txt` на сервере (в git и чаты не копировать).

| Что | Состояние |
|---|---|
| Бэкап до изменений | `/root/backup-before-eduvoice-20260917-160257` |
| Часовой пояс | Asia/Tashkent (система, PHP, логи Asterisk) |
| MySQL | Только `127.0.0.1`, анонимный пользователь удалён |
| Внутренние номера PJSIP | 101, 102 (софтфоны), 200 (оператор) |
| ARI | Включён, пользователь `eduvoice` (HTTP 200) |
| Очередь | `eduvoice-operators` (стратегия ringall, участник PJSIP/200) |
| План набора (`extensions_custom.conf`) | **7777** эхо-тест · **7000** ИИ-агент (AudioSocket → `${EDUVOICE_BRIDGE}`, при сбое → операторы) · **300** операторы |
| Глобальная переменная | `EDUVOICE_BRIDGE=127.0.0.1:9092` (`globals_custom.conf`) |
| Python | uv 0.12 + Python 3.12 в `/opt/eduvoice` (системный 3.6 не трогаем) |
| Библиотеки | websockets 17.1, httpx 0.28, python-dotenv, numpy 2.2.6, soxr 0.5.0, webrtcvad-wheels |
| Служба | `eduvoice-bridge` (systemd, пользователь `eduvoice`), сейчас эхо-заглушка на 127.0.0.1:9092 |
| Проверка звука | Имитация звонка на 7000: 1 513 кадров / 30,26 с прошли в мост и обратно |

**Ограничения сервера, которые учитываем:**
- glibc 2.17 → **NumPy < 2.3, soxr < 1**, **onnxruntime не ставится** (значит, Silero VAD нет, берём `webrtcvad`).
- AudioSocket в Asterisk 20.4 передаёт **только звук 8 кГц**, без DTMF и переменных канала.
- Функция `CURL()` в Asterisk есть → через неё управляем звонком.
- `git` не установлен, `rsync` есть → выкладка через `rsync`.

---

## 3. Архитектура

```
Звонящий (софтфон / телефон)
      │ SIP
      ▼
┌────────────────── FreePBX / Asterisk ──────────────────┐
│ [eduvoice-entry]  Answer → IVR: «1 — ассистент, 0 — оператор»  │
│ [eduvoice-ai]     CURL POST /calls/{uuid}/start            │
│                MixMonitor (запись)                       │
│                Dial(AudioSocket/127.0.0.1:9092/uuid) ◄╗   │
│                CURL GET /calls/{uuid}/next → operator/hangup
│ [eduvoice-operators] Queue(eduvoice-operators) → PJSIP/200   ║   │
└───────────────────────────────────────────────────────╫───┘
                                                        ║ 8 кГц PCM, кадры 20 мс
┌──────────────────── eduvoice-bridge (Python 3.12) ───────╫───────────┐
│ control_api (HTTP :9093)   audiosocket server (:9092) ═╝           │
│        │                          │                                │
│        ▼                          ▼                                │
│   CallRegistry  ◄──────  CallSession (состояния, раздел 5)          │
│                           │   │    │                                │
│              VAD (webrtcvad)  │    └─ AudioOut: очередь 24→8 кГц,    │
│                               │       отправка строго по 20 мс,     │
│                               │       мгновенная очистка (barge-in) │
│         ┌─────────────────────┼─────────────────────┐               │
│         ▼                     ▼                     ▼               │
│    SpeechToText          Brain (LLM + FAQ)     TextToSpeech          │
│    (интерфейс)           (интерфейс)           (интерфейс)           │
└─────────┼─────────────────────┼─────────────────────┼───────────────┘
          ▼                     ▼                     ▼
      VoiceLab STT          LLM (VoiceLab         VoiceLab TTS
      wss …/stt/stream      или другая)           wss …/tts/stream
      ◄──────────── модуль eduvoice.voicelab — делает человек B ──────────►
```

### Структура репозитория

```
eduvoice/
├─ pyproject.toml / uv.lock
├─ .env.example
├─ Makefile                      deploy, logs, test, originate-test
├─ eduvoice/
│  ├─ config.py                  настройки из .env
│  ├─ audio.py                   ресемплинг, PCM-утилиты, кадры 20 мс        (A)
│  ├─ audiosocket.py             протокол AudioSocket (чтение/запись кадров) (A)
│  ├─ vad.py                     определение начала/конца фразы              (A)
│  ├─ session.py                 состояния звонка (раздел 5)                 (A)
│  ├─ brain.py                   решение: FAQ / ответ / оператор / прощание  (A)
│  ├─ control_api.py             HTTP для CURL из Asterisk                    (A)
│  ├─ registry.py                данные звонков, решения /next               (A)
│  ├─ interfaces.py              контракты STT/TTS/LLM (раздел 4)             (A+B, фиксируем в первый час)
│  ├─ fakes.py                   заглушки STT/TTS/LLM для разработки без ключа (A)
│  ├─ voicelab/                  ВСЁ ПРО VOICELAB                             (B)
│  │  ├─ client.py               авторизация, тикеты, ретраи
│  │  ├─ stt.py                  VoiceLabSpeechToText
│  │  ├─ tts.py                  VoiceLabTextToSpeech (+ пул соединений)
│  │  ├─ llm.py                  VoiceLabLLM (если выберем их LLM)
│  │  └─ cli.py                  проверки, бенчмарк, генерация аудиофраз
│  └─ main.py                    запуск AudioSocket + control API
├─ content/
│  ├─ faq.yaml                   частые вопросы, узбекский (раздел 8)
│  └─ prompts.yaml               тексты служебных фраз (приветствие и т.д.)
├─ audio/                        сгенерированные фразы (делает B), в git не кладём
├─ asterisk/                     копии конфигов: extensions_custom.conf и др.
├─ tests/
│  ├─ recordings/                тестовые вопросы голосом (wav)
│  └─ test_*.py
└─ deploy/eduvoice-bridge.service
```

### Стек и версии (проверено на сервере)

| Что | Версия | Зачем |
|---|---|---|
| Python | 3.12 (uv) | Мост |
| websockets | 17.1 | VoiceLab STT/TTS по WebSocket |
| httpx | 0.28 | REST VoiceLab, LLM |
| numpy | 2.2.6 (< 2.3!) | Работа со звуком |
| soxr | 0.5.0 (< 1!) | Ресемплинг 8↔16↔24 кГц |
| webrtcvad-wheels | последняя | VAD |
| python-dotenv | последняя | Настройки |
| sox (системный) | есть | Конвертация фраз для Asterisk |

### Выкладка

```bash
# с машины разработчика
rsync -az --delete --exclude .venv --exclude audio/cache ./ root@10.103.10.86:/opt/eduvoice/app/
ssh root@10.103.10.86 'cd /opt/eduvoice/app && chown -R eduvoice:eduvoice /opt/eduvoice && systemctl restart eduvoice-bridge'
ssh root@10.103.10.86 'journalctl -u eduvoice-bridge -f'
```

`.env` на сервере лежит в `/opt/eduvoice/app/.env` (права 600, владелец `eduvoice`), в `rsync` не перезаписывается.

---

## 4. Контракты между частями

**Это фиксируем в первый час, дальше A и B работают независимо.** A пишет логику на заглушках (`fakes.py`), B реализует те же интерфейсы через VoiceLab. В конце меняем одну строку в конфиге.

### 4.1 Формат звука

| Где | Формат |
|---|---|
| AudioSocket (Asterisk ↔ мост) | PCM signed 16-bit little-endian, mono, **8 000 Гц**, кадр 20 мс = **320 байт** |
| Вход VoiceLab STT | PCM s16le, mono, **16 000 Гц** |
| Выход VoiceLab TTS | PCM s16le, mono, **24 000 Гц** |
| Внутри моста между модулями | `bytes` PCM s16le mono + явная частота в имени поля |

Ресемплинг делает **A** в `audio.py` (`soxr`). Модуль B принимает и отдаёт звук в «родных» частотах VoiceLab.

### 4.2 Интерфейсы (`eduvoice/interfaces.py`)

```python
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

Language = Literal["uz", "ru"]


@dataclass(frozen=True)
class Transcript:
    text: str                      # "" если речи нет
    language: Language
    duration_ms: int
    latency_ms: int                # от commit до финального текста


class SpeechToText(Protocol):
    async def open(self, language: Language) -> None:
        """Готовит соединение заранее (в начале звонка)."""

    async def transcribe(self, pcm16k: bytes) -> Transcript:
        """Одна фраза 0,1–35 с, PCM s16le mono 16 кГц → финальный текст.
        Пустая речь → Transcript(text=""). Сетевые ошибки → SttError."""

    async def close(self) -> None: ...


class TextToSpeech(Protocol):
    def stream(self, text: str, language: Language) -> AsyncIterator[bytes]:
        """PCM s16le mono 24 кГц кусками по мере генерации.
        Отмена итератора (aclose) должна прерывать синтез (barge-in)."""

    async def warm_up(self, language: Language) -> None:
        """Держит готовое соединение, чтобы первая фраза не ждала TLS."""


@dataclass
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


class ChatModel(Protocol):
    async def complete_json(self, messages: list[ChatMessage], timeout_s: float) -> dict:
        """Ответ модели как JSON-объект (схема решения — раздел 5.3)."""


class SttError(Exception): ...
class TtsError(Exception): ...
class LlmError(Exception): ...
```

### 4.3 HTTP-канал управления для Asterisk (`control_api.py`, порт 9093, только 127.0.0.1)

| Метод и путь | Кто вызывает | Тело / ответ |
|---|---|---|
| `POST /calls/{uuid}/start` | Asterisk `CURL` перед AudioSocket | form: `caller=998901234567` → `ok` |
| `GET /calls/{uuid}/next` | Asterisk `CURL` после AudioSocket | `operator` \| `hangup` (текст без переносов). Неизвестный uuid → `operator` |
| `GET /calls/{uuid}/summary` | Панель оператора (если будет) | JSON: номер, реплики, краткое резюме |
| `GET /health` | Проверка | `{"status":"ok","voicelab":"ok|down"}` |

### 4.4 План набора (заменяет текущий `extensions_custom.conf`)

```ini
[from-internal-custom]
exten => 7777,1,Answer()
 same => n,Playback(demo-echotest)
 same => n,Echo()
 same => n,Hangup()
exten => 7000,1,Goto(eduvoice-entry,s,1)
exten => 300,1,Goto(eduvoice-operators,s,1)

[eduvoice-entry]
; IVR на узбекском: 1 — поговорить с ассистентом, 0 — сразу оператор.
; Нажатия клавиш через AudioSocket не проходят, поэтому выбор делается здесь, до ИИ.
exten => s,1,Answer()
 same => n,Set(TIMEOUT(digit)=3)
 same => n,Background(custom/eduvoice-ivr-menu)
 same => n,WaitExten(4)
exten => 1,1,Goto(eduvoice-ai,s,1)
exten => 0,1,Goto(eduvoice-operators,s,1)
exten => t,1,Goto(eduvoice-ai,s,1)       ; ничего не нажал — идём к ассистенту
exten => i,1,Goto(eduvoice-ai,s,1)

[eduvoice-ai]
; UUID и язык задаются здесь, чтобы в контекст можно было звонить напрямую (тесты, раздел 9)
exten => s,1,Answer()
 same => n,Set(EDUVOICE_UUID=${SHELL(uuidgen | tr -d '\n')})
 same => n,Set(CURLOPT(conntimeout)=1)
 same => n,Set(CURLOPT(httptimeout)=2)
 same => n,Set(EDUVOICE_OK=${CURL(http://127.0.0.1:9093/calls/${EDUVOICE_UUID}/start,caller=${CALLERID(num)})})
 same => n,MixMonitor(eduvoice/${STRFTIME(${EPOCH},,%Y%m%d)}/${EDUVOICE_UUID}.wav)
 same => n,Dial(AudioSocket/${EDUVOICE_BRIDGE}/${EDUVOICE_UUID},,g)   ; не AudioSocket(): см. журнал решений
 same => n,Set(EDUVOICE_NEXT=${CURL(http://127.0.0.1:9093/calls/${EDUVOICE_UUID}/next)})
 same => n,GotoIf($["${EDUVOICE_NEXT}" = "hangup"]?bye)
 same => n,Goto(eduvoice-operators,s,1)
 same => n(bye),Hangup()

[eduvoice-operators]
exten => s,1,Answer()
 same => n,Queue(eduvoice-operators,t,,,300)
 same => n,Hangup()
```

**Правило безопасности:** всё, кроме явного `hangup`, уходит оператору. Мост упал → звонок не висит в тишине.

---

## 5. Логика диалога

### 5.1 Состояния `CallSession`

| Состояние | Действие | Переход |
|---|---|---|
| CONNECT | Прочитать UUID (первый кадр 0x01), взять номер из registry, `stt.open()`, `tts.warm_up()` | → GREETING |
| GREETING | Играть готовую фразу `greeting` (с предупреждением о записи) | Речь человека → LISTEN (перебивание разрешено) · фраза закончилась → LISTEN |
| LISTEN | VAD: начало речи → копим звук (+300 мс до начала). Конец: тишина **700 мс** или **30 с** речи | Конец речи → TRANSCRIBE · 6 с тишины → `reprompt` · ещё 6 с → FAREWELL |
| TRANSCRIBE | 8→16 кГц, `stt.transcribe()` | Пустой текст → `repeat_please` → LISTEN (2 раза подряд → TRANSFER) |
| THINK | `brain.decide(text)`; если дольше **1,2 с** → играть `filler` | По решению → SPEAK / TRANSFER / FAREWELL |
| SPEAK | FAQ → готовое аудио; иначе ответ по предложениям → `tts.stream()` → 24→8 кГц → AudioOut | Перебили → очистить AudioOut, отменить TTS → LISTEN · договорили → `anything_else` → LISTEN |
| TRANSFER | Играть `transfer`, `registry.next = operator`, закрыть сокет (кадр 0x00) | Asterisk → очередь |
| FAREWELL | Играть `goodbye`, `registry.next = hangup`, закрыть сокет | Asterisk → Hangup |

**Кадр 0x00 от Asterisk (человек положил трубку) в любом состоянии** → отменить всё (STT, LLM, TTS), записать лог, `next = hangup`.

### 5.2 Определение речи (VAD)

- `webrtcvad`, режим агрессивности **2**, кадры **20 мс на 8 кГц** (320 байт).
- Начало речи: **≥ 3 речевых кадра из 5** (≈ 60 мс).
- Конец фразы: **35 кадров тишины подряд** (700 мс). Параметр в `.env`, подбираем на тестах.
- **Перебивание во время SPEAK:** ≥ **15 речевых кадров из 20** (≈ 300 мс) и **не раньше 500 мс** от начала ответа (защита от эха линии).

### 5.3 Решение (`brain.py`)

LLM возвращает только JSON. **Маршрут звонка выбирает код, а не модель.**

```json
{
  "intent": "faq | answer | operator | repeat | goodbye | unclear | out_of_scope",
  "faq_id": "transfer_university",
  "answer": "Oʻqishni koʻchirish uchun …",
  "language": "uz"
}
```

| Условие | Действие |
|---|---|
| `operator` | TRANSFER |
| `goodbye` | FAREWELL |
| `repeat` | Повторить последний ответ из памяти (без LLM и TTS) |
| `faq` и `faq_id` есть в `faq.yaml` | Играть **готовое аудио** ответа |
| `answer` | TTS по предложениям |
| `unclear` или `out_of_scope` | Фраза «не понял / отвечаю только по высшему образованию». 2 раза подряд → TRANSFER |
| LLM не ответила за **5 с** или ошибка | `tech_problem` → TRANSFER |
| > **8 вопросов** или > **6 минут** | Предложить оператора |

---

## 6. План работ человека A

| # | Задача | Оценка | Готово, когда |
|---|---|---|---|
| A1 | Репозиторий, `pyproject`, `config.py`, `interfaces.py` (**вместе с B, первый час**), `Makefile deploy/logs` | 1 ч | `make deploy` обновляет код на сервере и перезапускает службу |
| A2 | `audiosocket.py`: чтение кадров, запись кадров, отправка звука строго по 20 мс, очистка очереди | 2 ч | Эхо-тест через `originate` работает на новом коде |
| A3 | `audio.py`: 8↔16↔24 кГц через `soxr`, склейка и нарезка кадров + тесты | 1 ч | Тест: 1 с тона 440 Гц после 8→16→8 не искажён по длине |
| A4 | `vad.py` (webrtcvad) + тест на записанных фразах | 2 ч | На 10 тестовых записях конец фразы найден правильно |
| A5 | `fakes.py`: STT возвращает текст из файла, TTS — тон/тишину, LLM — фиксированный JSON | 1 ч | Вся цепочка работает без ключа VoiceLab |
| A6 | `control_api.py` + `registry.py` + новый план набора (4.4) + IVR-фраза меню (временная запись голосом) | 2 ч | `originate` → `/start` получает язык и номер, `/next` отдаёт решение |
| A7 | `session.py`: состояния 5.1 на заглушках | 4 ч | Имитация звонка проходит GREETING → LISTEN → THINK → SPEAK → FAREWELL |
| A8 | Перебивание (barge-in) | 2 ч | Тестовая запись «перебивает» ответ → бот замолкает ≤ 0,5 с |
| A9 | `brain.py` + `faq.yaml` + промпт (раздел 8) | 3 ч | 8/10 тестовых вопросов → правильный `intent`/`faq_id` |
| A10 | Перевод оператору + фраза + проверка очереди | 1 ч | «operator» → звонит 200 |
| A11 | **Интеграция модуля B** (замена fakes на voicelab) | 2 ч | Реальный звонок: вопрос голосом → ответ голосом |
| A12 | Журнал звонка: реплики, решения, задержки по этапам (JSON-lines) | 1 ч | Файл лога по каждому звонку |
| A13 | Настройка порогов VAD/задержки на живых звонках, отказоустойчивость (раздел 11) | 3 ч | Критерии раздела 1 выполнены |
| | **Итого A** | **≈ 25 ч** | |

**Зависимости от B:** A5 позволяет не ждать B. A11 требует B: V3, V4, V6 (и V7, если LLM от VoiceLab).

---

## 7. ТЗ: VoiceLab (человек B)

Актуальное и полное задание вынесено в отдельный документ:
**[docs/VOICELAB-TASKS.md](docs/VOICELAB-TASKS.md)** — контракт, 14 задач с критериями
приёмки, открытые вопросы, бюджет задержки и чек-лист сдачи.

Здесь оставлена только суть: человек B делает модуль `eduvoice/voicelab.py`, реализующий
три интерфейса из `eduvoice/interfaces.py` (распознавание, синтез, модель) через API
VoiceLab. Мост, телефония и CRM уже работают на заглушках — переключение одной
переменной `EDUVOICE_PROVIDER=voicelab`.

---

## 8. Контент: частые вопросы и промпт

### 8.1 `content/faq.yaml` (минимум 10, цель 30)

```yaml
- id: transfer_university
  keywords: [koʻchirish, perevod, перевод, boshqa universitet]
  answer: "Boshqa oliy taʼlim muassasasiga oʻqishni koʻchirish uchun …"   # проверить по lex.uz
  source: "Nizom nomi, band raqami"
```

Все ответы только на узбекском. Темы для первой десятки: перевод в другой вуз · восстановление · академический отпуск · отчисление · стипендия · оплата контракта и рассрочка · перевод с контракта на грант · признание иностранного диплома · сроки приёма документов · как связаться с оператором/часы работы.

**Ответы проверять по официальным документам на lex.uz.** Непроверенный ответ не озвучивать.

### 8.2 Системный промпт (`brain.py`)

Правила, которые обязательно в промпте:
1. Ты ИИ-оператор колл-центра Министерства высшего образования. Отвечай **только** по вопросам высшего образования.
2. Сначала попробуй сопоставить вопрос с одним из FAQ (список id и кратких описаний) → `intent: faq`.
3. Если FAQ не подходит и уверенного ответа нет → `intent: unclear` (не придумывать).
4. Ответ — **не больше 2–3 коротких предложений**, для голоса: без списков, скобок, ссылок; числа словами.
5. Отвечай только на узбекском.
6. Просьба оператора в любой форме → `intent: operator`.
7. Вывод — **только JSON** по схеме 5.3.

### 8.3 Служебные фразы (`content/prompts.yaml`, только узбекский, генерирует B)

`ivr_menu` · `greeting` (с «suhbat yozib olinadi») · `anything_else` · `filler` (2–3 варианта) · `repeat_please` · `not_understood` · `out_of_scope` · `reprompt` · `transfer` · `tech_problem` · `goodbye`.

---

## 9. Тестирование без телефонов

**Главная идея:** вопрос «звонящего» — это записанный WAV, который Asterisk проигрывает в звонок на 7000. Так весь цикл проверяется одной командой.

Тестовый контекст в `extensions_custom.conf` — «звонящий», который ждёт приветствие, задаёт записанный вопрос и слушает ответ:

```ini
[eduvoice-test]
; extension q<имя файла>: пауза на приветствие → вопрос → ждём ответ → кладём трубку
exten => _q.,1,Answer()
 same => n,Wait(5)
 same => n,Playback(custom/${EXTEN:1})
 same => n,Wait(15)
 same => n,Hangup()
```

```bash
# 1. Положить тестовую фразу в звуки Asterisk (8 кГц)
sox question-uz-1.wav -r 8000 -c 1 -b 16 /var/lib/asterisk/sounds/en/custom/test-q-uz-1.wav

# 2. Соединить «звонящего» (eduvoice-test) с ИИ-агентом (eduvoice-ai, язык по умолчанию uz)
asterisk -rx "channel originate Local/s@eduvoice-ai/n extension qtest-q-uz-1@eduvoice-test"

# 3. Смотреть лог
journalctl -u eduvoice-bridge -f
```

| Уровень | Что | Где |
|---|---|---|
| Юнит (A) | кадры AudioSocket, ресемплинг, VAD на записях, решения `brain` на фиктивных JSON | `tests/test_audio.py`, `test_vad.py`, `test_brain.py` |
| Юнит (B) | STT/TTS клиенты на записях, обработка ошибок | `tests/test_voicelab_*.py` |
| Интеграция | `originate` + записанный вопрос → в логе: текст, решение, задержки | `make originate-test` |
| Живой звонок | Софтфон 101 → 7000; оператор на 200 | Раздел 12 |
| Отказ | `systemctl stop eduvoice-bridge` во время звонка → звонок уходит на 200 | Раздел 1, критерий 5 |

**Метрики из журнала звонка:** время конца речи → текст STT, текст → решение, решение → первый звук, итого. Для каждого звонка и в сумме (p50/p95).

---

## 10. Расписание по дням

| Время | Человек A | Человек B |
|---|---|---|
| **День 1, утро** | A1 (вместе с B: интерфейсы), A2, A3 | V1, V2 (открытые вопросы) |
| **День 1, день** | A4, A5, A6 | V3 (STT), V5 (голоса) |
| **День 1, вечер** | A7 на заглушках | V4 (TTS), V6 (служебные фразы) |
| ✅ **Итог дня 1** | Имитация звонка проходит все состояния на заглушках | STT и TTS работают по файлам, фразы на сервере |
| **День 2, утро** | A8 (перебивание), A9 (brain + FAQ) | V8 (бенчмарк), V9 (LLM) |
| **День 2, день** | **A11 интеграция с модулем B** | V7 (FAQ-аудио), V10 (отказоустойчивость), помощь в A11 |
| **День 2, вечер** | A10 (оператор), A12 (журнал) | Живые тестовые звонки, правка произношения |
| ✅ **Итог дня 2** | **MVP: живой звонок → вопрос → ответ → оператор** | |
| **День 3** | A13 (пороги, задержка), отказоустойчивость | Прогон 20 тестовых вопросов, итоговые цифры для слайда |
| | Демо: 3 репетиции, запасное видео | |

---

## 11. Риски и запасные варианты

| Риск | Признак | Что делаем |
|---|---|---|
| Задержка > 3 с | Бенчмарк V8, журнал A12 | Больше FAQ с готовым аудио, короче ответы, фраза-заполнитель раньше (0,8 с), быстрая LLM |
| STT плохо понимает телефонный звук | V8: много ошибок на записях через софтфон | Софтфон на широкополосном кодеке для демо; говорить чётче; ключевые слова FAQ |
| Нельзя переиспользовать WebSocket | V2, вопрос 1 | Пул заранее открытых соединений |
| Лимит LLM «2 генерации» | Одновременные звонки ждут | Семафор + FAQ без LLM по ключевым словам (`keywords` в faq.yaml) |
| Кредиты кончились | 402 `insufficient_credits` | Пополнить заранее, фразы и FAQ озвучены один раз |
| VoiceLab недоступен | `SttError`/`TtsError` | `tech_problem` → оператор (фраза уже готова файлом) |
| Эхо запускает перебивание | Бот замолкает сам | Поднять порог, 500 мс защиты, режим VAD 3 |
| Нет сети на площадке | — | Мобильный интернет; VPN к серверу; запасное видео |
| Мост упал | — | systemd `Restart=on-failure`; план набора уводит звонок оператору |

---

## 12. Демо и чек-лист

**Сценарий (3 минуты):**
1. Звоним с телефона на 7000, в меню нажимаем «1».
2. «Boshqa universitetga oʻqishni qanday koʻchirsam boʻladi?» → ответ голосом.
3. Перебиваем бота на середине → бот замолкает и слушает.
4. Второй вопрос — про стипендию → ответ.
5. «Operator bilan gaplashmoqchiman» → звонит ноутбук оператора.
6. Слайд: задержка p50, доля вопросов без оператора, «голос не уходит за пределы Узбекистана».

**Чек-лист перед защитой:**
- [ ] Все 6 критериев раздела 1 выполнены на живых звонках
- [ ] 20 тестовых вопросов прогнаны, цифры на слайде
- [ ] Отказ моста → оператор проверен
- [ ] Баланс VoiceLab пополнен
- [ ] Софтфоны настроены на 2 устройствах + ноутбук оператора
- [ ] Сеть до сервера с площадки проверена (или VPN)
- [ ] Запасное видео записано
- [ ] `.env` и пароли не попали в репозиторий

---

## 13. Что нужно от команды

1. **API-ключ VoiceLab** с правами STT, TTS (включая realtime), LLM, voices — нужен человеку B в первый час.
2. **Кто A, кто B** (и есть ли третий человек — ему: контент FAQ по lex.uz + тестовые записи голосом).
3. **Дата и время защиты.**
4. **Будет ли сервер 10.103.10.86 доступен с площадки** (или нужен VPN).
5. **Софтфоны:** 2 телефона + ноутбук для оператора.

**Источники:** [docs.voicelab.uz](https://docs.voicelab.uz/) · [аутентификация](https://docs.voicelab.uz/api/authentication) · [STT](https://docs.voicelab.uz/api/stt) · [STT realtime](https://docs.voicelab.uz/api/realtime-stt) · [TTS](https://docs.voicelab.uz/api/tts) · [LLM](https://docs.voicelab.uz/api/llm) · [ошибки и лимиты](https://docs.voicelab.uz/api/errors) · [AVA AI Voice Agent: FreePBX](https://github.com/hkjarral/AVA-AI-Voice-Agent-for-Asterisk/blob/main/docs/FreePBX-Integration-Guide.md)
