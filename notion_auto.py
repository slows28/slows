#!/usr/bin/env python3
"""
노션 자동 제어 스크립트
업데이트 매뉴얼 v8.0 기반 자동화
"""

import os
import json
import argparse
from datetime import date
from typing import Optional
from notion_client import Client
import anthropic

# ─── 설정 ────────────────────────────────────────────────────────────────────
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TODAY = date.today().isoformat()  # "2026-05-28"

DB_IDS = {
    "통합입력함": "a3868b234ab742939ab9f8758e8432f7",
    "업무원본":   "64ef3e91d820472795d354c39ddb8711",
    "회의록":     "ada16bb4fa54402b8486bdd32f42fcb5",
    "완료이력":   "1c964ce35da1421dae4c0f880d593e4e",
}

# 통합입력함 분류 → 업무원본 영역 매핑
CATEGORY_MAP = {
    "업무": "업무", "개인": "개인", "건강": "건강",
    "투자": "투자", "블로그": "블로그", "회의록": "회의록",
    "생각정리": "기타", "기타": "기타",
}

# 완료이력 DB 허용 영역값
COMPLETION_AREAS = {"업무", "개인", "블로그", "건강", "투자", "기타"}


# ─── 프로퍼티 헬퍼 ────────────────────────────────────────────────────────────
def _title(prop: dict) -> str:
    try:
        return prop["title"][0]["text"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""


def _text(prop: dict) -> str:
    try:
        return "".join(t["text"]["content"] for t in prop.get("rich_text", []))
    except (KeyError, TypeError):
        return ""


def _select(prop: dict) -> str:
    try:
        return (prop.get("select") or {}).get("name", "")
    except (KeyError, TypeError, AttributeError):
        return ""


def _date_start(prop: dict) -> str:
    try:
        return (prop.get("date") or {}).get("start", "")
    except (KeyError, TypeError, AttributeError):
        return ""


def _deadline_label(item: dict) -> str:
    d = _date_start(item["properties"].get("기한", {}))
    if not d:
        return ""
    try:
        delta = (date.fromisoformat(d[:10]) - date.today()).days
        if delta == 0:
            return "오늘"
        elif delta < 0:
            return f"D+{-delta} 지남"
        else:
            return f"D-{delta}"
    except ValueError:
        return d


# ─── 메인 클래스 ──────────────────────────────────────────────────────────────
class NotionAuto:
    def __init__(self):
        self.notion = Client(auth=NOTION_TOKEN)
        self.ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    # ── 조회 ──────────────────────────────────────────────────────────────────

    def fetch_unprocessed(self) -> list[dict]:
        """통합 입력함에서 미분류·확인 필요 항목 조회"""
        res = self.notion.databases.query(
            database_id=DB_IDS["통합입력함"],
            filter={
                "or": [
                    {"property": "처리 상태", "select": {"equals": "미분류"}},
                    {"property": "처리 상태", "select": {"equals": "확인 필요"}},
                ]
            },
            sorts=[{"property": "입력일", "direction": "ascending"}],
        )
        return res["results"]

    def fetch_active_work(self) -> list[dict]:
        """업무 원본 DB 활성 항목 (예정·진행·후속확인)"""
        res = self.notion.databases.query(
            database_id=DB_IDS["업무원본"],
            filter={
                "or": [
                    {"property": "상태", "select": {"equals": "예정"}},
                    {"property": "상태", "select": {"equals": "진행"}},
                    {"property": "상태", "select": {"equals": "후속 확인"}},
                ]
            },
            sorts=[
                {"property": "우선순위", "direction": "ascending"},
                {"property": "기한",     "direction": "ascending"},
            ],
        )
        return res["results"]

    def search_work_item(self, keyword: str) -> Optional[str]:
        """키워드로 업무 원본 DB 검색 → page_id"""
        if not keyword:
            return None
        res = self.notion.databases.query(
            database_id=DB_IDS["업무원본"],
            filter={"property": "업무명", "rich_text": {"contains": keyword[:20]}},
        )
        return res["results"][0]["id"] if res["results"] else None

    # ── AI 분류 ───────────────────────────────────────────────────────────────

    def classify(self, item: dict) -> dict:
        """Claude로 입력 항목 분류 및 요약"""
        props = item["properties"]
        title_txt = _title(props.get("입력 제목", {}))
        body_txt  = _text(props.get("입력 원문",  {}))

        prompt = f"""당신은 노션 업무 관리 시스템의 AI 어시스턴트입니다.
아래 입력을 분석하고 JSON만 반환하세요 (설명 없이).

입력 제목: {title_txt}
입력 내용: {body_txt}

{{
  "분류": "업무|개인|건강|투자|블로그|생각정리|회의록|기타",
  "긴급도": "높음|보통|낮음",
  "처리_유형": "신규등록|완료처리|일정변경|후속생성|점검요청|정리",
  "AI_요약": "50자 이내 핵심 요약",
  "반영_위치": "업무 원본 DB|회의록 DB|완료 이력 DB|기타",
  "업무명": "등록할 업무명 (신규등록·후속생성 시)",
  "기한": "YYYY-MM-DD (없으면 null)",
  "우선순위": "높음|보통|낮음",
  "다음_액션": "구체적 다음 행동 (없으면 빈 문자열)",
  "검색_키워드": "완료·변경 시 기존 항목 검색어 (없으면 빈 문자열)"
}}"""

        try:
            resp = self.ai.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
            # ```json ... ``` 블록 제거
            if text.startswith("```"):
                parts = text.split("```")
                text = parts[1] if len(parts) > 1 else parts[0]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text.strip())
        except Exception as e:
            print(f"    ⚠️  AI 분류 실패: {e}")
            return {
                "분류": "기타", "긴급도": "보통", "처리_유형": "신규등록",
                "AI_요약": title_txt[:50], "반영_위치": "업무 원본 DB",
                "업무명": title_txt, "기한": None, "우선순위": "보통",
                "다음_액션": "", "검색_키워드": "",
            }

    # ── DB 쓰기 ───────────────────────────────────────────────────────────────

    def create_work_item(self, r: dict):
        """업무 원본 DB에 새 항목 생성"""
        영역 = CATEGORY_MAP.get(r.get("분류", "기타"), "기타")
        props: dict = {
            "업무명":   {"title":  [{"text": {"content": (r.get("업무명") or "미정")[:200]}}]},
            "상태":     {"select": {"name": "예정"}},
            "영역":     {"select": {"name": 영역}},
            "우선순위": {"select": {"name": r.get("우선순위", "보통")}},
        }
        if r.get("다음_액션"):
            props["다음 액션"] = {"rich_text": [{"text": {"content": r["다음_액션"][:2000]}}]}
        if r.get("기한"):
            props["기한"] = {"date": {"start": r["기한"]}}
        self.notion.pages.create(
            parent={"database_id": DB_IDS["업무원본"]},
            properties=props,
        )

    def create_meeting_item(self, r: dict):
        """회의록 DB에 새 항목 생성"""
        props: dict = {
            "회의명": {"title": [{"text": {"content": (r.get("업무명") or "회의")[:200]}}]},
            "상태":   {"select": {"name": "예정"}},
        }
        if r.get("기한"):
            props["회의일"] = {"date": {"start": r["기한"]}}
        if r.get("다음_액션"):
            props["후속조치"] = {"rich_text": [{"text": {"content": r["다음_액션"][:2000]}}]}
        self.notion.pages.create(
            parent={"database_id": DB_IDS["회의록"]},
            properties=props,
        )

    def create_completion_item(self, name: str, 영역: str, memo: str = ""):
        """완료 이력 DB에 항목 기록"""
        영역_val = 영역 if 영역 in COMPLETION_AREAS else "기타"
        props: dict = {
            "완료 항목": {"title":  [{"text": {"content": name[:200]}}]},
            "상태":      {"select": {"name": "완료"}},
            "영역":      {"select": {"name": 영역_val}},
            "완료일":    {"date":   {"start": TODAY}},
        }
        if memo:
            props["메모"] = {"rich_text": [{"text": {"content": memo[:2000]}}]}
        self.notion.pages.create(
            parent={"database_id": DB_IDS["완료이력"]},
            properties=props,
        )

    def update_work_status(self, page_id: str, status: str, memo: str = ""):
        """업무 원본 DB 항목 상태 변경"""
        props: dict = {"상태": {"select": {"name": status}}}
        if memo:
            props["메모"] = {"rich_text": [{"text": {"content": memo[:2000]}}]}
        self.notion.pages.update(page_id=page_id, properties=props)

    def mark_processed(self, page_id: str, summary: str, location: str, 분류: str):
        """통합 입력함 항목 → 정리 완료 표시"""
        props: dict = {
            "처리 상태": {"select": {"name": "정리 완료"}},
            "AI 요약":   {"rich_text": [{"text": {"content": summary[:200]}}]},
            "반영 위치": {"rich_text": [{"text": {"content": location[:200]}}]},
        }
        if 분류 in CATEGORY_MAP:
            props["분류"] = {"select": {"name": 분류}}
        self.notion.pages.update(page_id=page_id, properties=props)

    # ── 라우팅 ────────────────────────────────────────────────────────────────

    def route(self, page_id: str, r: dict):
        """분류 결과에 따라 적절한 DB 반영"""
        유형 = r.get("처리_유형", "신규등록")
        분류 = r.get("분류", "기타")
        keyword = r.get("검색_키워드") or r.get("업무명", "")

        if 유형 == "완료처리":
            target_id = self.search_work_item(keyword) if keyword else None
            if target_id:
                self.update_work_status(target_id, "완료")
                print(f"    ✅ 완료 처리: {keyword}")
            self.create_completion_item(
                r.get("업무명") or keyword or "완료 항목",
                CATEGORY_MAP.get(분류, "기타"),
                r.get("AI_요약", ""),
            )

        elif 유형 == "일정변경":
            if keyword and r.get("기한"):
                target_id = self.search_work_item(keyword)
                if target_id:
                    self.notion.pages.update(
                        page_id=target_id,
                        properties={"기한": {"date": {"start": r["기한"]}}},
                    )
                    print(f"    📅 일정 변경: {keyword} → {r['기한']}")
                else:
                    print(f"    ⚠️  대상 항목 없음, 신규 등록으로 전환: {keyword}")
                    self.create_work_item(r)

        elif 유형 in ("신규등록", "후속생성"):
            if 분류 == "회의록":
                self.create_meeting_item(r)
                print(f"    📋 회의록 DB 등록: {r.get('업무명', '')}")
            else:
                self.create_work_item(r)
                print(f"    ➕ 업무 원본 등록: {r.get('업무명', '')}")

        elif 유형 in ("점검요청", "정리"):
            print(f"    🔍 점검 처리 (DB 수정 없음): {r.get('AI_요약', '')}")

    # ── 메인 워크플로우 ────────────────────────────────────────────────────────

    def process_inputs(self) -> list[dict]:
        """통합 입력함 일괄 처리"""
        items = self.fetch_unprocessed()
        if not items:
            print("  처리할 새 입력이 없습니다.")
            return []

        print(f"\n📥 미처리 입력 {len(items)}건 처리 중...")
        results = []

        for item in items:
            props  = item["properties"]
            title  = _title(props.get("입력 제목", {}))
            print(f"\n  [{title[:40]}]")

            r = self.classify(item)
            print(f"    분류: {r.get('분류')} / 유형: {r.get('처리_유형')} / 긴급: {r.get('긴급도')}")
            print(f"    요약: {r.get('AI_요약', '')}")

            self.route(item["id"], r)
            self.mark_processed(
                item["id"],
                r.get("AI_요약", ""),
                r.get("반영_위치", ""),
                r.get("분류", ""),
            )
            results.append({"title": title, "result": r})

        return results

    def run(self) -> str:
        """전체 일일 업데이트 실행"""
        print(f"\n🏠 노션 자동 업데이트 [{TODAY}]")
        print("=" * 50)

        processed = self.process_inputs()

        active = self.fetch_active_work()
        print(f"\n📋 현재 활성 업무: {len(active)}건")

        return self._report(processed, active)

    def _report(self, processed: list[dict], active: list[dict]) -> str:
        """완료 보고 (매뉴얼 양식)"""
        top = [
            _title(i["properties"].get("업무명", {}))
            for i in active
            if _select(i["properties"].get("우선순위", {})) == "높음"
        ][:3]

        remaining = [
            f"{_title(i['properties'].get('업무명', {}))}  {_deadline_label(i)}"
            for i in active
            if _date_start(i["properties"].get("기한", {})) >= TODAY
        ][:5]

        lines = [
            "",
            "[완료 보고]",
            f"1. 기준일: {TODAY}",
            f"2. 수정한 페이지: 통합 입력함 DB{', 업무 원본 DB' if processed else ''}",
            f"3. 처리 건수: {len(processed)}건 / 활성 업무: {len(active)}건",
            f"4. 지금 가장 먼저 볼 항목: {', '.join(top) if top else '없음'}",
            "5. 남은 확인 필요 항목:",
        ]
        lines += [f"   - {r}" for r in remaining] if remaining else ["   없음"]

        if processed:
            lines.append("\n처리 내역:")
            for p in processed:
                lines.append(f"  - {p['title'][:30]}: {p['result'].get('AI_요약', '')}")

        report = "\n".join(lines)
        print("\n" + "=" * 50)
        print(report)
        return report


