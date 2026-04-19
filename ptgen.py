# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import mimetypes
import re
import sys
import time
import os
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from typing import Iterable, List, Optional

import requests
from bs4 import BeautifulSoup

# =========================  必 填 配  =========================

TMDB_API_KEY = ""  # 必填：TMDB API Key
DOUBAN_COOKIE_RAW = r"""

""".strip()

# —— PTPPimg（可选）——
PTPIMG_UPLOAD_ENABLED = False                   # True 启用；False 禁用（禁用时直接用 TMDB/IMDb 链接）
PTPIMG_API_KEY = ""   # 启用时必填；仅需填写 KEY
PTPIMG_ENDPOINT = "https://ptpimg.me/upload.php"  # 固定上传入口

# =========================  缓 存 配 置  =========================
CACHE_ENABLED = True
CACHE_TTL_SECONDS = 600  # 600s
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "douban_meta_cache")

# =========================  全 局 参 数  =========================

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
TIMEOUT = (5, 12)
RETRY = 2
SLEEP = 0.9

DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
JSON_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

sess = requests.Session()
sess.headers.update(DEFAULT_HEADERS)

# =========================  基 础 工 具  =========================

def install_cookie(cookie_raw: str) -> None:
    """将浏览器复制的整条 Cookie 安装进 Session（写入 .douban.com / douban.com）。"""
    s = (cookie_raw or "").strip().strip('"').strip("'")
    if not s:
        return
    for part in [p.strip() for p in s.split(";") if p.strip()]:
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if not k:
            continue
        for domain in (".douban.com", "douban.com"):
            try:
                sess.cookies.set(k, v, domain=domain, path="/")
            except Exception:
                pass

install_cookie(DOUBAN_COOKIE_RAW)

def headers_for(url: str, json_mode: bool = False) -> dict:
    """针对特定 URL 附加适当的 Referer / 接受头。"""
    h = dict(JSON_HEADERS if json_mode else DEFAULT_HEADERS)
    if "movie.douban.com/j/subject_suggest" in url:
        h["Referer"] = "https://movie.douban.com/"
    elif "search.douban.com" in url or "movie.douban.com/subject_search" in url:
        h["Referer"] = "https://movie.douban.com/"
    return h

def GET(url: str, retry: int = RETRY, json_mode: bool = False) -> Optional[requests.Response]:
    """带重试 GET；豆瓣站点被重定向到登录/风控页则重试。"""
    h = headers_for(url, json_mode)
    for i in range(retry):
        try:
            r = sess.get(url, headers=h, timeout=TIMEOUT, allow_redirects=True)
            if r.status_code == 200:
                if ("douban.com/accounts/login" in r.url) or ("sec.douban.com" in r.url):
                    time.sleep(SLEEP * (i + 1))
                    continue
                return r
        except Exception:
            pass
        time.sleep(SLEEP * (i + 1))
    return None

def cleanstr(s: str) -> str:
    return re.sub(r'[\U00010000-\U0010ffff]', '', s or "")

def extract_imdb_id(s: str) -> Optional[str]:
    m = re.search(r"(tt\d{6,10})", s or "", re.I)
    return m.group(1).lower() if m else None

# =========================  缓 存 工 具（方案A：懒清理） =========================

def _cache_key_from_input(inp: str) -> Optional[str]:
    """
    为输入生成稳定 cache key：
      - 优先 IMDbID（ttxxxxxx）
      - 否则用豆瓣 subject id（subject/1234567）
    """
    s = (inp or "").strip()
    imdb_id = extract_imdb_id(s)
    if imdb_id:
        return f"imdb:{imdb_id.lower()}"

    m = re.search(r"(?:https?://)?movie\.douban\.com/subject/(\d+)", s, re.I)
    if m:
        return f"douban:{m.group(1)}"

    return None

def _cache_path(key: str) -> str:
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{h}.json")

def cache_get(key: str, ttl: int = CACHE_TTL_SECONDS) -> Optional[str]:
    """
    读取缓存：命中返回 out；过期则删除文件（懒清理）并返回 None。
    """
    if not (CACHE_ENABLED and key):
        return None
    path = _cache_path(key)
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        ts = float(data.get("ts", 0))
        out = data.get("out", "")
        if not out:
            return None
        if (time.time() - ts) <= ttl:
            return out
        # ⭐ 过期：删除文件（方案A）
        try:
            os.remove(path)
        except Exception:
            pass
    except Exception:
        return None
    return None

