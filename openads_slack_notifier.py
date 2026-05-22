#!/usr/bin/env python3
"""
마케팅 커뮤니티 신규 콘텐츠 → Slack 알림 자동화 스크립트

지원 사이트:
    - 오픈애즈 (openads.co.kr)
    - 캐릿 (careet.net)

사용법:
    1. 아래 SLACK_WEBHOOK_URL에 본인의 Slack Incoming Webhook URL을 입력
    2. (선택) ANTHROPIC_API_KEY에 Claude API 키 입력 시 AI 요약 활성화
    3. python openads_slack_notifier.py 실행

의존성 설치:
    pip install requests beautifulsoup4 anthropic
"""

import json
import os
import sys
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ============================================================
# 설정 (여기만 수정하세요!)
# ============================================================

# Slack Incoming Webhook URL (필수)
SLACK_WEBHOOK_URL = os.environ.get(
    "SLACK_WEBHOOK_URL",
    ""
)

# Anthropic API Key (선택 - 없으면 meta description 기반 요약 사용)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# 카카오 알림 설정 (나에게 보내기)
KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY", "")
KAKAO_REFRESH_TOKEN = os.environ.get("KAKAO_REFRESH_TOKEN", "")

# 데이터 저장 경로 (이미 본 콘텐츠 ID 기록)
DATA_DIR = Path(__file__).parent / "data"
SEEN_FILE = DATA_DIR / "seen_contents.json"

# 요청 헤더 (브라우저처럼 보이게)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(DATA_DIR / "notifier.log" if DATA_DIR.exists() else "notifier.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# 한국 표준시 (KST = UTC+9)
KST = timezone(timedelta(hours=9))


# ============================================================
# 날짜 유틸리티
# ============================================================

def extract_pub_date(soup) -> "datetime | None":
    """HTML soup에서 콘텐츠 발행일을 추출합니다.
    다음 순서로 시도합니다:
    1. article:published_time / og:article:published_time meta 태그
    2. name="date" 계열 meta 태그
    3. JSON-LD datePublished / dateCreated
    4. <time datetime="..."> 요소
    """
    # 1. property meta 태그
    for prop in ["article:published_time", "og:article:published_time",
                 "article:modified_time"]:
        tag = soup.find("meta", attrs={"property": prop})
        if tag:
            val = tag.get("content", "").strip()
            if val:
                try:
                    return datetime.fromisoformat(val.replace("Z", "+00:00"))
                except ValueError:
                    pass

    # 2. name meta 태그
    for name in ["date", "publish-date", "pubdate", "DC.date",
                 "article:published", "published_time"]:
        tag = soup.find("meta", attrs={"name": name})
        if tag:
            val = tag.get("content", "").strip()
            if val:
                try:
                    return datetime.fromisoformat(val.replace("Z", "+00:00"))
                except ValueError:
                    pass

    # 3. JSON-LD
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            raw = script.string or ""
            data = json.loads(raw)
            # @graph 배열 처리
            items = data if isinstance(data, list) else data.get("@graph", [data])
            for item in items:
                if not isinstance(item, dict):
                    continue
                for key in ["datePublished", "dateCreated", "dateModified"]:
                    val = item.get(key, "")
                    if val:
                        try:
                            return datetime.fromisoformat(
                                str(val).replace("Z", "+00:00"))
                        except ValueError:
                            pass
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
            pass

    # 4. <time datetime="..."> 요소
    for time_el in soup.find_all("time", attrs={"datetime": True}):
        val = time_el.get("datetime", "").strip()
        if val:
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            except ValueError:
                pass

    return None


def is_today_kst(dt: datetime) -> bool:
    """datetime이 오늘(KST 기준) 날짜인지 확인합니다."""
    now_kst = datetime.now(KST)
    dt_kst = dt.astimezone(KST) if dt.tzinfo else dt.replace(tzinfo=KST)
    return dt_kst.date() == now_kst.date()


# ============================================================
# 1-A. 오픈애즈 크롤러
# ============================================================

OPENADS_BASE_URL = "https://www.openads.co.kr"
OPENADS_LATEST_URL = f"{OPENADS_BASE_URL}/latest"


