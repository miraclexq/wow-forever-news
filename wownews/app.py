from __future__ import annotations

import argparse
import fcntl
import html
import json
import logging
import queue
import signal
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit

from .core import Settings, Store, canonical, fits_caption, keyboard, render_post, render_preview, utf16_len
from .services import APIError, Editor, Pipeline, Telegram
from .sources import Document, FetchError, Fetcher, article_body, discover, normalized, validate_url

log = logging.getLogger("wownews")


HELP = """Команды редактора:
/status — состояние источников и расход запросов
/scan — проверить источники
/queue — очередь черновиков
/show 12 — показать черновик
/add URL — подготовить пост по ссылке вручную
/evidence 12 — опорные факты и цитаты
/autopost off — выключить автопостинг
/autopost on — только официальные новости, если разрешено в .env
/resolve 12 posted — пост с неизвестным статусом уже вышел
/resolve 12 retry — проверил канал, поста нет, вернуть в очередь
/cancel — отменить правку
/whoami — мой Telegram ID

Новость публикуется только после кнопки «Опубликовать», пока автопостинг выключен.
Сначала нажми /start в личке у бота, чтобы он мог присылать сообщения."""


class App:
    def __init__(self, settings, store, telegram=None):
        self.settings, self.store = settings, store
        self.tg = telegram or Telegram(settings.token)
        self.fetcher = Fetcher()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.jobs = queue.Queue(maxsize=30)
        self.pipeline = Pipeline(settings, store, self.fetcher, Editor(settings, store),
                                 self.publish, self.deliver)

    def channel_check(self):
        if not self.settings.channel:
            raise APIError("Не заполнен PUBLISH_CHANNEL_ID в .env")
        bot = self.tg.call("getMe")
        chat = self.tg.call("getChat", chat_id=self.settings.channel)
        if chat["type"] != "channel":
            raise APIError("Для публикации нужен Telegram-канал")
        member = self.tg.call("getChatMember", chat_id=self.settings.channel, user_id=bot["id"])
        if member["status"] != "administrator" or not member.get("can_post_messages"):
            raise APIError("Добавь бота администратором канала с правом публикации")

    def publish(self, draft_id, revision, automatic=False):
        self.channel_check()
        if automatic and (not self.settings.allow_auto or self.store.get("autopost") != "official"):
            raise APIError("Автопостинг уже выключен")
        if not self.store.claim_publish(draft_id, revision):
            raise APIError("Этот черновик уже обработан или изменился. Открой /show заново")
        row = self.store.row(draft_id)
        try:
            sent, _ = self.send_article(self.settings.channel, row, render_post(row))
        except APIError as exc:
            self.store.execute("UPDATE drafts SET state=? WHERE id=?",
                               ("unknown" if exc.uncertain else "draft", draft_id))
            if exc.uncertain:
                self.store.set("last_problem",
                    f"Статус публикации #{draft_id} неизвестен. Проверь канал и используй /resolve")
            raise
        self.store.execute("UPDATE drafts SET state='published', public_message=? WHERE id=?",
                           (sent["message_id"], draft_id))
        return sent

    def send_article(self, chat, row, text, **kwargs):
        if not row.get("image_url"):
            raise APIError("Нет проверенной иллюстрации; отправка отложена")
        if not fits_caption(text):
            raise APIError("Текст не помещается в подпись к фото; сократи черновик")
        try:
            image = self.fetcher.image(row["image_url"], row["source_url"])
        except FetchError:
            raise APIError("Иллюстрация источника недоступна; отправка отложена") from None
        sent = self.tg.send_photo(chat, image, caption=text,
                                  reply_markup=kwargs.get("reply_markup"))
        return sent, True

    def deliver(self, draft_id):
        row = self.store.row(draft_id)
        if not row or not self.settings.destination:
            return
        claimed = self.store.execute(
            "UPDATE drafts SET delivery='sending' WHERE id=? AND delivery='new'", (draft_id,))
        if claimed.rowcount != 1:
            return
        try:
            sent, is_photo = self.send_article(self.settings.destination, row, render_preview(row),
                                               reply_markup=keyboard(row))
            self.store.execute("""UPDATE drafts SET delivery='sent', review_chat=?, review_message=?,
                review_is_photo=? WHERE id=?""", (str(sent["chat"]["id"]), sent["message_id"],
                                                int(is_photo), draft_id))
        except APIError as exc:
            self.store.execute("UPDATE drafts SET delivery=? WHERE id=?",
                               ("unknown" if exc.uncertain else "new", draft_id))
            if not exc.uncertain:
                self.store.set("delivery_retry:" + str(draft_id), time.time() + 900)
            self.store.set("last_problem", f"Черновик #{draft_id}: {exc}; /show {draft_id}")

    def refresh(self, draft_id):
        row = self.store.row(draft_id)
        if row and row["review_chat"] and row["review_message"]:
            try:
                preview = render_preview(row)
                if row["review_is_photo"] and not fits_caption(preview):
                    try:
                        self.tg.call("editMessageReplyMarkup", chat_id=row["review_chat"],
                                     message_id=row["review_message"],
                                     reply_markup={"inline_keyboard": []})
                    except APIError:
                        pass  # Old revision callbacks are rejected by the database.
                    sent = self.tg.send(row["review_chat"], preview,
                                        reply_parameters={"message_id": row["review_message"]},
                                        reply_markup=keyboard(row))
                    self.store.execute("UPDATE drafts SET review_message=?, review_is_photo=0 WHERE id=?",
                                       (sent["message_id"], draft_id))
                else:
                    is_photo = bool(row["review_is_photo"])
                    self.tg.call("editMessageCaption" if is_photo else "editMessageText",
                                 chat_id=row["review_chat"], message_id=row["review_message"],
                                 **({"caption": preview} if is_photo else {"text": preview,
                                         "link_preview_options": {"is_disabled": True}}),
                                 parse_mode="HTML", reply_markup=keyboard(row))
            except APIError:
                pass  # A deleted or old review message does not affect the saved draft.

    def reply(self, message, text):
        return self.tg.send(message["chat"]["id"], html.escape(text))

    def callback(self, callback):
        if callback.get("from", {}).get("id") not in self.settings.admins:
            self.tg.call("answerCallbackQuery", callback_query_id=callback["id"], text="Нет доступа")
            return
        self.tg.call("answerCallbackQuery", callback_query_id=callback["id"], text="Обрабатываю")
        message = callback.get("message")
        if not message:
            return
        try:
            action, raw_id, raw_revision = callback.get("data", "").split(":")
            draft_id, revision = int(raw_id), int(raw_revision)
            row = self.store.row(draft_id)
            if not row or row["revision"] != revision or row["state"] != "draft":
                self.reply(message, "Черновик уже обработан или изменился. Открой /show заново")
                return
            if action == "p":
                self.publish(draft_id, revision)
            elif action == "s":
                self.store.execute("UPDATE drafts SET state='skipped' WHERE id=? AND state='draft'", (draft_id,))
            elif action == "e":
                user_id = callback["from"]["id"]
                previous = self.store.one("SELECT * FROM edit_sessions WHERE user_id=?", (user_id,))
                if previous:
                    self.reply(message, "Сначала закончи текущую правку или отправь /cancel")
                    return
                prompt = self.tg.send(user_id,
                    f"Правка #{draft_id}. Ответь на это сообщение полным текстом: "
                    "первая строка — заголовок, затем пустая строка и текст. "
                    "Ссылку на источник бот добавит сам. Вся подпись — до 1024 символов. Отмена: /cancel",
                    reply_markup={"force_reply": True, "selective": True})
                self.store.execute("UPDATE drafts SET state='editing' WHERE id=? AND state='draft'", (draft_id,))
                self.store.execute("INSERT INTO edit_sessions VALUES(?,?,?,?,?)",
                    (user_id, str(user_id), prompt["message_id"], draft_id, time.time() + 3600))
            self.refresh(draft_id)
        except (ValueError, APIError) as exc:
            self.reply(message, str(exc) if isinstance(exc, APIError) else "Некорректная кнопка")

    def edit_reply(self, message, session):
        if (str(message["chat"]["id"]) != session["chat_id"] or
                message.get("reply_to_message", {}).get("message_id") != session["prompt_id"]):
            self.reply(message, "Ответь именно на сообщение с запросом правки. Отмена: /cancel")
            return
        text = message.get("text", "").strip()
        title, sep, body = text.partition("\n")
        body = body.strip()
        if not sep or not title or not body or utf16_len(title) > 120 or utf16_len(body) > 1024:
            self.reply(message, "Нужны заголовок до 120 символов и подпись к фото до 1024 символов")
            return
        row = self.store.row(session["draft_id"])
        if not fits_caption(render_post({**row, "title": title, "body": body})):
            self.reply(message, "Текст вместе с источником не помещается в подпись к фото")
            return
        self.store.execute("""UPDATE drafts SET title=?, body=?, revision=revision+1,
            needs_review=1, notes=?, state='draft' WHERE id=? AND state='editing'""",
            (title, body, json.dumps(["Отредактировано вручную"], ensure_ascii=False), session["draft_id"]))
        self.store.execute("DELETE FROM edit_sessions WHERE user_id=?", (session["user_id"],))
        self.refresh(session["draft_id"])
        self.reply(message, f'Правка #{session["draft_id"]} сохранена')

    def handle_message(self, message):
        user_id = message.get("from", {}).get("id")
        text = message.get("text", "")
        command, _, arg = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        if command in {"/whoami", "/start"} and message["chat"]["type"] == "private":
            self.reply(message, f"Твой Telegram ID: {user_id}\n" +
                       (HELP if user_id in self.settings.admins else
                        "Добавь этот номер в ADMIN_USER_IDS в .env на сервере и перезапусти сервис"))
            return
        if user_id not in self.settings.admins:
            return
        # Avoid group command spoofing and publish through a forwarded bot keyboard.
        if message["chat"]["type"] not in {"private", "supergroup", "group"}:
            return
        session = self.store.one("SELECT * FROM edit_sessions WHERE user_id=?", (user_id,))
        if command == "/cancel" and session:
            self.store.execute("UPDATE drafts SET state='draft' WHERE id=? AND state='editing'",
                               (session["draft_id"],))
            self.store.execute("DELETE FROM edit_sessions WHERE user_id=?", (user_id,))
            self.refresh(session["draft_id"])
            self.reply(message, "Правка отменена")
            return
        if session and session["expires"] < time.time():
            self.store.execute("UPDATE drafts SET state='draft' WHERE id=? AND state='editing'",
                               (session["draft_id"],))
            self.store.execute("DELETE FROM edit_sessions WHERE user_id=?", (user_id,))
            session = None
        if session and not command.startswith("/"):
            self.edit_reply(message, session)
            return
        try:
            if command in {"/help", "/start"}:
                self.reply(message, HELP)
            elif command == "/status":
                day = datetime.now(self.settings.zone).date().isoformat()
                usage = self.store.one("SELECT * FROM usage WHERE day=?", (day,)) or {}
                status = [
                    "Режим: " + self.store.get("autopost", "черновики"),
                    "Последняя проверка: " + self.store.get("last_scan", "ещё не было"),
                    f'Запросов сегодня: {usage.get("calls", 0)}/{self.settings.daily_limit}',
                    f'Токены: вход {usage.get("input_tokens", 0)}, выход {usage.get("output_tokens", 0)}',
                    "Ключ модели: " + ("не требуется (OpenCode)" if getattr(self.settings, "provider", "") == "opencode"
                                       else ("задан" if self.settings.api_key else "не задан")),
                    "Модель: " + getattr(self.settings, "provider", "openai") + " / " + self.settings.model,
                    "Последнее замечание: " + self.store.get("last_problem", "нет"),
                ]
                for source in self.settings.sources:
                    status.append(source["name"] + ": " + self.store.get("source:" + source["name"], "не проверен"))
                if getattr(self.settings, "provider", "") == "opencode":
                    status.append("Токены OpenCode не подсчитываются в этом статусе")
                self.reply(message, "\n".join(status))
            elif command == "/scan":
                self.wake.set()
                self.reply(message, "Проверка поставлена в очередь")
            elif command == "/queue":
                rows = self.store.all("""SELECT id,title,state FROM drafts
                    WHERE state IN ('draft','editing','unknown') ORDER BY id DESC LIMIT 30""")
                self.reply(message, "\n".join(f'#{r["id"]} [{r["state"]}] {r["title"]}' for r in rows)
                           or "Очередь пуста")
            elif command in {"/show", "/evidence"}:
                row = self.store.row(int(arg))
                if not row:
                    raise APIError("Черновик не найден")
                if command == "/show":
                    sent, is_photo = self.send_article(message["chat"]["id"], row, render_preview(row),
                                                       reply_markup=keyboard(row))
                    self.store.execute("""UPDATE drafts SET review_chat=?, review_message=?,
                        review_is_photo=?, delivery='sent' WHERE id=?""",
                        (str(message["chat"]["id"]), sent["message_id"], int(is_photo), row["id"]))
                else:
                    evidence = json.loads(row["evidence"])
                    summary = "\n\n".join(i["claim"] + "\nОригинал: " + i["quote"] for i in evidence)
                    self.reply(message, summary[:3300] or "Опорные цитаты отсутствуют")
            elif command == "/add":
                link = canonical(arg.strip())
                source = next((s for s in self.settings.sources if urlsplit(link).hostname in s["allowed_hosts"]), None)
                if not source:
                    raise APIError("Разрешены только домены из config.json. Новые источники добавляются в конфиг")
                validate_url(link, source["allowed_hosts"], resolve=False)
                self.jobs.put_nowait((link, source))
                self.wake.set()
                self.reply(message, "Ссылка добавлена на обработку")
            elif command == "/autopost":
                if arg.strip() == "off":
                    self.store.set("autopost", "off")
                    self.reply(message, "Автопостинг выключен")
                elif arg.strip() == "on":
                    if not self.settings.allow_auto:
                        raise APIError("Сначала установи ALLOW_AUTOPOST=true в .env и перезапусти сервис")
                    self.channel_check()
                    self.store.set("autopost", "official")
                    self.reply(message, "Включена публикация новых официальных новостей Blizzard после второй проверки. "
                               "Слухи, датамайн, неизвестные названия и обновления статей остаются в черновиках")
                else:
                    self.reply(message, "Используй /autopost on или /autopost off")
            elif command == "/resolve":
                raw_id, decision = arg.split()
                draft_id = int(raw_id)
                row = self.store.row(draft_id)
                if not row or row["state"] != "unknown" or decision not in {"posted", "retry"}:
                    raise APIError("Нужен черновик с неизвестным статусом и posted либо retry")
                self.store.execute("UPDATE drafts SET state=? WHERE id=? AND state='unknown'",
                                   ("published" if decision == "posted" else "draft", draft_id))
                self.refresh(draft_id)
                self.reply(message, "Статус обновлён. Возврат в очередь сам по себе пост не публикует")
            else:
                self.reply(message, HELP)
        except (ValueError, FetchError, APIError, queue.Full) as exc:
            self.reply(message, str(exc) if isinstance(exc, (APIError, FetchError))
                       else "Проверь параметры команды. Справка: /help")

    def worker(self):
        next_scan = 0
        while not self.stop.is_set():
            due = time.monotonic() >= next_scan or self.wake.is_set()
            self.wake.clear()
            if self.settings.admins and getattr(self.settings, "model_ready", bool(self.settings.api_key)):
                try:
                    while not self.jobs.empty() and not self.stop.is_set():
                        link, source = self.jobs.get_nowait()
                        markup = self.fetcher.text(link, source["allowed_hosts"])
                        title = next((normalized(n.text()) for n in Document(markup).root.walk()
                                      if n.tag == "h1"), "WoW Forever")
                        self.pipeline.process({"url": link, "title": title, "source": source["name"]},
                                              source, force=True)
                    if due:
                        self.pipeline.scan()
                        next_scan = time.monotonic() + self.settings.interval
                    for row in self.store.all("SELECT id FROM drafts WHERE delivery='new' ORDER BY id LIMIT 10"):
                        if float(self.store.get("delivery_retry:" + str(row["id"]), "0")) <= time.time():
                            self.deliver(row["id"])
                except Exception as exc:
                    # Keep this thread alive without leaking secrets from HTTP exceptions.
                    log.error("Фоновая обработка приостановлена: %s", type(exc).__name__)
                    self.store.set("last_problem", "Ошибка фоновой обработки: " + type(exc).__name__)
                    next_scan = time.monotonic() + 300
            self.stop.wait(2)

    def run(self):
        hook = self.tg.call("getWebhookInfo")
        if hook.get("url"):
            raise APIError("У бота уже настроен webhook. Используй отдельного бота или отключи прежнюю интеграцию")
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop.set())
        thread = threading.Thread(target=self.worker, daemon=True)
        thread.start()
        log.info("Бот запущен. Администраторов: %s. Автопостинг требует отдельного включения", len(self.settings.admins))
        while not self.stop.is_set():
            try:
                offset = int(self.store.get("telegram_offset", "0"))
                updates = self.tg.call("getUpdates", offset=offset, timeout=20,
                                      allowed_updates=["message", "callback_query"])
                for update in updates:
                    try:
                        if "callback_query" in update:
                            self.callback(update["callback_query"])
                        elif "message" in update:
                            self.handle_message(update["message"])
                    except Exception as exc:
                        log.error("Ошибка обработки команды: %s", type(exc).__name__)
                    self.store.set("telegram_offset", update["update_id"] + 1)
            except APIError:
                log.warning("Telegram временно недоступен; следующая попытка через 10 секунд")
                self.stop.wait(10)
        thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description="Редактор новостей WoW Forever")
    parser.add_argument("--check-sources", action="store_true",
                        help="проверить источники и извлечение текста, без Telegram и модели")
    parser.add_argument("--check-model", action="store_true",
                        help="проверить JSON-ответ модели без Telegram и публикаций")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings()
    if args.check_sources:
        fetcher = Fetcher()
        failed = 0
        for source in settings.sources:
            try:
                articles = discover(fetcher, source)
                body = article_body(fetcher.text(articles[0]["url"], source["allowed_hosts"]))
                print(f'{source["name"]}: OK; ссылок {len(articles)}; текст {len(body)} символов')
                print("  " + articles[0]["url"])
            except (FetchError, ValueError) as exc:
                failed += 1
                print(f'{source["name"]}: ERROR; {exc}')
        return 1 if failed else 0
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with (settings.data_dir / "instance.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Бот уже запущен из этой папки данных")
        store = Store(settings.data_dir / "news.sqlite3")
        try:
            if args.check_model:
                schema = {"type": "object", "properties": {"status": {"type": "string", "enum": ["ok"]}},
                          "required": ["status"], "additionalProperties": False}
                value = Editor(settings, store).request(
                    "connection_check", schema,
                    'Проверка соединения. Верни ровно JSON {"status":"ok"}.',
                    {"task": "connection_check"})
                if value != {"status": "ok"}:
                    raise APIError("Модель не прошла проверку JSON")
                print("Модель: " + settings.provider + " / " + settings.model)
                print("JSON-проверка: OK")
            else:
                App(settings, store).run()
        except (APIError, ValueError) as exc:
            raise SystemExit(str(exc)) from None
    return 0