def cache_set(key: str, out: str) -> None:
    """写缓存：只缓存最终输出文本。"""
    if not (CACHE_ENABLED and key and out):
        return
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _cache_path(key)
        tmp = f"{path}.tmp"
        payload = {"ts": time.time(), "out": out}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass

# =========================  JSON / HTML 解析 =========================

def balanced_json_after(html: str, pos: int) -> Optional[str]:
    """window.__DATA__ 的稳定 JSON 提取（括号/字符串配对）。"""
    i = html.find("{", pos)
    if i < 0:
        return None
    stack = 0
    j = i
    ins = False
    esc = False
    while j < len(html):
        ch = html[j]
        if ins:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                ins = False
        else:
            if ch == '"':
                ins = True
            elif ch == "{":
                stack += 1
            elif ch == "}":
                stack -= 1
                if stack == 0:
                    return html[i:j + 1]
        j += 1
    return None

def parse_window_data(html: str) -> List[str]:
    """从豆瓣搜索页的 window.__DATA__ 中抽取 subject URL 列表。"""
    urls: List[str] = []
    m = re.search(r"window\.__DATA__\s*=\s*", html or "")
    if not m:
        return urls
    js = balanced_json_after(html, m.end())
    if not js:
        return urls
    try:
        data = json.loads(js)
        for it in data.get("items", []) or []:
            u = (it.get("url") or "").strip()
            if re.match(r"^https?://movie\.douban\.com/subject/\d+/?$", u):
                urls.append(u)
    except Exception:
        pass
    out, seen = [], set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def parse_subjects_from_general(html: str) -> List[str]:
    """从通用搜索页抓 subject URL。"""
    found = re.findall(r'href="(https?://movie\.douban\.com/subject/\d+/)"', html or "", re.I)
    out, seen = [], set()
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

# =========================  IMDb → 豆瓣 匹配  =========================

def douban_candidates(imdb_id: str) -> Iterable[str]:
    """根据 IMDbID 在不同入口搜索豆瓣候选条目 URL。"""
    for base in ("https://search.douban.com/movie/subject_search",
                 "https://movie.douban.com/subject_search"):
        r = GET(f"{base}?search_text={imdb_id}&cat=1002")
        if r:
            for u in parse_window_data(r.text):
                yield u

    r = GET(f"https://www.douban.com/search?q={imdb_id}")
    if r:
        for u in parse_subjects_from_general(r.text):
            yield u

    # 用 IMDb 标题走 suggest
    ri = GET(f"https://www.imdb.com/title/{imdb_id}/")
    if ri and ri.text:
        t = re.search(r"<title>\s*(.*?)\s*</title>", ri.text, re.S | re.I)
        title = re.sub(r"\s*-\s*IMDb$", "", t.group(1).strip()) if t else ""
        if title:
            r2 = GET(
                f"https://movie.douban.com/j/subject_suggest?q={requests.utils.quote(title)}",
                json_mode=True
            )
            if r2:
                try:
                    for it in r2.json() or []:
                        u = (it.get("url") or "").strip()
                        if re.match(r"^https?://movie\.douban\.com/subject/\d+/?$", u):
                            yield u
                except Exception:
                    pass

def verify_subject_has_imdb(subject_url: str, imdb_id: str) -> bool:
    r = GET(subject_url)
    return bool(r and re.search(re.escape(imdb_id), r.text or "", re.I))

def imdb_to_douban(imdb_input: str) -> Optional[str]:
    imdb_id = extract_imdb_id(imdb_input)
    if not imdb_id:
        return None
    seen = set()
    for u in douban_candidates(imdb_id):
        if u in seen:
            continue
        seen.add(u)
        if verify_subject_has_imdb(u, imdb_id):
            return u
    return None

# =========================  海 报 源  =========================

