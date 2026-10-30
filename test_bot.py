import copy
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from wownews.app import App, fits_caption
from wownews.core import ROOT, Settings, Store, canonical, digest, keyboard, load_env, render_post, render_preview, utf16_len
from wownews.services import APIError, BudgetError, Editor, Pipeline, validate_result
from wownews.sources import FetchError, article_body, discover, forever_spell_names, image_from_markup, launch_times, parse_feed, validate_url


BODY = ("WoW Forever synthetic test: The cooldown of Example Spell changes from 20 seconds "
        "to 18 seconds. This text is fictional and is used only for automated tests. " * 3)
MARKUP = ('<html><meta property="og:image" content="https://bnetcmsus-a.akamaihd.net/cms/blog_header/story.jpg">'
          '<nav>Unrelated retail advertisement</nav><article><h1>Test</h1><p>' + BODY + '</p></article></html>')
TEST_IMAGE = 'https://bnetcmsus-a.akamaihd.net/cms/blog_header/story.jpg'
SOURCE = {"name": "Blizzard", "url": "https://news.blizzard.com/en-us/feed/world-of-warcraft",
          "allowed_hosts": ["news.blizzard.com"], "official": True,
          "dedicated": False, "track_updates": 1, "type": "listing"}
ARTICLE = {"url": "https://news.blizzard.com/en-us/article/1/synthetic",
           "title": "Synthetic WoW Forever news", "source": "Blizzard"}


def result():
    return {"relevant": True, "title": "Тестовое изменение",
            "body": "Учебный пример: кулдаун изменён с 20 до 18 секунд.",
            "kind": "official", "needs_review": False, "review_notes": [],
            "unknown_terms": [], "duplicate_of": None,
            "evidence": [{"claim": "Кулдаун 18 секунд", "quote": "from 20 seconds to 18 seconds"}]}


def settings():
    return SimpleNamespace(
        token="1:TEST", admins={42}, api_key="test-key", model="test-model",
        destination="42", channel="@test_channel", allow_auto=False,
        sources=[copy.deepcopy(SOURCE)], daily_limit=2, max_scan=10,
        interval=900, zone=ZoneInfo("Europe/Moscow"), terms={}, editorial="test",
    )


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.photos = []
        self.calls = []
        self.publish_error = None

    def send(self, chat, text, **kwargs):
        if chat == "@test_channel" and self.publish_error:
            raise self.publish_error
        self.sent.append((chat, text, kwargs))
        return {"message_id": len(self.sent), "chat": {"id": chat}}

    def send_photo(self, chat, image, **kwargs):
        if chat == "@test_channel" and self.publish_error:
            raise self.publish_error
        self.photos.append((chat, image, kwargs))
        return {"message_id": 100 + len(self.photos), "chat": {"id": chat}}

    def call(self, method, **payload):
        self.calls.append((method, payload))
        if method == "getMe":
            return {"id": 1}
        if method == "getChat":
            return {"type": "channel"}
        if method == "getChatMember":
            return {"status": "administrator", "can_post_messages": True}
        return True


class BotTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.cfg = settings()
        self.tg = FakeTelegram()
        self.app = App(self.cfg, self.store, self.tg)
        self.app.fetcher.image = lambda *_: b'jpeg'

    def tearDown(self):
        self.store.db.close()

    def make_draft(self):
        return self.store.draft(ARTICLE, digest(BODY), result(), TEST_IMAGE)

    def test_only_publisher_image_on_known_cdn_is_selected(self):
        good = '<meta property="og:image" content="https://bnetcmsus-a.akamaihd.net/cms/blog_header/story.jpg">'
        bad = '<meta property="og:image" content="https://untrusted.example/cms/blog_header/story.jpg">'
        self.assertEqual(image_from_markup(bad + good, ARTICLE['url']),
                         'https://bnetcmsus-a.akamaihd.net/cms/blog_header/story.jpg')
        self.assertEqual(image_from_markup(bad, ARTICLE['url']), '')

    def test_short_preview_is_one_photo_with_caption_and_buttons(self):
        url = 'https://bnetcmsus-a.akamaihd.net/cms/blog_header/story.jpg'
        draft_id = self.store.draft(ARTICLE, digest(BODY), result(), url)
        self.app.fetcher.image = lambda *_: b'jpeg'
        self.app.deliver(draft_id)
        self.assertEqual(len(self.tg.photos), 1)
        self.assertEqual(self.tg.photos[0][0:2], ('42', b'jpeg'))
        self.assertIn('reply_markup', self.tg.photos[0][2])
        self.assertIn('Источник:', self.tg.photos[0][2]['caption'])
        self.assertEqual(self.tg.sent, [])
        self.assertEqual(self.store.row(draft_id)['delivery'], 'sent')
        self.assertEqual(self.store.row(draft_id)['review_is_photo'], 1)

    def test_long_caption_is_not_sent_or_truncated(self):
        url = 'https://bnetcmsus-a.akamaihd.net/cms/blog_header/story.jpg'
        post = result()
        post['body'] = 'Полный текст. ' * 110
        draft_id = self.store.draft(ARTICLE, digest(BODY), post, url)
        self.app.fetcher.image = lambda *_: b'jpeg'
        self.app.deliver(draft_id)
        self.assertEqual(self.tg.photos, [])
        self.assertEqual(self.tg.sent, [])
        self.assertEqual(self.store.row(draft_id)['delivery'], 'new')

    def test_missing_image_never_delivers_a_text_only_news_post(self):
        draft_id = self.store.draft(ARTICLE, digest(BODY), result())
        self.app.deliver(draft_id)
        self.assertEqual(self.tg.photos, [])
        self.assertEqual(self.tg.sent, [])
        self.assertEqual(self.store.row(draft_id)['delivery'], 'new')
        self.assertGreater(float(self.store.get('delivery_retry:' + str(draft_id))), 0)

    def test_ambiguous_photo_upload_marks_delivery_unknown(self):
        url = 'https://bnetcmsus-a.akamaihd.net/cms/blog_header/story.jpg'
        draft_id = self.store.draft(ARTICLE, digest(BODY), result(), url)
        self.app.fetcher.image = lambda *_: b'jpeg'
        def fail(chat, photo, **kwargs):
            raise APIError('Telegram failed', uncertain=True)
        self.tg.send_photo = fail
        self.app.deliver(draft_id)
        self.assertEqual(self.store.row(draft_id)['delivery'], 'unknown')
        self.assertEqual(len(self.tg.photos), 0)

    def test_manual_publication_sends_photo_then_full_text_once(self):
        url = 'https://bnetcmsus-a.akamaihd.net/cms/blog_header/story.jpg'
        draft_id = self.store.draft(ARTICLE, digest(BODY), result(), url)
        self.app.fetcher.image = lambda *_: b'jpeg'
        self.app.publish(draft_id, 1)
        self.assertEqual(len(self.tg.photos), 1)
        self.assertEqual(self.tg.photos[0][0:2], ('@test_channel', b'jpeg'))
        self.assertIn('Источник:', self.tg.photos[0][2]['caption'])
        self.assertEqual(self.tg.sent, [])
        self.assertEqual(self.store.row(draft_id)['state'], 'published')
        with self.assertRaises(APIError):
            self.app.publish(draft_id, 1)
        self.assertEqual(len(self.tg.photos), 1)

    def test_caption_length_counts_visible_html_and_emojis(self):
        self.assertTrue(fits_caption('<b>Новость ✨</b> &amp; подробности'))
        self.assertTrue(fits_caption('<b>' + 'А' * 1024 + '</b>'))
        self.assertFalse(fits_caption('<b>' + 'А' * 1025 + '</b>'))
        self.assertFalse(fits_caption('🚀' * 513))

    def test_launch_time_comes_from_utc_and_current_year_not_wrong_cst_label(self):
        body = ('Europe (CST) | UTC\n12:00 a.m. | 23:00\nNovember 5 | November 4*')
        times = launch_times(body, datetime(2026, 10, 30, tzinfo=timezone.utc))
        self.assertEqual(times['central_europe'], '2026-11-05T00:00:00+01:00')
        self.assertEqual(times['moscow'], '2026-11-05T02:00:00+03:00')
        self.assertIsNone(launch_times(body, datetime(2026, 1, 1, tzinfo=timezone.utc)))

    def test_editing_photo_preview_updates_caption_or_moves_long_text_to_reply(self):
        url = 'https://bnetcmsus-a.akamaihd.net/cms/blog_header/story.jpg'
        draft_id = self.store.draft(ARTICLE, digest(BODY), result(), url)
        self.app.fetcher.image = lambda *_: b'jpeg'
        self.app.deliver(draft_id)
        self.app.refresh(draft_id)
        self.assertEqual(self.tg.calls[-1][0], 'editMessageCaption')
        self.store.execute('UPDATE drafts SET body=?, revision=revision+1 WHERE id=?',
                           ('Подробный текст. ' * 95, draft_id))
        self.app.refresh(draft_id)
        self.assertEqual(self.tg.calls[-1][0], 'editMessageReplyMarkup')
        self.assertEqual(self.store.row(draft_id)['review_is_photo'], 0)
        self.assertEqual(self.tg.sent[-1][2]['reply_parameters'], {'message_id': 101})

    def test_canonical_url_drops_tracking_not_meaningful_parameters(self):
        self.assertEqual(canonical("https://www.wowhead.com/news/x?utm_source=x&id=7#foo"),
                         "https://www.wowhead.com/news/x?id=7")

    def test_foreign_hosts_and_credentials_rejected(self):
        for url in ["http://news.blizzard.com/x", "https://evil.invalid/x",
                    "https://user@news.blizzard.com/x", "https://news.blizzard.com:22/x"]:
            with self.subTest(url=url), self.assertRaises(FetchError):
                validate_url(url, SOURCE["allowed_hosts"], resolve=False)
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(FetchError):
                validate_url(ARTICLE["url"], SOURCE["allowed_hosts"])

    def test_extract_article_excludes_navigation_preserves_table_cells(self):
        text = article_body(MARKUP.replace("</article>", "<table><tr><td>Example</td><td>18</td></tr></table></article>"))
        self.assertIn("Example | 18", text)
        self.assertNotIn("advertisement", text)

    def test_noob_board_only_discovers_topic_openers_not_replies(self):
        source = {"name": "Noob Club", "url": "https://www.noob-club.ru/index.php?board=268.0",
                  "type": "noob_board", "allowed_hosts": ["www.noob-club.ru"]}
        listing = ('<td class="subject windowbg2"><span><a href="/index.php?topic=123.0">'
                   'Новая информация о Forever</a></span><a href="/index.php?topic=123.15">2</a></td>'
                   '<td class="lastpost"><a href="/index.php?topic=123.msg456#msg456">ответ</a></td>')
        found = discover(SimpleNamespace(text=lambda *_: listing), source)
        self.assertEqual([a['url'] for a in found],
                         ['https://www.noob-club.ru/index.php?topic=123.0'])

    def test_noob_article_extracts_first_post_without_comments(self):
        text = ('<div id="forumposts"><div class="inner" id="msg_100">'
                '<p>Новая информация о Forever и другие факты из первого сообщения редакции.</p>'
                '<p>Подробности для игроков: в новости объявлено изменение игровых правил, '
                'указаны сроки запуска и приведены подробные условия события.</p></div>'
                '<div class="inner" id="msg_101">Ложные сведения из ответа читателя.</div></div>')
        self.assertIn('из первого сообщения', article_body(text))
        self.assertNotIn('Ложные сведения', article_body(text))

    def test_forever_spell_lookup_rejects_retail_and_unlocalized_names(self):
        markup = ('<a href="https://www.wowhead.com/forever/spell=427712">Pandemic</a>'
                  '<a href="https://www.wowhead.com/ru/spell=123">Retail spell</a>')
        calls = []
        def fetch(url, hosts):
            calls.append(url)
            return '<title>Пандемия - Заклинание - [Forever]</title>'
        names = forever_spell_names(markup, SimpleNamespace(text=fetch))
        self.assertEqual(names['Pandemic']['name'], 'Пандемия')
        self.assertEqual(calls, ['https://www.wowhead.com/forever/ru/spell=427712'])
        self.assertEqual(forever_spell_names(markup, SimpleNamespace(
            text=lambda *_: '<title>Pandemic - Spell - [Forever]</title>')), {})

    def test_missing_article_fails_closed(self):
        with self.assertRaises(FetchError):
            article_body("<title>WoW Forever important news</title><main>Please complete CAPTCHA</main>")

    def test_wowhead_embedded_json_string_is_decoded_without_javascript_execution(self):
        literal = json.dumps("[db=classicplus][h2]Synthetic[/h2][html]<p>" + BODY + "</p>[/html]")
        markup = '<div class="news-post-content text"><script>WH.markup.printHtml(' + literal + ');</script></div>'
        text = article_body(markup)
        self.assertIn("18 seconds", text)
        self.assertNotIn("printHtml", text)
        self.assertNotIn("[db=", text)

    def test_atom_namespace_and_foreign_entry(self):
        xml = """<feed xmlns="http://www.w3.org/2005/Atom">
        <entry><title>Valid</title><link href="https://news.blizzard.com/en-us/article/1/x?utm_a=b"/></entry>
        <entry><title>Invalid</title><link href="https://evil.invalid/article"/></entry></feed>"""
        items = parse_feed(xml, SOURCE)
        self.assertEqual(len(items), 1)
        self.assertNotIn("utm_", items[0]["url"])

    def test_evidence_outside_source_forces_review(self):
        payload = result()
        payload["evidence"][0]["quote"] = "invented quote which is not in the source"
        validate_result(payload, BODY, set())
        self.assertTrue(payload["needs_review"])

    def test_nonexistent_duplicate_id_rejected(self):
        payload = result()
        payload["duplicate_of"] = 9999
        with self.assertRaises(APIError):
            validate_result(payload, BODY, set())

    def test_model_cannot_force_a_long_caption(self):
        payload = result()
        payload['body'] = 'а' * 1025
        with self.assertRaises(APIError):
            validate_result(payload, BODY, set())

    def test_unknown_terms_never_auto_publish(self):
        editor = Editor(self.cfg, self.store)
        payload = result()
        payload["unknown_terms"] = ["Example Spell"]
        with patch.object(editor, "request", return_value=payload):
            post = editor.prepare({**ARTICLE, "official": True}, BODY)
        self.assertTrue(post["needs_review"])
        self.assertIn("Example Spell", post["review_notes"][0])

    def test_truncated_article_requires_review(self):
        editor = Editor(self.cfg, self.store)
        with patch.object(editor, "request", return_value=result()):
            post = editor.prepare({**ARTICLE, "official": True}, (BODY * 100)[:28000])
        self.assertTrue(post["needs_review"])

    def test_news_site_not_promoted_to_official_by_model(self):
        editor = Editor(self.cfg, self.store)
        with patch.object(editor, "request", return_value=result()):
            post = editor.prepare({**ARTICLE, "official": False}, BODY)
        self.assertEqual(post["kind"], "report")

    def test_editor_rewrites_oversized_post_once_instead_of_cutting_it(self):
        editor = Editor(self.cfg, self.store)
        too_long = result()
        too_long['body'] = 'Длинный абзац. ' * 70
        with patch.object(editor, 'request', side_effect=[too_long, result()]) as model:
            post = editor.prepare({**ARTICLE, 'official': True}, BODY)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(post['body'], result()['body'])

    def test_editor_keeps_fuller_post_when_complete_caption_fits(self):
        editor = Editor(self.cfg, self.store)
        fuller = result()
        fuller['body'] = 'Факт из источника. ' * 39  # Over 700, under Telegram's actual limit.
        with patch.object(editor, 'request', return_value=fuller) as model:
            post = editor.prepare({**ARTICLE, 'official': True}, BODY)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(post['body'], fuller['body'])

    def test_editor_counts_title_and_source_before_rewriting(self):
        editor = Editor(self.cfg, self.store)
        first = result()
        first['body'] = 'а' * 990  # Legal body, but too long as a complete caption.
        with patch.object(editor, 'request', side_effect=[first, result()]) as model:
            editor.prepare({**ARTICLE, 'official': True}, BODY)
        self.assertEqual(model.call_count, 2)
        self.assertIn('для body доступно', model.call_args_list[1].args[2])

    def test_unauthorized_button_cannot_publish(self):
        draft_id = self.make_draft()
        self.app.callback({"id": "q", "from": {"id": 999}, "data": f"p:{draft_id}:1",
                           "message": {"chat": {"id": 999}}})
        self.assertEqual(self.store.row(draft_id)["state"], "draft")
        self.assertFalse(self.tg.sent)

    def test_publication_is_idempotent_on_double_click(self):
        draft_id = self.make_draft()
        self.app.publish(draft_id, 1)
        with self.assertRaises(APIError):
            self.app.publish(draft_id, 1)
        self.assertEqual(len([s for s in self.tg.photos if s[0] == "@test_channel"]), 1)
        self.assertEqual(self.store.row(draft_id)["state"], "published")

    def test_ambiguous_network_timeout_blocks_republication(self):
        draft_id = self.make_draft()
        self.tg.publish_error = APIError("timeout", uncertain=True)
        with self.assertRaises(APIError):
            self.app.publish(draft_id, 1)
        self.assertEqual(self.store.row(draft_id)["state"], "unknown")
        self.tg.publish_error = None
        with self.assertRaises(APIError):
            self.app.publish(draft_id, 1)
        self.assertFalse(self.tg.photos)

    def test_explicit_rejection_allows_manual_retry(self):
        draft_id = self.make_draft()
        self.tg.publish_error = APIError("HTTP 400")
        with self.assertRaises(APIError):
            self.app.publish(draft_id, 1)
        self.assertEqual(self.store.row(draft_id)["state"], "draft")

    def test_autopost_off_blocks_publication_after_verification(self):
        draft_id = self.make_draft()
        self.cfg.allow_auto = True
        self.store.set("autopost", "off")
        with self.assertRaises(APIError):
            self.app.publish(draft_id, 1, automatic=True)
        self.assertEqual(self.store.row(draft_id)["state"], "draft")
        self.assertFalse(self.tg.sent)

    def test_edit_button_in_channel_opens_private_prompt(self):
        draft_id = self.make_draft()
        self.app.callback({"id": "q", "from": {"id": 42}, "data": f"e:{draft_id}:1",
                           "message": {"chat": {"id": -100123}}})
        self.assertEqual(self.tg.sent[0][0], 42)
        session = self.store.one("SELECT * FROM edit_sessions WHERE user_id=42")
        self.assertEqual(session["chat_id"], "42")

    def test_restart_recovers_inflight_state_as_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "news.sqlite3"
            first = Store(path)
            draft_id = first.draft(ARTICLE, digest(BODY), result())
            first.claim_publish(draft_id, 1)
            first.db.close()
            second = Store(path)
            self.assertEqual(second.row(draft_id)["state"], "unknown")
            second.db.close()

    def test_edit_requires_reply_to_specific_prompt(self):
        draft_id = self.make_draft()
        self.store.execute("UPDATE drafts SET state='editing' WHERE id=?", (draft_id,))
        session = {"user_id": 42, "chat_id": "42", "prompt_id": 5, "draft_id": draft_id}
        self.app.edit_reply({"chat": {"id": 42}, "text": "Новый заголовок\nНовый текст",
                             "reply_to_message": {"message_id": 4}}, session)
        self.assertEqual(self.store.row(draft_id)["title"], result()["title"])
        self.app.edit_reply({"chat": {"id": 42}, "text": "Новый заголовок\nНовый текст",
                             "reply_to_message": {"message_id": 5}}, session)
        self.assertEqual(self.store.row(draft_id)["revision"], 2)
        with self.assertRaises(APIError):
            self.app.publish(draft_id, 1)

    def test_daily_budget_is_atomic_and_persists(self):
        outcomes = []
        workers = [threading.Thread(target=lambda: outcomes.append(
            self.store.reserve_call("2026-10-30", 2))) for _ in range(15)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(sum(outcomes), 2)
        self.assertFalse(self.store.reserve_call("2026-10-30", 2))

    def test_first_scan_seeds_existing_news_without_generating_posts(self):
        editor = SimpleNamespace(prepare=lambda *_args, **_kwargs: self.fail("Archive must not be posted"))
        fetcher = SimpleNamespace(text=lambda *_: MARKUP)
        pipeline = Pipeline(self.cfg, self.store, fetcher, editor, None, None)
        with patch("wownews.services.discover", return_value=[ARTICLE]):
            pipeline.scan()
        self.assertEqual(len(self.store.all("SELECT * FROM articles")), 1)
        self.assertFalse(self.store.all("SELECT * FROM drafts"))

    def test_new_article_creates_draft_and_exact_content_repeat_is_skipped(self):
        calls, deliveries = [], []
        editor = SimpleNamespace(prepare=lambda *_a, **_kw: (calls.append(1) or result()))
        pipeline = Pipeline(self.cfg, self.store, SimpleNamespace(text=lambda *_: MARKUP, image=lambda *_: b'jpeg'),
                            editor, None, deliveries.append)
        pipeline.process(ARTICLE, SOURCE)
        pipeline.process({**ARTICLE, "url": ARTICLE["url"] + "-copy"}, SOURCE)
        self.assertEqual(len(self.store.all("SELECT * FROM drafts")), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(deliveries), 1)

    def test_article_without_image_does_not_spend_model_request(self):
        calls=[]
        markup=MARKUP.replace('<meta property="og:image" content="' + TEST_IMAGE + '">', '')
        pipeline=Pipeline(self.cfg,self.store,SimpleNamespace(text=lambda *_: markup),
                          SimpleNamespace(prepare=lambda *_a,**_kw: calls.append(1)),None,None)
        pipeline.process(ARTICLE,SOURCE)
        self.assertEqual(calls,[])
        self.assertFalse(self.store.all('SELECT * FROM drafts'))

    def test_translated_duplicate_does_not_create_another_draft(self):
        existing = self.make_draft()
        duplicate = result()
        duplicate['duplicate_of'] = existing
        article = {**ARTICLE, 'url': 'https://www.noob-club.ru/index.php?topic=123.0',
                   'source': 'Noob Club', 'title': 'Та же новость на русском'}
        source = {'name': 'Noob Club', 'official': False, 'dedicated': True,
                  'allowed_hosts': ['www.noob-club.ru']}
        markup = MARKUP.replace(TEST_IMAGE, 'https://images.noob-club.ru/news/2026/10/photo.jpg')
        pipeline = Pipeline(self.cfg, self.store,
                            SimpleNamespace(text=lambda *_: markup, image=lambda *_: b'jpeg'),
                            SimpleNamespace(prepare=lambda *_a, **_kw: duplicate), None,
                            lambda *_a: self.fail('duplicate must not be delivered'))
        pipeline.process(article, source)
        self.assertEqual([r['id'] for r in self.store.all('SELECT id FROM drafts')], [existing])
        self.assertIsNotNone(self.store.one('SELECT * FROM articles WHERE url=?', (article['url'],)))

    def test_autopost_requires_explicit_setting_and_verification(self):
        published, delivered = [], []
        self.cfg.allow_auto = True
        self.store.set("autopost", "official")
        editor = SimpleNamespace(prepare=lambda *_a, **_kw: result(),
                                 verify=lambda *_a: (False, ["Нужно проверить дату"]))
        pipeline = Pipeline(self.cfg, self.store, SimpleNamespace(text=lambda *_: MARKUP, image=lambda *_: b'jpeg'),
                            editor, lambda *_a: published.append(1), delivered.append)
        pipeline.process(ARTICLE, SOURCE)
        self.assertFalse(published)
        self.assertEqual(len(delivered), 1)
        self.assertTrue(self.store.row(delivered[0])["needs_review"])

    def test_budget_exhaustion_leaves_article_for_future_attempt(self):
        def exhausted(*_a, **_kw):
            raise BudgetError()
        pipeline = Pipeline(self.cfg, self.store, SimpleNamespace(text=lambda *_: MARKUP, image=lambda *_: b'jpeg'),
                            SimpleNamespace(prepare=exhausted), None, None)
        pipeline.process(ARTICLE, SOURCE)
        self.assertFalse(self.store.all("SELECT * FROM drafts"))
        self.assertFalse(self.store.one("SELECT * FROM articles WHERE url=?", (ARTICLE["url"],)))

    def test_rendered_content_escapes_html_and_has_source_from_code(self):
        draft_id = self.make_draft()
        row = self.store.row(draft_id)
        row["body"] = "<script>fake</script> & 18 секунд"
        output = render_post(row)
        self.assertIn("&lt;script&gt;", output)
        self.assertIn(ARTICLE["url"], output)
        self.assertEqual(render_preview(row), output)
        self.assertNotIn("Черновик #", render_preview(row))
        self.assertTrue(keyboard(row)["inline_keyboard"])

    def test_dotenv_is_literal_and_never_executes_shell(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            file = Path(folder) / ".env"
            file.write_text("TEST_VAR='$(echo unsafe)'\n", encoding="utf-8")
            load_env(file)
            self.assertEqual(os.environ["TEST_VAR"], "$(echo unsafe)")

    def test_gemini_settings_use_only_google_key(self):
        with patch.dict(os.environ, {
            "MODEL_PROVIDER": "gemini", "GEMINI_API_KEY": "google-test",
            "OPENAI_API_KEY": "must-not-be-used",
        }, clear=True):
            cfg = Settings(ROOT)
        self.assertEqual(cfg.api_key, "google-test")
        self.assertEqual(cfg.key_variable, "GEMINI_API_KEY")
        self.assertEqual(cfg.model, "gemini-3.5-flash-lite")

    def gemini_editor(self):
        self.cfg.provider = "gemini"
        self.cfg.model = "gemini-3.5-flash-lite"
        self.cfg.request_gap = 0
        return Editor(self.cfg, self.store)

    def test_gemini_request_uses_header_not_url_and_counts_tokens(self):
        editor = self.gemini_editor()
        response = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"text": "private reasoning", "thought": True},
            {"text": json.dumps({"approved": True, "issues": []})}]}}],
            "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 30, "thoughtsTokenCount": 10}}
        with patch("wownews.services.post_json", return_value=response) as transport:
            value = editor.request("verify", {}, "system rules", {"article": "synthetic"})
        self.assertTrue(value["approved"])
        args, kwargs = transport.call_args
        self.assertTrue(args[0].startswith("https://generativelanguage.googleapis.com/"))
        self.assertNotIn(self.cfg.api_key, args[0])
        self.assertEqual(args[2], {"x-goog-api-key": self.cfg.api_key})
        self.assertEqual(args[1]["generationConfig"]["responseMimeType"], "application/json")
        self.assertNotIn(self.cfg.api_key, json.dumps(args[1]))
        usage = self.store.one("SELECT * FROM usage")
        self.assertEqual((usage["calls"], usage["input_tokens"], usage["output_tokens"]), (1, 120, 40))

    def test_gemini_rate_limit_pauses_without_falling_back_to_paid_provider(self):
        editor = self.gemini_editor()
        with patch("wownews.services.post_json", side_effect=APIError("limited", status=429)) as transport:
            with self.assertRaises(BudgetError):
                editor.request("test", {}, "", {})
            with self.assertRaises(BudgetError):
                editor.request("test", {}, "", {})
        self.assertEqual(transport.call_count, 1)
        self.assertGreater(float(self.store.get("model_pause:gemini:" + editor.settings.model)), 0)
        self.assertIn("generativelanguage.googleapis.com", transport.call_args.args[0])

    def test_gemini_incomplete_json_is_not_accepted_as_a_post(self):
        editor = self.gemini_editor()
        response = {"candidates": [{"finishReason": "MAX_TOKENS", "content": {
            "parts": [{"text": '{"approved":'}]}}]}
        with patch("wownews.services.post_json", return_value=response):
            with self.assertRaises(APIError):
                editor.request("test", {}, "", {})
        self.assertFalse(self.store.all("SELECT * FROM drafts"))

    def test_gemini_empty_or_blocked_response_fails_closed(self):
        editor = self.gemini_editor()
        with patch("wownews.services.post_json", return_value={"promptFeedback": {"blockReason": "OTHER"}}):
            with self.assertRaises(APIError):
                editor.request("test", {}, "", {})

    def test_gemini_model_cannot_inject_url_path(self):
        editor = self.gemini_editor()
        self.cfg.model = "../../evil"
        with patch("wownews.services.post_json") as transport:
            with self.assertRaises(APIError):
                editor.request("test", {}, "", {})
        transport.assert_not_called()

    def test_openai_request_remains_available_when_explicitly_selected(self):
        editor = Editor(self.cfg, self.store)
        response = {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"approved":true,"issues":[]}'}]}]}
        with patch("wownews.services.post_json", return_value=response) as transport:
            answer = editor.request("verify", {}, "rules", {})
        self.assertTrue(answer["approved"])
        self.assertEqual(transport.call_args.args[0], "https://api.openai.com/v1/responses")


if __name__ == "__main__":
    unittest.main()
