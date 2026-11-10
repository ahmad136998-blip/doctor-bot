"""Offline end-to-end test: fake Telegram API, real handlers. Run: python tests/test_flow.py"""
import asyncio
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = TMP
os.environ["ALLOWED_USER_IDS"] = "111"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image
from telegram import Update
from telegram.request import BaseRequest

import bot
import db

DOCTOR, STRANGER = 111, 999


def jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (800, 1100), "white").save(buf, "JPEG")
    return buf.getvalue()


class FakeRequest(BaseRequest):
    def __init__(self):
        self.calls = []
        self.mid = 1000

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    def _msg(self, chat_id):
        self.mid += 1
        return {"message_id": self.mid, "date": int(time.time()), "chat": {"id": chat_id, "type": "private"}}

    async def do_request(self, url, method, request_data=None, **kw):
        if "/file/bot" in url:
            return 200, jpeg()
        name = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        self.calls.append((name, params))
        chat = params.get("chat_id", DOCTOR)
        if name == "getMe":
            res = {"id": 1, "is_bot": True, "first_name": "b", "username": "b_bot"}
        elif name == "getFile":
            res = {"file_id": "f", "file_unique_id": "u", "file_path": "photos/x.jpg", "file_size": 100}
        elif name == "sendMediaGroup":
            res = [self._msg(chat) for _ in params["media"]]
        elif name == "answerCallbackQuery":
            res = True
        else:
            res = self._msg(chat)
        return 200, json.dumps({"ok": True, "result": res}).encode()


req = FakeRequest()
app = bot.build_app("123:TEST", request=req)
uid_counter = [0]


def _base(user):
    uid_counter[0] += 1
    return uid_counter[0], {"id": user, "is_bot": False, "first_name": "u"}, {"id": user, "type": "private"}


async def send(text=None, photo=False, user=DOCTOR):
    uid, frm, chat = _base(user)
    m = {"message_id": uid, "date": int(time.time()), "chat": chat, "from": frm}
    if text is not None:
        m["text"] = text
        if text.startswith("/"):
            m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
    if photo:
        m["photo"] = [{"file_id": "f", "file_unique_id": "u", "width": 800, "height": 1100, "file_size": 100}]
    req.calls.clear()
    await app.process_update(Update.de_json({"update_id": uid, "message": m}, app.bot))
    return list(req.calls)


async def click(data, user=DOCTOR):
    uid, frm, chat = _base(user)
    cq = {
        "id": str(uid), "from": frm, "chat_instance": "c", "data": data,
        "message": {"message_id": 5, "date": int(time.time()), "chat": chat},
    }
    req.calls.clear()
    await app.process_update(Update.de_json({"update_id": uid, "callback_query": cq}, app.bot))
    return list(req.calls)


def texts(calls):
    return " | ".join(str(p.get("text") or p.get("caption") or "") for _, p in calls)


def names(calls):
    return [n for n, _ in calls]


async def main():
    await app.initialize()

    # access control
    c = await send("/start", user=STRANGER)
    assert "دسترسی ندارید" in texts(c) and "999" in texts(c)
    c = await send(photo=True, user=STRANGER)
    assert "دسترسی ندارید" in texts(c)
    c = await click("p:1", user=STRANGER)
    assert names(c) == ["answerCallbackQuery"]

    # create patient with phone (Persian digits)
    await send("/start")
    await send(bot.BTN_NEW)
    await send("علي كريمي")  # Arabic letters
    c = await send("abc")
    assert "درست نیست" in texts(c)
    c = await send("۰۹۱۲۳۴۵۶۷۸۹")
    assert "ثبت شد" in texts(c) and "09123456789" in texts(c)
    assert db.get_patient(1)["phone"] == "09123456789"

    # second patient without phone, duplicate-name warning
    await send(bot.BTN_NEW)
    c = await send("علی کریمی")
    assert "هم‌نام" in texts(c)
    c = await click("nophone")
    assert "ثبت شد" in texts(c)
    assert db.get_patient(2) is not None

    # search: Persian spelling finds Arabic-typed name; phone; file number
    c = await send("کریمی")
    assert "2 نتیجه" in texts(c)
    c = await send("3456")
    assert "شماره پرونده: 1" in texts(c)
    c = await send("۲")
    assert "شماره پرونده: 2" in texts(c)
    c = await send("ناموجود")
    assert "پیدا نشد" in texts(c)

    # photo without active patient is rejected
    c = await send(photo=True)
    assert "ذخیره نشد" in texts(c) and not db.list_docs(1)

    # upload flow
    await click("add:1")
    c = await click("k:1:visit")
    assert "ویزیت" in texts(c)
    c = await send(photo=True)
    assert "ذخیره شد" in texts(c) and "#1" in texts(c)
    await click("k:1:lab")
    for _ in range(11):
        await send(photo=True)
    docs = db.list_docs(1)
    assert len(docs) == 12 and all(Path(d["path"]).exists() for d in docs)
    assert not db.list_docs(2)

    # delete one doc
    victim = docs[-1]
    c = await click(f"dd:{victim['id']}")
    assert "editMessageText" in names(c) and not Path(victim["path"]).exists()
    assert len(db.list_docs(1)) == 11

    # typing a search leaves upload mode
    await send("کریمی")
    c = await send(photo=True)
    assert "ذخیره نشد" in texts(c) and len(db.list_docs(1)) == 11

    # show: 11 photos -> album of 10 + single photo
    c = await click("show:1")
    assert names(c) == ["answerCallbackQuery", "sendMediaGroup", "sendPhoto"]

    # pdf
    c = await click("pdf:1")
    assert "sendDocument" in names(c) and "11 صفحه" in texts(c)
    c = await click("pdf:2")
    assert "عکسی ندارد" in texts(c)

    # edit
    await click("en:1")
    c = await send("علی کریمی‌نژاد")
    assert db.get_patient(1)["name"] == "علی کریمی‌نژاد"
    await click("ep:1")
    await send("07701234567")
    assert db.get_patient(1)["phone"] == "07701234567"

    # delete patient removes files
    c = await click("del:1")
    assert "11 عکس" in texts(c)
    await click("delok:1")
    assert db.get_patient(1) is None and not db.list_docs(1)
    assert not list((Path(TMP) / "files" / "1").glob("*.jpg"))
    c = await click("show:1")
    assert "پیدا نشد" in texts(c)

    # no temp files left behind
    assert not list(Path(TMP).glob("tmp-*"))
    await app.shutdown()
    print("ALL OK")


asyncio.run(main())
