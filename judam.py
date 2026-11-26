#!/usr/bin/env python3
"""주담통화 블로그 후기 수집 + 기업별 정리 페이지 생성.

사용법:
  python judam.py collect   # 네이버 검색 API로 오늘 글 수집 -> data/YYYY-MM-DD.json
  python judam.py watchlist # 네이버 증권 상장 보통주 전체 + 업종 -> watchlist.csv
  python judam.py backfill  # 워치리스트 종목별 지난 글 -> data/backfill/posts.json (한 번만)
  python judam.py build     # data/*.json -> site/ 정적 페이지 (기업마다 최근 5건)
환경변수: NAVER_CLIENT_ID, NAVER_CLIENT_SECRET (.env 파일에 적어도 됨)
"""
import csv
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SITE = ROOT / "site"
KST = timezone(timedelta(hours=9))

KEYWORDS = ["주담통화", "주담 통화", "주담과 통화", "주식담당자 통화", "IR 통화"]
API = "https://openapi.naver.com/v1/search/blog.json"
TAG_RE = re.compile(r"<[^>]+>")


def load_env():
    """.env의 KEY=VALUE를 읽어 이미 설정된 환경변수가 없을 때만 채운다."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip("\"'"))


def clean(s):
    return html.unescape(TAG_RE.sub("", s or "")).strip()


def load_watchlist():
    """watchlist.csv: name,code,aliases(세미콜론 구분),market,sector(네이버 업종)"""
    items = []
    path = ROOT / "watchlist.csv"
    if not path.exists():
        return items
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip()
            if not name:
                continue
            aliases = [a.strip() for a in (row.get("aliases") or "").split(";") if a.strip()]
            items.append({"name": name, "code": (row.get("code") or "").strip(),
                          "sector": (row.get("sector") or "").strip(), "market": (row.get("market") or "").strip(),
                          "aliases": aliases,
                          "terms": sorted({name, *aliases}, key=len, reverse=True)})
    return items


def match_companies(title, desc, watchlist):
    """긴 이름부터 찾고 찾은 자리를 지워서 'SK하이닉스'가 'SK'로도 잡히지 않게 한다.
    두 글자 이하 이름은 제목에서만, 앞뒤에 글자가 붙지 않을 때만 인정한다
    ('테스트'의 테스, 'SK온'의 SK, 요약 속 '대상', '나노', '선진' 같은 일반 단어)."""
    text = title + "\n" + desc
    head = len(title)
    terms = sorted(((t, c["name"]) for c in watchlist for t in c["terms"]),
                   key=lambda x: len(x[0]), reverse=True)
    hits = []
    for t, name in terms:
        if t not in text:
            continue
        if len(t) <= 2:
            pat = re.compile(rf"(?<![가-힣A-Za-z0-9]){re.escape(t)}(?![가-힣A-Za-z0-9])")
            if not pat.search(text[:head]):
                continue
            text = pat.sub("\0", text)
        else:
            text = text.replace(t, "\0")
        if name not in hits:
            hits.append(name)
    order = [c["name"] for c in watchlist]
    return sorted(hits, key=order.index)


def search(query, cid, secret, start):
    qs = urllib.parse.urlencode({"query": query, "display": 100, "start": start, "sort": "date"})
    req = urllib.request.Request(f"{API}?{qs}", headers={
        "X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def naver_keys():
    load_env()
    cid, secret = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    if not (cid and secret):
        sys.exit("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 값이 필요합니다. .env에 적거나 환경변수로 넣어주세요.")
    return cid, secret


STOCK_API = "https://m.stock.naver.com/api/stocks"


def naver_stock(path):
    req = urllib.request.Request(f"{STOCK_API}/{path}", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def update_watchlist():
    """네이버 증권에서 코스피·코스닥 상장 보통주 전체(스팩 제외)를 시총순으로 받아 업종과 함께 watchlist.csv로.
    이미 적어 둔 aliases는 유지한다."""
    sector = {}
    groups, p = [], 1
    while True:
        g = naver_stock(f"industry?page={p}&pageSize=100").get("groups", [])
        groups += g
        if len(g) < 100:
            break
        p += 1
    for g in groups:
        p = 1
        while True:
            st = naver_stock(f"industry/{g['no']}?page={p}&pageSize=100").get("stocks", [])
            for x in st:
                sector[x["itemCode"]] = g["name"]
            if len(st) < 100:
                break
            p += 1
    path = ROOT / "watchlist.csv"
    old = {}
    if path.exists():
        with path.open(encoding="utf-8-sig") as f:
            old = {r["name"]: r.get("aliases", "") for r in csv.DictReader(f)}
    rows = []
    for m, label in (("KOSPI", "코스피"), ("KOSDAQ", "코스닥")):
        p = 1
        while True:
            st = naver_stock(f"marketValue/{m}?page={p}&pageSize=100").get("stocks", [])
            for x in st:
                if x["stockEndType"] == "stock" and x["itemCode"].endswith("0") and "스팩" not in x["stockName"]:
                    rows.append({"name": x["stockName"], "code": x["itemCode"], "aliases": old.get(x["stockName"], ""),
                                 "market": label, "sector": sector.get(x["itemCode"], "")})
            if len(st) < 100:
                break
            p += 1
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "code", "aliases", "market", "sector"])
        w.writeheader()
        w.writerows(rows)
    print(f"watchlist.csv: {len(rows)}곳")


DART = "https://opendart.fss.or.kr/api"
CACHE = ROOT / ".cache"


def dart_get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def dart_listed(key):
    """DART 고유번호 파일에서 종목코드가 있는 회사만 {회사명: [고유번호, 종목코드]}. 하루 한 번 받아 .cache에 둔다."""
    path = CACHE / "listed.json"
    if path.exists() and time.time() - path.stat().st_mtime < 86400:
        return json.loads(path.read_text(encoding="utf-8"))
    z = zipfile.ZipFile(io.BytesIO(dart_get(f"{DART}/corpCode.xml?crtfc_key={key}")))
    listed = {}
    for e in ET.fromstring(z.read(z.namelist()[0])).iter("list"):
        sc = (e.findtext("stock_code") or "").strip()
        if sc:
            listed[(e.findtext("corp_name") or "").strip()] = [(e.findtext("corp_code") or "").strip(), sc]
    CACHE.mkdir(exist_ok=True)
    path.write_text(json.dumps(listed, ensure_ascii=False), encoding="utf-8")
    return listed


def dart_disclosures(key, corp_code, day):
    """그날 포함 최근 7일 공시 목록(최신순)."""
    end = datetime.strptime(day, "%Y%m%d")
    qs = urllib.parse.urlencode({"crtfc_key": key, "corp_code": corp_code, "page_count": 100,
                                 "bgn_de": (end - timedelta(days=6)).strftime("%Y%m%d"), "end_de": day})
    res = json.loads(dart_get(f"{DART}/list.json?{qs}"))
    return [{"date": f"{d['rcept_dt'][:4]}-{d['rcept_dt'][4:6]}-{d['rcept_dt'][6:]}",
             "title": re.sub(r"\s+", " ", d["report_nm"]).strip(), "rcept_no": d["rcept_no"]}
            for d in res.get("list", [])]


def collect(day=None):
    cid, secret = naver_keys()
    day = day or datetime.now(KST).strftime("%Y%m%d")
    watchlist = load_watchlist()
    dart_key = os.environ.get("DART_API_KEY")
    listed = dart_listed(dart_key) if dart_key else {}
    if not dart_key:
        print("DART_API_KEY가 없어 공시 조회는 건너뜁니다.")
    posts = {}
    for kw in KEYWORDS:
        for start in (1, 101, 201):  # 최신순 최대 300건
            items = search(kw, cid, secret, start).get("items", [])
            older = False
            for it in items:
                pd = it.get("postdate", "")
                if pd < day:
                    older = True
                    continue
                if pd != day:
                    continue
                link = it["link"]
                if link in posts:
                    continue
                title, desc = clean(it["title"]), clean(it["description"])
                if not any(k in title + " " + desc for k in KEYWORDS):
                    continue  # 네이버가 단어를 나눠 찾아 섞여 온 주담대·환율 글 제외
                posts[link] = {
                    "date": f"{pd[:4]}-{pd[4:6]}-{pd[6:]}", "title": title, "desc": desc, "link": link,
                    "blogger": clean(it.get("bloggername")), "bloglink": it.get("bloggerlink", ""),
                    "companies": match_companies(title, desc, watchlist),
                }
            if older or len(items) < 100:
                break
    disclosures = {}
    if dart_key:
        corp = {sc: cc for cc, sc in listed.values()}
        codes = {c["name"]: c["code"] for c in watchlist}
        for name in sorted({n for p in posts.values() for n in p["companies"]}):
            cc = corp.get(codes.get(name, ""))
            if cc:
                try:
                    disclosures[name] = dart_disclosures(dart_key, cc, day)
                except Exception as e:  # 공시 조회 실패로 수집 전체를 멈추지 않는다
                    print(f"  공시 {name}: {e}")
    DATA.mkdir(exist_ok=True)
    out = DATA / f"{day[:4]}-{day[4:6]}-{day[6:]}.json"
    payload = {"date": out.stem, "collected_at": datetime.now(KST).isoformat(timespec="minutes"),
               "posts": list(posts.values()), "disclosures": disclosures}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{out.name}: {len(posts)}건")


def backfill():
    """워치리스트 종목마다 '종목명 + 검색어'로 지난 글을 찾아 data/backfill/posts.json에 모은다.
    이미 찾은 종목(done)은 건너뛰므로 워치리스트에 종목을 늘린 뒤 다시 돌리면 새 종목만 검색한다.
    끝나면 모은 글 전체를 지금 워치리스트로 다시 분류한다."""
    cid, secret = naver_keys()
    watchlist = load_watchlist()
    out = DATA / "backfill" / "posts.json"
    old = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    posts = {p["link"]: p for p in old.get("posts", [])}
    done = set(old.get("done", []))
    todo = [c for c in watchlist if c["name"] not in done]
    print(f"새로 찾을 종목 {len(todo)}곳 (이미 찾은 종목 {len(done)}곳)")

    def save():
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {"collected_at": datetime.now(KST).isoformat(timespec="minutes"),
                   "done": sorted(done), "posts": list(posts.values())}
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    for i, c in enumerate(todo, 1):
        for kw in KEYWORDS:
            try:
                items = search(f"{c['name']} {kw}", cid, secret, 1).get("items", [])
            except Exception as e:  # 한 종목 실패로 전체를 멈추지 않는다
                print(f"  {c['name']} {kw}: {e}")
                continue
            for it in items:
                title, desc = clean(it["title"]), clean(it["description"])
                text = title + " " + desc
                if not any(k in text for k in KEYWORDS):
                    continue
                companies = match_companies(title, desc, watchlist)
                if c["name"] not in companies:
                    continue
                pd = it.get("postdate", "")
                posts.setdefault(it["link"], {
                    "date": f"{pd[:4]}-{pd[4:6]}-{pd[6:]}", "title": title, "desc": desc, "link": it["link"],
                    "blogger": clean(it.get("bloggername")), "bloglink": it.get("bloggerlink", ""),
                    "companies": companies,
                })
        done.add(c["name"])
        if i % 100 == 0:  # 중간에 멈춰도 이어서 돌릴 수 있게 저장
            save()
            print(f"{i}/{len(todo)} 종목, {len(posts)}건")
    for p in posts.values():
        p["companies"] = match_companies(p["title"], p["desc"], watchlist)
    posts = {k: p for k, p in posts.items() if p["companies"]}
    save()
    print(f"{out.relative_to(ROOT)}: {len(posts)}건")


# ---------- 페이지 생성 ----------

# 흰 종이 위의 통화 기록부 — 카드 없이 괘선으로 기업을 나누고, 색은 검색어 형광펜 하나만 쓴다(docs/plan.md).
CSS = """
:root{--paper:#fff;--ink:#1f2328;--pencil:#646b75;--rule:#e6e8eb;--wash:#f4f5f7;--marker:#fff0a8;color-scheme:light}
*{box-sizing:border-box}
html,body{background:var(--paper)}
body{margin:0;color:var(--ink);font:16px/1.6 "Pretendard Variable","Pretendard","Apple SD Gothic Neo","Noto Sans KR",system-ui,sans-serif;
  font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased;word-break:keep-all}
.wrap{max-width:1000px;margin:0 auto;padding:40px 24px 72px}
header.top{margin-bottom:20px}
h1{font-size:14px;font-weight:700;margin:0 0 16px;color:#d4281c}
.lead{font-size:30px;line-height:1.35;font-weight:700;letter-spacing:-.02em;margin:0;max-width:24em}
.when{margin:10px 0 0;font-size:14px;color:var(--pencil)}
.when a,.empty a{color:var(--ink);text-underline-offset:3px}
.empty nav.idx{margin:14px 0 0}.empty nav.idx a{text-decoration:none}
.search{position:sticky;top:0;z-index:2;display:block;background:var(--paper);padding:12px 0}
.search input{width:100%;font:inherit;font-size:16px;color:var(--ink);background:var(--wash);border:1px solid transparent;
  border-radius:12px;padding:13px 64px 13px 46px;outline:none;-webkit-appearance:none;appearance:none}
.search input::placeholder{color:var(--pencil)}
.search input::-webkit-search-cancel-button{display:none}
.search input:focus{background:var(--paper);border-color:var(--ink)}
.search svg{position:absolute;left:16px;top:50%;transform:translateY(-50%);color:var(--pencil);pointer-events:none}
.search .cnt{position:absolute;right:16px;top:50%;transform:translateY(-50%);font-size:13px;color:var(--pencil)}
nav.idx{display:flex;flex-wrap:wrap;gap:8px;margin:4px 0 32px}
nav.idx a{display:inline-flex;align-items:baseline;gap:6px;padding:5px 12px;border:1px solid var(--rule);border-radius:999px;
  color:var(--ink);text-decoration:none;font-size:14px;font-weight:500}
nav.idx a:hover{border-color:var(--ink)}
nav.idx a i{font-style:normal;font-size:13px;color:var(--pencil)}
nav.idx a.etc{color:var(--pencil)}
section.co{display:grid;grid-template-columns:200px minmax(0,1fr);gap:12px 40px;padding:24px 0;border-top:1px solid var(--rule);scroll-margin-top:76px}
.who{position:sticky;top:84px;align-self:start}
.who h2{font-size:18px;line-height:1.4;font-weight:700;margin:0}
.who span{display:block;font-size:13px;color:var(--pencil)}
.who .code{font-size:14px;margin-top:2px}
section.etc .who h2{color:var(--pencil)}
article+article{margin-top:22px}
article a.t{font-size:16px;line-height:1.5;font-weight:700;color:var(--ink);text-decoration:none;overflow-wrap:anywhere}
article a.t:hover{text-decoration:underline;text-underline-offset:3px}
article p{margin:6px 0 0;max-width:46em;font-size:14px;line-height:1.65;color:var(--pencil);overflow-wrap:anywhere;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
article .by{margin-top:6px;font-size:13px;color:var(--pencil)}
a:focus-visible{outline:2px solid var(--ink);outline-offset:3px;border-radius:4px}
mark{background:var(--marker);color:inherit;border-radius:2px}
.empty{padding:40px 0;border-top:1px solid var(--rule);color:var(--pencil)}
.archive{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 16px;margin-top:48px;padding-top:20px;border-top:1px solid var(--rule);font-size:14px}
.archive b{font-weight:700;margin-right:4px}
.archive a{color:var(--pencil);text-decoration:none}
.archive a:hover{color:var(--ink);text-decoration:underline;text-underline-offset:3px}
.archive a[aria-current]{color:var(--ink);font-weight:700}
footer{margin-top:16px;font-size:13px;color:var(--pencil)}
.surge{margin:0 0 20px;padding:14px 0;border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)}
.surge h2{font-size:14px;font-weight:700;margin:0 0 8px}
.surge ul{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:6px 28px}
.surge p{margin:0;font-size:14px;color:var(--pencil)}
.surge li{font-size:15px;font-weight:600}.surge li a{color:var(--ink)}
.surge li span{margin-left:8px;font-size:13px;font-weight:400;color:var(--pencil)}
.dis{margin:0 0 20px;padding:10px 14px;background:var(--wash);border-radius:10px;font-size:14px}
.dis b{display:block;font-size:13px;color:var(--pencil);margin-bottom:4px}
.dis ul{list-style:none;margin:0;padding:0}.dis li{line-height:1.7;overflow-wrap:anywhere}
.dis time{color:var(--pencil);margin-right:6px}.dis a{color:var(--ink);text-decoration:none}
.dis a:hover{text-decoration:underline;text-underline-offset:3px}
.hide{display:none!important}
@media (max-width:720px){
  .wrap{padding:28px 16px 56px}
  .lead{font-size:24px}
  section.co{grid-template-columns:minmax(0,1fr);gap:12px;padding:20px 0}
  .who{position:static;display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 10px}
  .who .code{margin-top:0}
}
"""

JS = """
const q=document.getElementById('q'),cnt=document.getElementById('cnt'),none=document.getElementById('none');
const secs=[...document.querySelectorAll('section.co')],chips=[...document.querySelectorAll('nav.idx a')];
secs.forEach(s=>s.querySelectorAll('article').forEach(a=>a.dataset.raw=a.innerHTML));
q.addEventListener('input',()=>{const v=q.value.trim().toLowerCase();let shown=0;
secs.forEach((s,i)=>{const head=s.querySelector('.who').textContent.toLowerCase();const coHit=!v||head.includes(v);let n=0;
s.querySelectorAll('article').forEach(a=>{a.innerHTML=a.dataset.raw;const hit=coHit||a.textContent.toLowerCase().includes(v);a.classList.toggle('hide',!hit);if(hit){n++;if(v)mark(a,v)}});
s.classList.toggle('hide',n===0);if(chips[i])chips[i].classList.toggle('hide',n===0);shown+=n});
cnt.textContent=v?shown+'건':'';none.classList.toggle('hide',shown>0||!v);
document.getElementById('all').href='search.html?q='+encodeURIComponent(q.value.trim());
sug.innerHTML='';if(v&&!shown)suggest(v)});
// 이날 글에 없으면 전체 기간 데이터(search.json)에서 맞는 기업을 글 수와 함께 보여 준다
const sug=document.getElementById('sug');let S=null;
const E=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function suggest(v){const go=()=>{if(q.value.trim().toLowerCase()!==v)return;
const hits=S.companies.map((c,i)=>[c,i]).filter(([c])=>c[0].toLowerCase().includes(v)||c[1].startsWith(v)||(c[4]||'').toLowerCase().includes(v))
.sort((a,b)=>S.n[b[1]]-S.n[a[1]]).slice(0,8);
sug.innerHTML=hits.map(([c,i])=>'<a href="search.html?q='+encodeURIComponent(c[0])+'">'+E(c[0])+'<i>'+S.n[i]+'</i></a>').join('')};
if(S)return go();fetch('search.json?v=__V__').then(r=>r.json()).then(d=>{S=d;S.n=d.companies.map(()=>0);
d.posts.forEach(p=>p[4].forEach(c=>S.n[c]++));go()}).catch(()=>{})}
function mark(el,v){const w=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);const ns=[];while(w.nextNode())ns.push(w.currentNode);
ns.forEach(t=>{const i=t.data.toLowerCase().indexOf(v);if(i<0)return;const r=document.createRange();r.setStart(t,i);r.setEnd(t,i+v.length);const m=document.createElement('mark');r.surroundContents(m)})}
"""


def esc(s):
    return html.escape(s or "", quote=True)


RECENT = 5  # 기업마다 보여 줄 최근 글 수(그날 글이 더 많으면 그날 글은 모두)


def surges(history, date, top=5):
    """최근 7일 글 수가 앞선 4주 주평균의 2배 이상이고 3건 이상인 기업.
    지난 글 채워넣기는 검색어마다 최신 100건까지라, 글이 100건 넘게 쌓였는데 5주 전까지 닿지 않으면
    기준 기간이 잘렸을 수 있어 뺀다."""
    d = datetime.strptime(date, "%Y-%m-%d")
    w0, b0 = (d - timedelta(days=6)).strftime("%F"), (d - timedelta(days=34)).strftime("%F")
    out = []
    for name, posts in history.items():
        if posts[-1]["date"] > b0 and len(posts) >= 100:
            continue
        now = sum(1 for p in posts if w0 <= p["date"] <= date)
        base = sum(1 for p in posts if b0 <= p["date"] < w0) / 4
        if now >= 3 and now >= 2 * base:
            out.append((name, now, base))
    return sorted(out, key=lambda x: (-x[1] / max(x[2], 0.5), -x[1]))[:top]


def render_day(data, watchlist, archive_dates, history):
    codes = {c["name"]: c["code"] for c in watchlist}
    dis = data.get("disclosures", {})
    sectors = {c["name"]: c["sector"] for c in watchlist}
    order = [c["name"] for c in watchlist]
    groups = {}
    for p in data["posts"]:
        p = {**p, "date": p.get("date") or data["date"]}
        for name in (p["companies"] or ["기타"]):
            groups.setdefault(name, []).append(p)
    recent = {}  # 그날 글 + 그 전 글을 합쳐 최신순 최근 5건
    for k, today in groups.items():
        links = {p["link"] for p in today}
        past = [p for p in history.get(k, []) if p["date"] < data["date"] and p["link"] not in links]
        recent[k] = today + past[:max(0, RECENT - len(today))]
    named = sorted([k for k in groups if k != "기타"],
                   key=lambda k: (-len(groups[k]), order.index(k) if k in order else 999))
    keys = named + (["기타"] if "기타" in groups else [])

    def label(k):
        return "기업 미확인" if k == "기타" else esc(k)

    etc_cls = ' class="etc"'
    chips = "".join(f'<a href="#c{i}"{etc_cls if k == "기타" else ""}>{label(k)}<i>{len(groups[k])}</i></a>'
                    for i, k in enumerate(keys))
    secs = []
    for i, k in enumerate(keys):
        arts = "".join(
            f'<article><a class="t" href="{esc(p["link"])}" target="_blank" rel="noopener">{esc(p["title"])}</a>'
            f'<p>{esc(p["desc"])}</p><div class="by"><time>{esc(p["date"])}</time> · {esc(p["blogger"])}</div></article>'
            for p in recent[k])
        if dis.get(k):
            items = "".join(f'<li><time>{esc(x["date"][5:])}</time> <a href="https://dart.fss.or.kr/dsaf001/main.do?rcpNo='
                            f'{esc(x["rcept_no"])}" target="_blank" rel="noopener">{esc(x["title"])}</a></li>'
                            for x in dis[k][:5])
            arts = f'<div class="dis"><b>최근 7일 공시</b><ul>{items}</ul></div>' + arts
        if k == "기타":
            meta = '<span>기업 이름을 찾지 못한 글</span>'
        else:
            meta = f'<span class="code">{esc(codes.get(k))}</span>' if codes.get(k) else ""
            meta += f'<span>{esc(sectors[k])}</span>' if sectors.get(k) else ""
        secs.append(f'<section class="co{" etc" if k == "기타" else ""}" id="c{i}">'
                    f'<div class="who"><h2>{label(k)}</h2>{meta}</div><div class="posts">{arts}</div></section>')
    body = "".join(secs) or '<div class="empty">아래 지난 정리에서 다른 날짜를 볼 수 있습니다.</div>'
    current = ' aria-current="page"'
    arch = "".join(f'<a href="{d}.html"{current if d == data["date"] else ""}>{d}</a>'
                   for d in archive_dates[:30])

    n, m = len(data["posts"]), len(named)
    if not n:
        lead = "이날 올라온 주담통화 후기가 없습니다."
    else:
        lead = f"주담통화 후기 {n}건이 올라왔고, 워치리스트 기업은 {f'{m}곳입니다' if m else '없습니다'}."
    sv = surges(history, data["date"])
    surge = ('<section class="surge"><h2>최근 7일 언급 급증</h2><p>앞선 4주보다 2배 넘게 늘어난 기업은 없습니다.</p></section>'
             if history else "")
    if sv:
        anchor = {k: i for i, k in enumerate(keys)}

        def link(k):
            return f'<a href="#c{anchor[k]}">{esc(k)}</a>' if k in anchor else esc(k)
        rows = "".join(
            f'<li>{link(k)}'
            f'<span>7일 {now}건 · 앞선 4주 주평균 {base:g}건</span></li>' for k, now, base in sv)
        surge = f'<section class="surge"><h2>최근 7일 언급 급증</h2><ul>{rows}</ul></section>'

    day = datetime.strptime(data["date"], "%Y-%m-%d")
    when = f"{day.year}년 {day.month}월 {day.day}일 {'월화수목금토일'[day.weekday()]}요일 {data['collected_at'][11:16]} 수집"
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light">
<title>주담통화 데일리 {data['date']}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
<style>{CSS}</style></head><body><div class="wrap">
<header class="top"><h1>주담통화 데일리</h1><p class="lead">{lead}</p><p class="when">{when} · <a href="search.html">상장사 전체 기간 검색</a></p></header>{surge}
<label class="search"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
<input id="q" type="search" placeholder="기업명, 종목코드, 키워드로 찾기" aria-label="글 검색" autocomplete="off"><span class="cnt" id="cnt" aria-live="polite"></span></label>
<nav class="idx" aria-label="기업 바로가기">{chips}</nav>{body}<div class="empty hide" id="none">이날 글에는 찾는 말이 없습니다. <a id="all" href="search.html">전체 기간에서 찾기</a><nav class="idx" id="sug" aria-label="전체 기간에서 찾은 기업"></nav></div>
<nav class="archive" aria-label="지난 정리"><b>지난 정리</b>{arch}</nav>
<footer>네이버 블로그 검색 결과의 제목, 요약, 링크만 보여 줍니다. 내용은 각 블로거의 의견이며 투자 판단의 근거가 아닙니다.</footer>
</div><script>{JS}</script></body></html>"""


SEARCH_JS = """
const q=document.getElementById('q'),cnt=document.getElementById('cnt'),pick=document.getElementById('pick'),out=document.getElementById('out');
let D=null;const E=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
fetch('search.json?v=__V__').then(r=>r.json()).then(d=>{D=d;D.byCo=d.companies.map(()=>[]);
d.posts.forEach((p,i)=>p[4].forEach(c=>D.byCo[c].push(i)));
const v=new URLSearchParams(location.search).get('q');if(v){q.value=v}run()})
.catch(()=>{out.innerHTML='<p class="none">검색 데이터를 읽지 못했습니다. 파일을 바로 열면(file://) 브라우저가 막으니 웹 주소로 열어 주세요.</p>'});
const art=p=>'<article><a class="t" href="'+E(p[2])+'" target="_blank" rel="noopener">'+E(p[1])+'</a><div class="by"><time>'+E(p[0])+'</time> · '+E(p[3])+'</div></article>';
function show(ci){const c=D.companies[ci],ps=D.byCo[ci];
[...pick.children].forEach(a=>a.setAttribute('aria-pressed',a.dataset.i==ci));
const meta='<span class="code">'+E(c[1])+'</span><span>'+E(c[2])+(c[3]?' · '+E(c[3]):'')+'</span>';
out.innerHTML='<section class="co"><div class="who"><h2>'+E(c[0])+'</h2>'+meta+'</div><div class="posts">'+
(ps.length?ps.map(i=>art(D.posts[i])).join(''):'<p class="none">모아 둔 주담통화 글이 없습니다.</p>')+'</div></section>';
cnt.textContent=ps.length+'건'}
function run(){if(!D)return;const v=q.value.trim().toLowerCase();
history.replaceState(null,'',v?'?q='+encodeURIComponent(q.value.trim()):location.pathname);
if(!v){pick.innerHTML='';cnt.textContent='';out.innerHTML='<p class="none">기업명이나 종목코드를 넣으면 그 회사의 주담통화 후기를 최신순으로 보여 줍니다.</p>';return}
const hits=[];D.companies.forEach((c,i)=>{const n=c[0].toLowerCase(),al=(c[4]||'').toLowerCase();
let r=n===v||c[1]===v?0:n.startsWith(v)||c[1].startsWith(v)?1:n.includes(v)||al.includes(v)?2:-1;if(r>=0)hits.push([r,-D.byCo[i].length,i])});
hits.sort((a,b)=>a[0]-b[0]||a[1]-b[1]);
pick.innerHTML=hits.slice(0,12).map(h=>'<a href="#" data-i="'+h[2]+'">'+E(D.companies[h[2]][0])+'<i>'+D.byCo[h[2]].length+'</i></a>').join('');
if(hits.length){show(hits[0][2]);return}
const ps=D.posts.filter(p=>p[1].toLowerCase().includes(v));cnt.textContent=ps.length+'건';
out.innerHTML=ps.length?'<section class="co"><div class="who"><h2>제목 검색</h2><span>상장사 이름이 아니라 제목에서 찾은 글</span></div><div class="posts">'+ps.slice(0,200).map(art).join('')+'</div></section>'
:'<p class="none">찾는 말이 들어간 기업이나 글이 없습니다.</p>'}
let t;q.addEventListener('input',()=>{clearTimeout(t);t=setTimeout(run,120)});
pick.addEventListener('click',e=>{const a=e.target.closest('a');if(a){e.preventDefault();show(+a.dataset.i)}});
"""


def render_search(n_posts, n_cos, updated):
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light">
<title>주담통화 전체 기간 검색</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
<style>{CSS}
nav.idx a[aria-pressed=true]{{border-color:var(--ink);font-weight:700}}
.none{{margin:0;padding:32px 0;border-top:1px solid var(--rule);color:var(--pencil)}}
section.co .none{{padding:0;border:0}}</style></head><body><div class="wrap">
<header class="top"><h1><a href="index.html" style="color:inherit;text-decoration:none">주담통화 데일리</a></h1>
<p class="lead">상장사 {n_cos:,}곳의 주담통화 후기를 찾아봅니다.</p>
<p class="when">모아 둔 글 {n_posts:,}건 · {updated} 기준 · <a href="index.html">오늘 정리로</a></p></header>
<label class="search"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
<input id="q" type="search" placeholder="기업명, 종목코드로 찾기" aria-label="기업 검색" autocomplete="off" autofocus><span class="cnt" id="cnt" aria-live="polite"></span></label>
<nav class="idx" id="pick" aria-label="찾은 기업"></nav><div id="out"></div>
<footer>네이버 블로그 검색 결과의 제목, 날짜, 링크만 보여 줍니다. 내용은 각 블로거의 의견이며 투자 판단의 근거가 아닙니다.</footer>
</div><script>{SEARCH_JS}</script></body></html>"""


def build():
    watchlist = load_watchlist()
    files = sorted(DATA.glob("*.json"), reverse=True)
    SITE.mkdir(exist_ok=True)
    dates = [f.stem for f in files]
    days = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    # 기업별 전체 글 모음(날짜별 파일 + 지난 글 채워넣기), 링크 중복 제거 후 최신순
    pool = {}
    sources = [(d["posts"], d["date"]) for d in days]
    bf = DATA / "backfill" / "posts.json"
    if bf.exists():
        sources.append((json.loads(bf.read_text(encoding="utf-8"))["posts"], ""))
    for posts, day in sources:
        for p in posts:
            pool.setdefault(p["link"], {**p, "date": p.get("date") or day})
    history = {}
    for p in sorted(pool.values(), key=lambda p: p["date"], reverse=True):
        for name in p["companies"]:
            history.setdefault(name, []).append(p)
    # 전체 기간 검색 데이터: 상장사 목록 + 글(설명은 빼서 가볍게), 최신순
    idx = {c["name"]: i for i, c in enumerate(watchlist)}
    rows = [[p["date"], p["title"], p["link"], p.get("blogger", ""), [idx[n] for n in p["companies"] if n in idx]]
            for p in sorted(pool.values(), key=lambda p: p["date"], reverse=True)]
    rows = [r for r in rows if r[4]]
    cos = [[c["name"], c["code"], c["market"], c["sector"], ";".join(c["aliases"])] for c in watchlist]
    (SITE / "search.json").write_text(json.dumps({"companies": cos, "posts": rows}, ensure_ascii=False,
                                                 separators=(",", ":")), encoding="utf-8")
    stamp = datetime.now(KST).strftime("%Y%m%d%H%M%S")  # 브라우저가 옛 search.json을 쓰지 않게
    (SITE / "search.html").write_text(render_search(len(rows), len(watchlist), dates[0] if dates else "-")
                                      .replace("__V__", stamp), encoding="utf-8")
    for i, (f, data) in enumerate(zip(files, days)):
        page = render_day(data, watchlist, dates, history).replace("__V__", stamp)
        (SITE / f"{f.stem}.html").write_text(page, encoding="utf-8")
        if i == 0:
            (SITE / "index.html").write_text(page, encoding="utf-8")
    if not files:
        (SITE / "index.html").write_text("<!doctype html><meta charset=utf-8><p>아직 데이터가 없습니다.", encoding="utf-8")
    print(f"site/: {len(files)}일치")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "collect":
        collect(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "watchlist":
        update_watchlist()
    elif cmd == "backfill":
        backfill()
    elif cmd == "build":
        build()
    else:
        print(__doc__)