def tmdb_poster_from_imdb(imdb_id: str) -> str:
    """TMDB API：通过 IMDbID 反查电影/剧集/剧集分集，返回原始尺寸海报地址。"""
    if not TMDB_API_KEY or not imdb_id:
        return ""
    url = f"https://api.themoviedb.org/3/find/{imdb_id}?api_key={TMDB_API_KEY}&external_source=imdb_id"
    r = GET(url, json_mode=True)
    if not r:
        return ""
    try:
        data = r.json()
    except Exception:
        return ""

    def pick(results):
        for it in results or []:
            p = it.get("poster_path")
            if p:
                return "https://image.tmdb.org/t/p/original" + p
        return ""

    return (pick(data.get("movie_results"))
            or pick(data.get("tv_results"))
            or pick(data.get("tv_episode_results"))
            or "")

def imdb_og_image(imdb_id: str) -> str:
    """IMDb 页面：优先 og:image，其次 JSON-LD image。"""
    if not imdb_id:
        return ""
    r = GET(f"https://www.imdb.com/title/{imdb_id}/")
    if not r or not r.text:
        return ""
    m = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', r.text, re.I)
    if m and m.group(1).strip():
        return m.group(1).strip()
    for block in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', r.text, re.S | re.I):
        try:
            data = json.loads(block.strip())
            img = data.get("image")
            if isinstance(img, str) and img.strip():
                return img.strip()
            if isinstance(img, list):
                for it in img:
                    if isinstance(it, str) and it.strip():
                        return it.strip()
        except Exception:
            pass
    return ""

def douban_poster_from_page(soup: BeautifulSoup) -> str:
    """豆瓣页海报（作为最终兜底）。"""
    img = soup.find("img", title="点击看更多海报")
    if not img or "src" not in img.attrs:
        return ""
    url = img["src"]
    m = re.search(r"/(p\d+)\.", url)
    if m:
        return f"https://img9.doubanio.com/view/photo/l_ratio_poster/public/{m.group(1)}.jpg"
    return url.replace(".webp", ".jpg") if url.endswith(".webp") else url

# =========================  PTPimg 上 传  =========================

def ptpimg_upload_from_link(image_url: str) -> str:
    """
    开启 PTPIMG_UPLOAD_ENABLED 后调用：
      - 优先：远程链接直传（更快）
      - 兜底：下载后文件上传
    成功返回 https://ptpimg.me/<code>.<ext> ，失败返回空串
    """
    if not (PTPIMG_UPLOAD_ENABLED and PTPIMG_API_KEY and image_url):
        return ""

    # 方式一：远程链接直传
    try:
        resp = sess.post(PTPIMG_ENDPOINT, data={"api_key": PTPIMG_API_KEY, "source": image_url}, timeout=TIMEOUT)
        if resp.ok:
            data = resp.json()
            if isinstance(data, list) and data:
                code, ext = data[0].get("code"), data[0].get("ext")
                if code and ext:
                    return f"https://ptpimg.me/{code}.{ext}"
    except Exception:
        pass

    # 方式二：下载后文件上传
    try:
        r = GET(image_url)
        if not (r and r.content):
            return ""
        content_type = r.headers.get("Content-Type", "image/jpeg").split(";", 1)[0].strip()
        ext = (mimetypes.guess_extension(content_type) or ".jpg")
        files = {"file-upload[]": (f"poster{ext}", r.content, content_type)}
        resp = sess.post(PTPIMG_ENDPOINT, data={"api_key": PTPIMG_API_KEY}, files=files, timeout=TIMEOUT)
        if resp.ok:
            data = resp.json()
            if isinstance(data, list) and data:
                code, ext = data[0].get("code"), data[0].get("ext")
                if code and ext:
                    return f"https://ptpimg.me/{code}.{ext}"
    except Exception:
        pass

    return ""

# =========================  豆 瓣 详 情  =========================