def fetch_openads_contents() -> list[dict]:
    """오픈애즈 /latest 페이지에서 콘텐츠 카드 목록을 파싱합니다."""
    logger.info("[오픈애즈] 최신 콘텐츠 페이지 요청 중...")

    try:
        resp = requests.get(OPENADS_LATEST_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"[오픈애즈] 페이지 요청 실패: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    content_links = soup.select('a[href*="contentDetail"]')

    seen_ids = set()
    contents = []

    for link in content_links:
        href = link.get("href", "")
        conts_id = None
        if "contsId=" in href:
            try:
                conts_id = href.split("contsId=")[1].split("&")[0]
            except IndexError:
                continue

        if not conts_id or conts_id in seen_ids:
            continue
        seen_ids.add(conts_id)

        title = link.get_text(strip=True)
        if not title:
            continue

        category = _extract_openads_category(link)

        contents.append({
            "id": f"openads_{conts_id}",
            "source": "오픈애즈",
            "title": title,
            "category": category,
            "url": f"{OPENADS_BASE_URL}/content/contentDetail?contsId={conts_id}",
            "_contsId": conts_id,
        })

    # 최신 20개만 처리 (오래된 항목 필터링 비용 절감)
    contents = contents[:20]
    logger.info(f"[오픈애즈] 총 {len(contents)}개 콘텐츠 발견")
    return contents


def _extract_openads_category(link_element) -> str:
    """링크 요소 주변에서 카테고리/소스 텍스트를 추출합니다."""
    container = link_element
    for _ in range(8):
        if container.parent:
            container = container.parent

    title_text = link_element.get_text(strip=True)[:10]
    for el in container.find_all(["span", "small", "p"]):
        text = el.get_text(strip=True)
        if 1 < len(text) < 30 and title_text not in text:
            return text
    return ""


def fetch_openads_detail(conts_id: str) -> dict:
    """오픈애즈 상세 페이지에서 메타데이터를 추출합니다."""
    url = f"{OPENADS_BASE_URL}/content/contentDetail?contsId={conts_id}"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"[오픈애즈] 상세 페이지 요청 실패 (contsId={conts_id}): {e}")
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")

    description = ""
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc:
        description = meta_desc.get("content", "").strip()

    thumbnail = ""
    og_image = soup.find("meta", attrs={"property": "og:image"})
    if og_image:
        thumbnail = og_image.get("content", "").strip()

    body_text = ""
    for selector in [".content-body", ".article-body", ".detail-content",
                     "[class*='content']", "article", ".post-content"]:
        body_el = soup.select_one(selector)
        if body_el and len(body_el.get_text(strip=True)) > 100:
            body_text = body_el.get_text(strip=True)[:2000]
            break

    tags = []
    for tag_el in soup.select('a[href*="category"], .tag, [class*="tag"], [class*="keyword"]'):
        tag_text = tag_el.get_text(strip=True)
        if 0 < len(tag_text) < 25 and tag_text not in tags:
            tags.append(tag_text)

    pub_date = extract_pub_date(soup)

    return {
        "description": description,
        "thumbnail": thumbnail,
        "body_text": body_text,
        "tags": tags[:5],
        "pub_date": pub_date,
    }


# ============================================================
# 1-B. 캐릿(Careet) 크롤러
# ============================================================

CAREET_BASE_URL = "https://www.careet.net"
CAREET_ALL_URL = f"{CAREET_BASE_URL}/Content/All"


