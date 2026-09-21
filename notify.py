#!/usr/bin/env python3
"""YouTubeチャンネルの新着動画をDiscordのWebhookに投稿する。

- channels.txt に書いたチャンネルのRSSを読み、まだ通知していない動画だけを投稿する
- 通知済みの動画IDは state.json に記録する（GitHub Actions がコミットして保存）
- 新しく追加したチャンネルは、その時点の既存動画を「通知済み」として記録し、
  「通知を始めました」と一言だけ投稿する（過去動画が15本流れるのを防ぐため）
外部ライブラリ不要（Python標準ライブラリのみ）。
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
CHANNELS_FILE = os.path.join(ROOT, "channels.txt")
TEMPLATE_FILE = os.path.join(ROOT, "template.txt")
STATE_FILE = os.path.join(ROOT, "state.json")

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
SKIP_SHORTS = os.environ.get("SKIP_SHORTS", "false").lower() == "true"
DRY_RUN = os.environ.get("DRY_RUN") == "1"

MAX_AGE = timedelta(days=3)      # これより古い動画は、取りこぼし扱いでも投稿しない
SEEN_KEEP = 200                  # チャンネルごとに覚えておく動画IDの数
JST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; yt-discord-notifier/1.0)"
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
UC_RE = re.compile(r"(UC[0-9A-Za-z_-]{22})")


def log(*a):
    print(*a, flush=True)


# ---------------- HTTP ----------------
def http_get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # YouTubeのRSSはたまに500/404を返すので数回やり直す
            last = e
            time.sleep(2 * (i + 1))
    raise last


def post_discord(content, allowed):
    body = json.dumps({"content": content, "allowed_mentions": {"parse": allowed}}).encode("utf-8")
    for _ in range(5):
        req = urllib.request.Request(
            WEBHOOK, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=20):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 429:  # 送りすぎ。指定秒だけ待って再送
                try:
                    wait = float(json.loads(e.read().decode()).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                time.sleep(wait + 0.5)
                continue
            log(f"  ! Discordへの投稿に失敗: HTTP {e.code} {e.read()[:200]!r}")
            return False
        except Exception as e:
            log(f"  ! Discordへの投稿に失敗: {e}")
            time.sleep(2)
    return False


# ---------------- 設定の読み込み ----------------
def read_channels():
    lines = []
    with open(CHANNELS_FILE, encoding="utf-8") as f:
        for raw in f:
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            s = re.split(r"\s+#", s, maxsplit=1)[0].strip()  # 行末のメモを除く
            if s:
                lines.append(s)
    return lines


def normalize(entry):
    """1行の書き方を (キー, 直接ID or None, 調べに行くURL or None) にそろえる。"""
    m = UC_RE.search(entry)
    if m:
        return m.group(1), m.group(1), None
    s = entry.split("?")[0].rstrip("/")
    m = re.search(r"@([\w.\-]+)", s)
    if m:
        h = "@" + m.group(1)
        return h.lower(), None, "https://www.youtube.com/" + h
    m = re.search(r"youtube\.com/((?:c|user)/[\w.\-]+)", s)
    if m:
        return m.group(1).lower(), None, "https://www.youtube.com/" + m.group(1)
    if re.fullmatch(r"[\w.\-]+", s):
        return ("@" + s).lower(), None, "https://www.youtube.com/@" + s
    return None, None, None


def resolve_id(page_url):
    html = http_get(page_url)
    for pat in (
        r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})"',
        r'"externalId"\s*:\s*"(UC[0-9A-Za-z_-]{22})"',
        r'<meta itemprop="identifier" content="(UC[0-9A-Za-z_-]{22})"',
        r'channel_id=(UC[0-9A-Za-z_-]{22})',
    ):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def load_template():
    try:
        with open(TEMPLATE_FILE, encoding="utf-8") as f:
            t = f.read().strip()
            if t:
                return t
    except FileNotFoundError:
        pass
    return "【新着動画】{channel}\n{title}\n{url}"


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


# ---------------- RSS ----------------
def fetch_feed(cid):
    xml = http_get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
    root = ET.fromstring(xml)
    title = (root.findtext("a:title", default="", namespaces=NS) or "").strip()
    videos = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        if not vid:
            continue
        link_el = e.find("a:link", NS)
        link = link_el.get("href") if link_el is not None else ""
        pub = e.findtext("a:published", default="", namespaces=NS)
        try:
            published = datetime.fromisoformat(pub.replace("Z", "+00:00"))
        except ValueError:
            published = datetime.now(timezone.utc)
        videos.append({
            "id": vid,
            "title": (e.findtext("a:title", default="", namespaces=NS) or "").strip(),
            "author": (e.findtext("a:author/a:name", default="", namespaces=NS) or title).strip(),
            "published": published,
            "is_short": "/shorts/" in (link or ""),
        })
    return title, videos


def safe(s):
    # 動画タイトルに @everyone 等が入っていても誤爆しないようにする
    return s.replace("@", "@\u200b")


def render(tpl, v):
    url = f"https://www.youtube.com/watch?v={v['id']}"
    date = v["published"].astimezone(JST)
    return (tpl.replace("{channel}", safe(v["author"]))
               .replace("{title}", safe(v["title"]))
               .replace("{url}", url)
               .replace("{date}", f"{date.month}月{date.day}日 {date:%H:%M}"))


# ---------------- 本体 ----------------
def main():
    if not WEBHOOK and not DRY_RUN:
        log("DISCORD_WEBHOOK_URL が設定されていません（リポジトリの Settings → Secrets に登録してください）")
        sys.exit(1)

    tpl = load_template()
    allowed = []
    if "@everyone" in tpl or "@here" in tpl:
        allowed.append("everyone")
    if "<@&" in tpl:
        allowed.append("roles")

    state = load_state()
    resolved = state.setdefault("resolved", {})
    chans = state.setdefault("channels", {})
    now = datetime.now(timezone.utc)
    state["heartbeat"] = now.strftime("%Y-%m")  # 月1回は必ずコミットが起き、定期実行が止められるのを防ぐ

    entries = read_channels()
    log(f"登録チャンネル: {len(entries)}件")
    posted = 0

    for entry in entries:
        key, cid, page = normalize(entry)
        if not key:
            log(f"- 読めない行を飛ばしました: {entry}")
            continue
        if not cid:
            cid = resolved.get(key)
            if not cid:
                try:
                    cid = resolve_id(page)
                except Exception as e:
                    log(f"- {entry}: チャンネルIDを調べられませんでした（{e}）。次回また試します")
                    continue
                if not cid:
                    log(f"- {entry}: チャンネルIDが見つかりませんでした。URLを確認してください")
                    continue
                resolved[key] = cid

        try:
            ch_title, videos = fetch_feed(cid)
        except Exception as e:
            log(f"- {entry}: RSSの取得に失敗（{e}）。次回また試します")
            continue

        rec = chans.get(cid)
        if rec is None:
            chans[cid] = {"title": ch_title, "seen": [v["id"] for v in videos][:SEEN_KEEP]}
            log(f"- {ch_title}: 新規登録。既存の{len(videos)}本は通知済み扱いにしました")
            # 設定がうまくいったか分かるよう、登録した時だけ一言知らせる
            notice = f"「{safe(ch_title)}」の新着通知を始めました。"
            if DRY_RUN:
                log("  [DRY RUN] " + notice)
            else:
                post_discord(notice, [])
                time.sleep(1.2)
            continue

        rec["title"] = ch_title or rec.get("title", "")
        seen = rec.get("seen", [])
        seen_set = set(seen)
        fresh = [v for v in videos if v["id"] not in seen_set]
        fresh.sort(key=lambda v: v["published"])  # 古い順に流す

        for v in fresh:
            too_old = now - v["published"] > MAX_AGE
            skip = too_old or (SKIP_SHORTS and v["is_short"])
            if skip:
                seen.insert(0, v["id"])
                continue
            msg = render(tpl, v)
            if DRY_RUN:
                log("  [DRY RUN] " + msg.replace("\n", " / "))
                ok = True
            else:
                ok = post_discord(msg, allowed)
                time.sleep(1.2)
            if ok:
                seen.insert(0, v["id"])
                posted += 1
                log(f"  + 投稿: {v['author']} / {v['title']}")
        rec["seen"] = seen[:SEEN_KEEP]
        if not fresh:
            log(f"- {ch_title}: 新着なし")

    # channels.txt から消したチャンネルの記録は片付ける
    active_ids = set()
    for entry in entries:
        key, cid, _ = normalize(entry)
        cid = cid or resolved.get(key)
        if cid:
            active_ids.add(cid)
    for cid in list(chans):
        if cid not in active_ids:
            del chans[cid]

    save_state(state)
    log(f"完了：{posted}件を投稿しました")


if __name__ == "__main__":
    main()