class DoubanMovie:
    """豆瓣详情抓取与格式化（只负责格式化输出文本）。"""

    def __init__(self, url: str):
        m = re.search(r"/subject/(\d+)", url)
        if not m:
            raise ValueError("Invalid Douban URL")
        self.id = m.group(1)
        self.url = f"https://movie.douban.com/subject/{self.id}/"
        r = GET(self.url)
        if not r:
            raise RuntimeError("fetch fail")
        self.html = cleanstr(r.text)
        self.soup = BeautifulSoup(self.html, "lxml")
        self.jsonld = self._jsonld()
        self.info = {}

    # ---------- 基础解析 ----------
    def _jsonld(self) -> Optional[dict]:
        try:
            tag = self.soup.find("script", type="application/ld+json")
            return json.loads(tag.string) if tag and tag.string else None
        except Exception:
            return None

    def _info_text(self, key: str) -> str:
        try:
            tag = self.soup.select_one(f'#info span.pl:-soup-contains("{key}")')
            if not tag:
                return ""
            node = tag.next_sibling
            while node:
                if getattr(node, "name", None) == "span":
                    return node.get_text(strip=True)
                if isinstance(node, str) and node.strip():
                    return node.strip()
                node = node.next_sibling
        except Exception:
            pass
        return ""

    def _names(self) -> dict:
        try:
            page_title = (self.soup.title.text or "").replace("(豆瓣)", "").strip()
            off = self.soup.find("span", property="v:itemreviewed")
            official = off.text.strip() if off else ""
            aka = [x.strip() for x in (self._info_text("又名") or "").split("/") if x.strip()]
            if page_title not in official and official:
                original, trans = official, page_title
            else:
                maybe = official.replace(page_title, "").strip()
                if maybe and re.match(r"^[a-zA-Z0-9\s:'’.·-]+$", maybe):
                    original, trans = maybe, page_title
                else:
                    original, trans = page_title, (aka[0] if aka else page_title)
            season = "".join(re.findall("第.*季", page_title))
            return {"chinesename": page_title, "originalTitle": original, "translatedTitle": trans, "akaTitles": aka, "seasonstr": season}
        except Exception:
            return {}

    def _year(self) -> Optional[int]:
        y = self.soup.find("span", class_="year")
        return int(re.search(r"(\d{4})", y.text).group(1)) if y else None

    def _persons(self, key: str) -> List[dict]:
        # 优先 JSON-LD
        try:
            kmap = {"导演": "director", "编剧": "author", "主演": "actor"}
            arr = self.jsonld.get(kmap[key]) if self.jsonld else None
            if arr:
                if not isinstance(arr, list):
                    arr = [arr]
                out = []
                for p in arr:
                    if not isinstance(p, dict):
                        continue
                    raw = " ".join((p.get("name", "")).split())
                    m = re.match(r"([\u4e00-\u9fa5·\s]+)\s*([a-zA-Z0-9\s.'’:·-]+)$", raw)
                    zh, en = (m.groups() if m else (raw, ""))
                    out.append({"name_zh": zh.strip(), "name_en": en.strip()})
                return out
        except Exception:
            pass
        # 回退 HTML
        out = []
        tag = self.soup.select_one(f'#info span.pl:-soup-contains("{key}")')
        if tag:
            attrs = tag.find_next_sibling("span", class_="attrs")
            if attrs:
                for a in attrs.find_all("a"):
                    out.append({"name_zh": a.text.strip(), "name_en": ""})
        return out

    def _genres(self) -> List[str]:
        return [x.text for x in self.soup.find_all("span", property="v:genre")]

    def _summary(self) -> str:
        x = self.soup.find("span", class_="all hidden") or self.soup.find("span", property="v:summary")
        return x.text.strip().replace("\u3000", "") if x else ""

    def _rating(self) -> dict:
        avg = self.soup.find("strong", property="v:average")
        cnt = self.soup.find("span", property="v:votes")
        return {"average": avg.text if avg else "0", "reviews_count": cnt.text if cnt else "0"}

    def _imdb(self) -> str:
        t = self.soup.find(lambda x: x.name == "span" and "IMDb" in x.text)
        return t.next_sibling.strip() if t else ""

    # ---------- 解析总控 ----------
    def parse(self) -> None:
        pubdates = [x.get_text(strip=True) for x in self.soup.find_all("span", property="v:initialReleaseDate")]

        duration_text = self._info_text("片长") or ""
        duration_label = "片长"
        if not duration_text:
            duration_text = self._info_text("单集片长")
            if duration_text:
                duration_label = "单集片长"

        names = self._names()
        year = self._year()
        imdb_id = self._imdb()

        # 海报优先：TMDB(API) > IMDb > 豆瓣
        poster = tmdb_poster_from_imdb(imdb_id) or imdb_og_image(imdb_id) or douban_poster_from_page(self.soup)

        # 启用 PTPimg 上传则覆盖为 PTPimg 链接
        if PTPIMG_UPLOAD_ENABLED and poster:
            up = ptpimg_upload_from_link(poster)
            if up:
                poster = up

        self.info = {
            "id": self.id,
            "url": self.url,
            "names": names,
            "year": year,
            "image_url": poster,
            "directors": self._persons("导演"),
            "writers": self._persons("编剧"),
            "actors": self._persons("主演"),
            "genres": self._genres(),
            "countries": [c.strip() for c in (self._info_text("制片国家/地区") or "").split("/") if c.strip()],
            "languages": [l.strip() for l in (self._info_text("语言") or "").split("/") if l.strip()],
            "pubdates": pubdates,
            "release_label": "首播" if self.soup.select_one('#info span.pl:-soup-contains("首播")') else "上映日期",
            "episodes": self._info_text("集数"),
            "durations": [x.strip() for x in duration_text.split("/") if x.strip()],
            "duration_label": duration_label,
            "summary": self._summary(),
            "rating": self._rating(),
            "imdb_id": imdb_id,
        }

    # ---------- 输出 ----------
    def format(self) -> str:
        d = self.info
        if not d:
            return ""
        out = []
        if d.get("image_url"):
            out.append(f"[img]{d['image_url']}[/img]")

        ns = d.get("names") or {}
        if ns.get("translatedTitle"):
            titles = [ns["translatedTitle"]] + ns.get("akaTitles", [])
            uniq = list(dict.fromkeys([t for t in titles if t]))
            out.append(f"\n◎译　　名　{' / '.join(uniq)}")
        if ns.get("seasonstr"):
            out.append(f"◎季　　数　{ns['seasonstr']}")
        if ns.get("originalTitle"):
            out.append(f"◎片　　名　{ns['originalTitle']}")
        if d.get("year"):
            out.append(f"◎年　　代　{d['year']}")
        if d.get("countries"):
            out.append(f"◎产　　地　{' / '.join(d['countries'])}")
        if d.get("genres"):
            out.append(f"◎类　　别　{' / '.join(d['genres'])}")
        if d.get("languages"):
            out.append(f"◎语　　言　{' / '.join(d['languages'])}")

        out.append(("◎首　　播　" if d["release_label"] == "首播" else "◎上映日期　") + " / ".join(d.get("pubdates", [])))

        if d.get("imdb_id"):
            out.append(f"◎IMDb链接  https://www.imdb.com/title/{d['imdb_id']}/")

        r = d.get("rating", {})
        if r.get("average", "0") != "0":
            out.append(f"◎豆瓣评分　{r['average']}/10 from {r['reviews_count']} users")

        out.append(f"◎豆瓣链接　{d['url']}")

        if d.get("durations"):
            lbl = "◎单集片长" if d["duration_label"] == "单集片长" else "◎片　　长"
            out.append(f"{lbl}　{' / '.join(d['durations'])}")

        if d.get("episodes"):
            out.append(f"◎集　　数　{d['episodes']}")

        # 人员对齐（续行使用“与标签等宽的全角空格 + 全角空格”）
        def _name(p: dict) -> str:
            return f"{p.get('name_zh', '')} {p.get('name_en', '')}".strip()

        def _pad(label: str) -> str:
            return re.sub(r"[^\s]", "　", label) + "　"

        for key, label in (("directors", "◎导　　演"), ("writers", "◎编　　剧"), ("actors", "◎主　　演")):
            arr = d.get(key) or []
            if not arr:
                continue
            out.append(f"{label}　{_name(arr[0])}")
            pfx = _pad(label)
            for p in arr[1:]:
                out.append(f"{pfx}{_name(p)}")

        out.append("\n◎简　　介")
        summary = re.sub(r"\s*<br\s*/?>\s*", " ", d.get("summary", ""))
        out.append(f"\n　　{summary if summary else '暂无相关剧情介绍'}")

        return "\n".join(out)