def fetch_careet_contents() -> list[dict]:
    """캐릿 /Content/All 페이지에서 콘텐츠 목록을 파싱합니다."""
    logger.info("[캐릿] 최신 콘텐츠 페이지 요청 중...")

    try:
        resp = requests.get(CAREET_ALL_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"[캐릿] 페이지 요청 실패: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # 숫자 ID 형태의 링크 추출 (예: /1885)
    import re
    content_links = soup.find_all("a", href=re.compile(r"^/\d+$"))

    seen_ids = set()
    contents = []

    for link in content_links:
        href = link.get("href", "")
        content_id = href.lstrip("/")

        if not content_id or content_id in seen_ids:
            continue
        seen_ids.add(content_id)

        # 제목 추출 (카드 내 텍스트)
        title = link.get_text(strip=True)
        if not title or len(title) < 5:
            continue

        # 제목 정리 (줄바꿈 등 제거)
        title = " ".join(title.split())

        # 썸네일 이미지 추출 (절대 URL만 허용)
        img = link.find("img")
        raw_thumb = img.get("src", "") if img else ""
        thumbnail = raw_thumb if raw_thumb.startswith("http") else ""

        # 카테고리 추출 (부모 요소에서)
        category = _extract_careet_category(link)

        contents.append({
            "id": f"careet_{content_id}",
            "source": "캐릿",
            "title": title,
            "category": category,
            "url": f"{CAREET_BASE_URL}/{content_id}",
            "_contentId": content_id,
            "thumbnail": thumbnail,
        })

    # 최신 20개만 처리 (오래된 항목 필터링 비용 절감)
    contents = contents[:20]
    logger.info(f"[캐릿] 총 {len(contents)}개 콘텐츠 발견")
    return contents


def _extract_careet_category(link_element) -> str:
    """캐릿 링크 요소에서 카테고리를 추출합니다."""
    # 부모 컨테이너에서 시리즈명/카테고리 찾기
    container = link_element.parent
    if container:
        for el in container.find_all(["span", "small", "p", "div"]):
            text = el.get_text(strip=True)
            # 짧은 태그성 텍스트를 카테고리로 간주
            if 1 < len(text) < 20:
                # 제목과 동일하지 않은지 확인
                title_start = link_element.get_text(strip=True)[:10]
                if title_start not in text:
                    return text
    return ""


def fetch_careet_detail(content_id: str) -> dict:
    """캐릿 상세 페이지에서 메타데이터를 추출합니다."""
    url = f"{CAREET_BASE_URL}/{content_id}"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"[캐릿] 상세 페이지 요청 실패 (id={content_id}): {e}")
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")

    # og:title → 제목 보정용
    og_title = ""
    og_title_tag = soup.find("meta", attrs={"property": "og:title"})
    if og_title_tag:
        og_title = og_title_tag.get("content", "").strip()

    # og:description → 요약용
    description = ""
    og_desc = soup.find("meta", attrs={"property": "og:description"})
    if og_desc:
        description = og_desc.get("content", "").strip()
    if not description:
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc:
            description = meta_desc.get("content", "").strip()

    # og:image → 썸네일 (절대 URL만 허용)
    thumbnail = ""
    og_image = soup.find("meta", attrs={"property": "og:image"})
    if og_image:
        raw = og_image.get("content", "").strip()
        thumbnail = raw if raw.startswith("http") else ""

    # 본문 텍스트 (AI 요약용)
    body_text = ""
    for selector in [".content-detail", ".article-body", ".post-content",
                     "[class*='detail']", "article", ".viewer"]:
        body_el = soup.select_one(selector)
        if body_el and len(body_el.get_text(strip=True)) > 100:
            body_text = body_el.get_text(strip=True)[:2000]
            break

    pub_date = extract_pub_date(soup)

    return {
        "og_title": og_title,
        "description": description,
        "thumbnail": thumbnail,
        "body_text": body_text,
        "tags": [],
        "pub_date": pub_date,
    }


# ============================================================
# 2. AI 요약 (선택 — Anthropic API 키가 있을 때만 동작)
# ============================================================

def generate_summary(title: str, description: str, body_text: str) -> str:
    """
    콘텐츠를 3~4줄로 요약합니다.
    - API 키가 없으면 meta description을 정리해서 반환
    - API 키가 있으면 Claude API로 요약 생성
    """
    if not ANTHROPIC_API_KEY:
        if description:
            sentences = description.replace("\n", " ").strip()
            if len(sentences) > 200:
                sentences = sentences[:200] + "..."
            return sentences
        return "요약을 가져올 수 없습니다."

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        text_to_summarize = body_text if body_text else description
        if not text_to_summarize:
            return "요약을 가져올 수 없습니다."

        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": f"""다음 마케팅 콘텐츠를 3~4줄로 간결하게 요약해주세요.
핵심 내용과 인사이트를 중심으로 작성하고, 한국어로 답변해주세요.