# ─── CLI ──────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description="노션 자동 제어 스크립트",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
명령:
  update   미처리 입력함 처리 + 완료 보고  (기본값)
  process  입력함 처리만 실행
  status   현재 활성 업무 목록 출력
""",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="update",
        choices=["update", "process", "status"],
    )
    args = parser.parse_args()

    if not NOTION_TOKEN:
        print("❌ 환경변수 NOTION_TOKEN 이 설정되지 않았습니다.")
        return 1
    if not ANTHROPIC_KEY:
        print("❌ 환경변수 ANTHROPIC_API_KEY 가 설정되지 않았습니다.")
        return 1

    bot = NotionAuto()

    if args.command in ("update", None):
        bot.run()
    elif args.command == "process":
        bot.process_inputs()
    elif args.command == "status":
        active = bot.fetch_active_work()
        print(f"\n현재 활성 업무 {len(active)}건:\n")
        for item in active:
            p        = item["properties"]
            name     = _title(p.get("업무명", {}))
            status   = _select(p.get("상태", {}))
            priority = _select(p.get("우선순위", {}))
            label    = _deadline_label(item)
            star     = "🔴" if priority == "높음" else "🟡" if priority == "보통" else "⚪"
            print(f"  {star} [{status}] {name}{f'  {label}' if label else ''}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