# =========================  主 程 序  =========================

def main() -> None:
    # 命令：
    #   python ptgen.py <imdb|douban_url>   -> CLI 文本输出
    #   python ptgen.py --serve [host] [port] -> 启动 Web + API
    if len(sys.argv) >= 2 and sys.argv[1] == "--serve":
        host = sys.argv[2] if len(sys.argv) >= 3 else "127.0.0.1"
        port = int(sys.argv[3]) if len(sys.argv) >= 4 else 8000
        run_server(host, port)
        return

    # 无参数：静默退出
    if len(sys.argv) < 2:
        return

    txt = generate_from_input(sys.argv[1].strip())
    if txt:
        sys.stdout.write(txt.rstrip() + "\n")


def generate_from_input(inp: str) -> str:
    """统一处理输入并返回最终格式化文本；失败返回空串。"""
    # 1) 先尝试走缓存（同输入 600s 内直接返回；过期会被懒清理删除）
    cache_key = _cache_key_from_input(inp)
    if cache_key:
        cached = cache_get(cache_key, CACHE_TTL_SECONDS)
        if cached:
            return cached

    # 2) 解析输入 -> 得到豆瓣 URL
    m = re.search(r"https?://movie\.douban\.com/subject/\d+/?", inp)
    if m:
        db_url = m.group(0)
    else:
        imdb_id = extract_imdb_id(inp)
        if imdb_id:
            db_url = imdb_to_douban(imdb_id)
        else:
            return ""

    if not db_url:
        return ""

    try:
        dm = DoubanMovie(db_url)
        dm.parse()
        txt = dm.format()
        if txt and cache_key:
            # 3) 写缓存（只缓存最终输出文本）
            cache_set(cache_key, txt)
        return txt or ""
    except Exception:
        return ""