제목: {title}
본문: {text_to_summarize[:1500]}"""
            }]
        )
        return message.content[0].text.strip()
    except Exception as e:
        logger.warning(f"AI 요약 실패, 기본 요약 사용: {e}")
        if description:
            return description[:200] + ("..." if len(description) > 200 else "")
        return "요약을 가져올 수 없습니다."


# ============================================================
# 3. 카카오톡 나에게 보내기
# ============================================================

def get_kakao_access_token() -> str:
    """KAKAO_REFRESH_TOKEN으로 새 access_token을 발급받습니다."""
    if not KAKAO_REST_API_KEY or not KAKAO_REFRESH_TOKEN:
        return ""
    try:
        resp = requests.post(
            "https://kauth.kakao.com/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": KAKAO_REST_API_KEY,
                "refresh_token": KAKAO_REFRESH_TOKEN,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            token = data.get("access_token", "")
            logger.info("카카오 액세스 토큰 발급 성공")
            return token
        else:
            logger.error(f"카카오 토큰 발급 실패: {resp.status_code} - {resp.text}")
            return ""
    except requests.RequestException as e:
        logger.error(f"카카오 토큰 발급 오류: {e}")
        return ""


def send_kakao_notification(contents: list[dict], source_name: str, source_url: str) -> bool:
    """신규 콘텐츠 목록을 카카오톡 나에게 보내기로 전송합니다."""
    if not contents:
        return True
    if not KAKAO_REST_API_KEY or not KAKAO_REFRESH_TOKEN:
        logger.info("카카오 환경변수 미설정 — 카카오 알림 건너뜀")
        return True

    access_token = get_kakao_access_token()
    if not access_token:
        return False

    # 소스별 이모지
    emoji_map = {"오픈애즈": "📢", "캐릿": "🥕"}
    emoji = emoji_map.get(source_name, "📣")

    # 20개씩 나눠서 전송
    CHUNK_SIZE = 20
    all_success = True

    for chunk_start in range(0, len(contents), CHUNK_SIZE):
        chunk = contents[chunk_start:chunk_start + CHUNK_SIZE]
        lines = [f"{emoji} *{source_name} 신규 콘텐츠 {len(chunk)}건*\n"]
        for i, item in enumerate(chunk, 1):
            title = item.get("title", "")
            url = item.get("url", "")
            category = item.get("category", "")
            summary = item.get("summary", "")
            cat_str = f"[{category}] " if category else ""
            summary_str = f"\n{summary[:80]}..." if len(summary) > 80 else (f"\n{summary}" if summary else "")
            lines.append(f"{i}. {cat_str}{title}{summary_str}\n🔗 {url}\n")

        lines.append(f"\n🕐 {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST")
        text = "\n".join(lines)[:2000]  # 카카오 텍스트 최대 2000자

        payload = {
            "template_object": json.dumps({
                "object_type": "text",
                "text": text,
                "link": {
                    "web_url": source_url,
                    "mobile_web_url": source_url,
                },
                "button_title": f"{source_name} 바로가기",
            })
        }

        try:
            resp = requests.post(
                "https://kapi.kakao.com/v2/api/talk/memo/default/send",
                headers={"Authorization": f"Bearer {access_token}"},
                data=payload,
                timeout=10,
            )
            if resp.status_code == 200 and resp.json().get("result_code") == 0:
                logger.info(f"[{source_name}] 카카오톡 전송 성공! ({len(chunk)}건)")
            else:
                logger.error(f"[{source_name}] 카카오톡 전송 실패: {resp.status_code} - {resp.text}")
                all_success = False
        except requests.RequestException as e:
            logger.error(f"[{source_name}] 카카오톡 전송 오류: {e}")
            all_success = False

        if chunk_start + CHUNK_SIZE < len(contents):
            time.sleep(1)

    return all_success


# ============================================================
# 4. Slack 메시지 전송 (Block Kit 포맷)
# ============================================================

def _slack_escape(text: str) -> str:
    """Slack mrkdwn 링크 라벨에 사용할 수 없는 특수문자를 이스케이프합니다."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_slack_notification(contents: list[dict], source_name: str, source_url: str) -> bool:
    """
    신규 콘텐츠 목록을 Slack Block Kit 형태로 전송합니다.
    Slack은 메시지당 최대 50블록 제한이 있으므로 20개씩 나눠 전송합니다.
    """
    if not contents:
        logger.info(f"[{source_name}] 전송할 신규 콘텐츠 없음")
        return True

    if SLACK_WEBHOOK_URL == "YOUR_SLACK_WEBHOOK_URL_HERE":
        logger.error("Slack Webhook URL이 설정되지 않았습니다!")
        return False

    # 소스별 이모지
    emoji_map = {"오픈애즈": "📢", "캐릿": "🥕"}
    emoji = emoji_map.get(source_name, "📣")

    # Slack 50블록 제한 → 20개씩 청크로 분할
    CHUNK_SIZE = 20
    total = len(contents)
    all_success = True

    for chunk_idx, chunk_start in enumerate(range(0, total, CHUNK_SIZE)):
        chunk = contents[chunk_start:chunk_start + CHUNK_SIZE]
        chunk_label = f" ({chunk_start+1}~{chunk_start+len(chunk)}/{total})" if total > CHUNK_SIZE else f" ({total}건)"

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} {source_name} 신규 콘텐츠{chunk_label}",
                    "emoji": True,
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')} 업데이트",
                    }
                ],
            },
            {"type": "divider"},
        ]

        for item in chunk:
            safe_title = _slack_escape(item['title'])
            safe_category = _slack_escape(item.get('category', ''))
            safe_summary = _slack_escape(item.get('summary', ''))
            section_text = (
                f"*<{item['url']}|{safe_title}>*\n"
                f"{'📁 ' + safe_category if safe_category else ''}"
                f"{'  |  🏷️ ' + ', '.join(item.get('tags', [])) if item.get('tags') else ''}\n\n"
                f"{safe_summary}"
            )[:3000]

            section = {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": section_text,
                },
            }

            thumbnail_url = item.get("thumbnail", "")
            if thumbnail_url and thumbnail_url.startswith("http"):
                alt = item["title"][:75].strip() or "thumbnail"
                section["accessory"] = {
                    "type": "image",
                    "image_url": thumbnail_url,
                    "alt_text": alt,
                }

            blocks.append(section)
            blocks.append({"type": "divider"})

        # 푸터
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"💡 <{source_url}|{source_name}에서 더 보기>",
                }
            ],
        })

        payload = {"blocks": blocks}

        try:
            resp = requests.post(
                SLACK_WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info(f"[{source_name}] Slack 전송 성공! (청크 {chunk_idx+1}: {len(chunk)}건)")
            else:
                logger.error(f"[{source_name}] Slack 전송 실패: {resp.status_code} - {resp.text}")
                all_success = False
        except requests.RequestException as e:
            logger.error(f"[{source_name}] Slack 전송 오류: {e}")
            all_success = False

        if chunk_start + CHUNK_SIZE < total:
            time.sleep(1)  # 청크 간 짧은 딜레이

    return all_success


# ============================================================
# 4. 중복 관리 (seen.json)
# ============================================================

def load_seen_ids() -> set:
    """이미 알림을 보낸 콘텐츠 ID 목록을 불러옵니다."""
    if SEEN_FILE.exists():
        try:
            data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
            return set(data.get("seen_ids", []))
        except (json.JSONDecodeError, KeyError):
            return set()
    return set()


def save_seen_ids(seen_ids: set):
    """알림을 보낸 콘텐츠 ID 목록을 저장합니다."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "seen_ids": sorted(seen_ids),
        "last_updated": datetime.now().isoformat(),
    }
    SEEN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"seen_contents.json 업데이트 완료 (총 {len(seen_ids)}개 기록)")


# ============================================================
# 5. 메인 실행
# ============================================================

def process_openads(seen_ids: set) -> tuple[bool, set]:
    """오픈애즈 콘텐츠를 처리합니다."""
    all_contents = fetch_openads_contents()
    if not all_contents:
        logger.warning("[오픈애즈] 콘텐츠를 가져오지 못했습니다.")
        return True, set()

    new_contents = [c for c in all_contents if c["id"] not in seen_ids]
    if not new_contents:
        logger.info("[오픈애즈] 신규 콘텐츠 없음")
        return True, set()

    logger.info(f"[오픈애즈] 🆕 신규 콘텐츠 {len(new_contents)}건 발견! 날짜 확인 중...")

    today_str = datetime.now(KST).strftime("%Y-%m-%d")
    today_contents = []
    skipped_ids = set()  # 날짜 지난 항목도 seen에 추가해 재처리 방지

    for i, content in enumerate(new_contents):
        logger.info(f"  [{i+1}/{len(new_contents)}] 상세 정보 수집: {content['title'][:40]}...")
        detail = fetch_openads_detail(content["_contsId"])

        content["thumbnail"] = detail.get("thumbnail", "")
        content["tags"] = detail.get("tags", [])
        content["summary"] = generate_summary(
            content["title"],
            detail.get("description", ""),
            detail.get("body_text", ""),
        )

        pub_date = detail.get("pub_date")
        if pub_date is not None:
            if is_today_kst(pub_date):
                pub_str = pub_date.astimezone(KST).strftime("%Y-%m-%d")
                logger.info(f"    ✅ 오늘 발행 ({pub_str}): {content['title'][:40]}")
                today_contents.append(content)
            else:
                pub_str = pub_date.astimezone(KST).strftime("%Y-%m-%d")
                logger.info(f"    ⏭️ 오늘({today_str}) 아닌 콘텐츠 건너뜀 ({pub_str}): {content['title'][:40]}")
                skipped_ids.add(content["id"])
        else:
            # 날짜 확인 불가 → 보수적으로 전송 (놓치지 않도록)
            logger.info(f"    ❓ 발행일 확인 불가, 전송: {content['title'][:40]}")
            today_contents.append(content)

        time.sleep(1)

    if not today_contents:
        logger.info(f"[오픈애즈] 오늘({today_str}) 발행된 신규 콘텐츠 없음")
        return True, skipped_ids  # 날짜 지난 항목은 seen에 추가

    openads_url = "https://www.openads.co.kr/latest"
    slack_ok = send_slack_notification(today_contents, "오픈애즈", openads_url)
    send_kakao_notification(today_contents, "오픈애즈", openads_url)
    new_ids = ({c["id"] for c in today_contents} | skipped_ids) if slack_ok else skipped_ids
    return slack_ok, new_ids


def process_careet(seen_ids: set) -> tuple[bool, set]:
    """캐릿 콘텐츠를 처리합니다."""
    all_contents = fetch_careet_contents()
    if not all_contents:
        logger.warning("[캐릿] 콘텐츠를 가져오지 못했습니다.")
        return True, set()

    new_contents = [c for c in all_contents if c["id"] not in seen_ids]
    if not new_contents:
        logger.info("[캐릿] 신규 콘텐츠 없음")
        return True, set()

    logger.info(f"[캐릿] 🆕 신규 콘텐츠 {len(new_contents)}건 발견! 날짜 확인 중...")

    today_str = datetime.now(KST).strftime("%Y-%m-%d")
    today_contents = []
    skipped_ids = set()  # 날짜 지난 항목도 seen에 추가해 재처리 방지

    for i, content in enumerate(new_contents):
        logger.info(f"  [{i+1}/{len(new_contents)}] 상세 정보 수집: {content['title'][:40]}...")
        detail = fetch_careet_detail(content["_contentId"])

        # 썸네일이 목록에서 없으면 상세 페이지에서 가져오기
        if not content.get("thumbnail"):
            content["thumbnail"] = detail.get("thumbnail", "")
        content["tags"] = detail.get("tags", [])
        content["summary"] = generate_summary(
            content["title"],
            detail.get("description", ""),
            detail.get("body_text", ""),
        )

        pub_date = detail.get("pub_date")
        if pub_date is not None:
            if is_today_kst(pub_date):
                pub_str = pub_date.astimezone(KST).strftime("%Y-%m-%d")
                logger.info(f"    ✅ 오늘 발행 ({pub_str}): {content['title'][:40]}")
                today_contents.append(content)
            else:
                pub_str = pub_date.astimezone(KST).strftime("%Y-%m-%d")
                logger.info(f"    ⏭️ 오늘({today_str}) 아닌 콘텐츠 건너뜀 ({pub_str}): {content['title'][:40]}")
                skipped_ids.add(content["id"])
        else:
            # 날짜 확인 불가 → 보수적으로 전송 (놓치지 않도록)
            logger.info(f"    ❓ 발행일 확인 불가, 전송: {content['title'][:40]}")
            today_contents.append(content)

        time.sleep(1)

    if not today_contents:
        logger.info(f"[캐릿] 오늘({today_str}) 발행된 신규 콘텐츠 없음")
        return True, skipped_ids  # 날짜 지난 항목은 seen에 추가

    careet_url = "https://www.careet.net/Content/All"
    slack_ok = send_slack_notification(today_contents, "캐릿", careet_url)
    send_kakao_notification(today_contents, "캐릿", careet_url)
    new_ids = ({c["id"] for c in today_contents} | skipped_ids) if slack_ok else skipped_ids
    return slack_ok, new_ids


def main():
    logger.info("=" * 50)
    logger.info("마케팅 커뮤니티 → Slack 알림봇 실행")
    logger.info("  - 오픈애즈 (openads.co.kr)")
    logger.info("  - 캐릿 (careet.net)")
    logger.info("=" * 50)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    seen_ids = load_seen_ids()
    all_new_ids = set()

    # 오픈애즈 처리
    success1, new_ids1 = process_openads(seen_ids)
    all_new_ids |= new_ids1

    # 캐릿 처리
    success2, new_ids2 = process_careet(seen_ids)
    all_new_ids |= new_ids2

    # seen_ids 업데이트
    if all_new_ids:
        save_seen_ids(seen_ids | all_new_ids)

    logger.info("실행 완료!")


if __name__ == "__main__":
    main()