class PTGenHandler(BaseHTTPRequestHandler):
    """极简 Web UI + API。"""

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        p = urlparse(self.path)
        if p.path == "/":
            self._send_html(self._index_html())
            return
        if p.path == "/api/generate":
            qs = parse_qs(p.query)
            inp = (qs.get("input") or [""])[0].strip()
            self._handle_generate(inp)
            return
        self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        p = urlparse(self.path)
        if p.path != "/api/generate":
            self.send_error(404, "Not Found")
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="ignore") if length > 0 else ""
        inp = ""
        try:
            data = json.loads(raw) if raw.strip() else {}
            inp = str(data.get("input", "")).strip()
        except Exception:
            inp = ""
        self._handle_generate(inp)

    def _handle_generate(self, inp: str) -> None:
        if not inp:
            self._send_json(400, {"ok": False, "error": "input 不能为空"})
            return
        txt = generate_from_input(inp)
        if not txt:
            self._send_json(422, {"ok": False, "error": "解析失败，请检查 IMDbID 或豆瓣链接"})
            return
        self._send_json(200, {"ok": True, "input": inp, "result": txt})

    @staticmethod
    def _index_html() -> str:
        return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ptgen Web</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:920px;margin:40px auto;padding:0 16px}
    .row{display:flex;gap:8px;align-items:center}
    input{flex:1;padding:10px;border:1px solid #ccc;border-radius:8px}
    button{padding:10px 16px;border:0;border-radius:8px;background:#1677ff;color:#fff;cursor:pointer}
    pre{white-space:pre-wrap;background:#f7f7f8;border:1px solid #eee;border-radius:8px;padding:12px;min-height:220px}
    .hint{color:#666;font-size:14px}
  </style>
</head>
<body>
  <h1>ptgen Web</h1>
  <p class="hint">输入 IMDb ID（如 tt0133093）或豆瓣链接，点击“生成”。也可调用 API：<code>/api/generate</code></p>
  <div class="row">
    <input id="inp" placeholder="tt0133093 或 https://movie.douban.com/subject/1291843/" />
    <button id="go">生成</button>
  </div>
  <p class="hint">POST JSON 示例：<code>{"input":"tt0133093"}</code></p>
  <pre id="out">结果会显示在这里…</pre>
  <script>
    const out = document.getElementById('out');
    document.getElementById('go').onclick = async () => {
      const input = document.getElementById('inp').value.trim();
      if (!input){ out.textContent = '请输入内容'; return; }
      out.textContent = '处理中...';
      try{
        const r = await fetch('/api/generate', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({input})
        });
        const data = await r.json();
        out.textContent = data.ok ? data.result : ('错误：' + (data.error || r.status));
      }catch(e){
        out.textContent = '请求失败：' + e;
      }
    };
  </script>
</body>
</html>"""


def run_server(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), PTGenHandler)
    print(f"ptgen web server running on http://{host}:{port}")
    server.serve_forever()

if __name__ == "__main__":
    main()
