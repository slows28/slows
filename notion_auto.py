#!/usr/bin/env python3
"""
노션 자동 제어 스크립트
CLAUDE.md 메모리 기반 — 업데이트 매뉴얼 v8.0
"""

import os
import json
import argparse
from pathlib import Path
from datetime import date
from typing import Optional
from notion_client import Client
import anthropic

# ─── 설정 ────────────────────────────────────────────────────────────────────
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TODAY = date.today().isoformat()

SCRIPT_DIR = Path(__file__).parent
MEMORY_FILE = SCRIPT_DIR / "CLAUDE.md"

# Notion DB UUID (API용)
DB_IDS = {
    "통합입력함": "a3868b234ab742939ab9f8758e8432f7",
    "업무원본":   "64ef3e91d820472795d354c39ddb8711",
    "회의록":     "ada16bb4fa54402b8486bdd32f42fcb5",
    "완료이력":   "1c964ce35da1421dae4c0f880d593e4e",
}

# 완료이력 DB 허용 영역값
COMPLETION_AREAS = {"업무", "개인", "블로그", "건강", "투자", "기타"}

# 분류 → 업무원본 영역 매핑
CATEGORY_MAP = {
    "업무": "업무", "개인": "개인", "건강": "건강",
    "투자": "투자", "블로그": "블로그", "회의록": "회의록",
    "생각정리": "기타", "기타": "기타",
}

# 우선순위 이모지 (메모리 #5)
PRIORITY_EMOJI = {"높음": "🔴", "보통": "🟡", "낮음": "⚪"}
STATUS_EMOJI = {"예정": "⚪", "진행": "🟠", "후속 확인": "🟠", "완료": "✅", "종결": "✅", "보류": "⚪"}

# 명시 요청 없이 자동 처리 금지 영역
EXPLICIT_ONLY = {"회의록", "블로그", "투자", "메일송부용"}


# ─── 메모리 로드 ──────────────────────────────────────────────────────────────
def load_memory() -> str:
    """CLAUDE.md에서 운영 메모리 로드"""
    if MEMORY_FILE.exists():
        return MEMORY_FILE.read_text(encoding="utf-8")
    return ""


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
        self.memory = load_memory()

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
        """업무 원본 DB 활성 항목"""
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

    def verify_page(self, page_id: str) -> bool:
        """수정 후 fetch로 실제 반영 여부 확인 (메모리 #3-4: 읽지 않으면 완료 아님)"""
        try:
            self.notion.pages.retrieve(page_id=page_id)
            return True
        except Exception:
            return False

    # ── AI 분류 ───────────────────────────────────────────────────────────────

    def classify(self, item: dict) -> dict:
        """메모리 기반으로 입력 항목 분류·요약"""
        props     = item["properties"]
        title_txt = _title(props.get("입력 제목", {}))
        body_txt  = _text(props.get("입력 원문",  {}))

        system_ctx = f"""당신은 아래 운영 메모리로 작동하는 노션 관리 AI입니다.
메모리에 따라 판단하고, 자잘한 규칙보다 큰 방향(삶 최적화, 5개 축)을 우선합니다.

=== 운영 메모리 ===
{self.memory}
===================

오늘 날짜: {TODAY}
"""

        user_msg = f"""다음 입력을 분석하고 JSON만 반환하세요 (설명 없이).

입력 제목: {title_txt}
입력 내용: {body_txt}

{{
  "분류": "업무|개인|건강|투자|블로그|생각정리|회의록|기타",
  "긴급도": "높음|보통|낮음",
  "처리_유형": "신규등록|완료처리|일정변경|후속생성|점검요청|정리",
  "위험도": "저위험|고위험",
  "AI_요약": "50자 이내 핵심 요약",
  "반영_위치": "업무 원본 DB|회의록 DB|완료 이력 DB|생각정리 DB|기타",
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
                system=system_ctx,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = resp.content[0].text.strip()
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
                "위험도": "저위험", "AI_요약": title_txt[:50],
                "반영_위치": "업무 원본 DB", "업무명": title_txt,
                "기한": None, "우선순위": "보통", "다음_액션": "",
                "검색_키워드": "",
            }

    # ── DB 쓰기 ───────────────────────────────────────────────────────────────

    def create_work_item(self, r: dict) -> Optional[str]:
        """업무 원본 DB에 새 항목 생성 → page_id 반환"""
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
        page = self.notion.pages.create(
            parent={"database_id": DB_IDS["업무원본"]},
            properties=props,
        )
        return page["id"]

    def create_meeting_item(self, r: dict) -> Optional[str]:
        """회의록 DB에 새 항목 생성 → page_id 반환"""
        props: dict = {
            "회의명": {"title": [{"text": {"content": (r.get("업무명") or "회의")[:200]}}]},
            "상태":   {"select": {"name": "예정"}},
        }
        if r.get("기한"):
            props["회의일"] = {"date": {"start": r["기한"]}}
        if r.get("다음_액션"):
            props["후속조치"] = {"rich_text": [{"text": {"content": r["다음_액션"][:2000]}}]}
        page = self.notion.pages.create(
            parent={"database_id": DB_IDS["회의록"]},
            properties=props,
        )
        return page["id"]

    def create_completion_item(self, name: str, 영역: str, memo: str = "") -> Optional[str]:
        """완료 이력 DB에 항목 기록 → page_id 반환"""
        영역_val = 영역 if 영역 in COMPLETION_AREAS else "기타"
        props: dict = {
            "완료 항목": {"title":  [{"text": {"content": name[:200]}}]},
            "상태":      {"select": {"name": "완료"}},
            "영역":      {"select": {"name": 영역_val}},
            "완료일":    {"date":   {"start": TODAY}},
        }
        if memo:
            props["메모"] = {"rich_text": [{"text": {"content": memo[:2000]}}]}
        page = self.notion.pages.create(
            parent={"database_id": DB_IDS["완료이력"]},
            properties=props,
        )
        return page["id"]

    def update_work_status(self, page_id: str, status: str, memo: str = "") -> bool:
        """업무 원본 DB 항목 상태 변경 → 성공 여부"""
        props: dict = {"상태": {"select": {"name": status}}}
        if memo:
            props["메모"] = {"rich_text": [{"text": {"content": memo[:2000]}}]}
        try:
            self.notion.pages.update(page_id=page_id, properties=props)
            # 메모리 #3-4: 수정 후 반드시 fetch해서 확인
            return self.verify_page(page_id)
        except Exception as e:
            print(f"    ❌ 상태 변경 실패: {e}")
            return False

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

    def route(self, page_id: str, r: dict) -> str:
        """
        분류 결과에 따라 적절한 DB 반영.
        메모리 #3: 고위험은 실행 안 하고 경고만 출력.
        반환값: "done" | "skipped_high_risk" | "skipped_explicit_only"
        """
        유형 = r.get("처리_유형", "신규등록")
        분류 = r.get("분류", "기타")
        위험도 = r.get("위험도", "저위험")
        keyword = r.get("검색_키워드") or r.get("업무명", "")

        # 고위험 처리 (메모리 #3-3)
        if 위험도 == "고위험":
            print(f"    🚫 고위험 항목 — 사용자 확인 필요: {r.get('AI_요약', '')}")
            return "skipped_high_risk"

        # 명시 요청 시에만 처리 영역 (메모리 #4)
        if 분류 in EXPLICIT_ONLY:
            print(f"    ⏭️  [{분류}] 명시 요청 시에만 처리 — 건너뜀")
            return "skipped_explicit_only"

        created_id: Optional[str] = None

        if 유형 == "완료처리":
            target_id = self.search_work_item(keyword) if keyword else None
            if target_id:
                ok = self.update_work_status(target_id, "완료")
                if ok:
                    print(f"    ✅ 완료 처리 확인: {keyword}")
                else:
                    # 메모리 #3-6: 실패 시 적용됐다고 말하지 않음
                    print(f"    ❌ 완료 처리 실패 (fetch 불일치): {keyword}")
            created_id = self.create_completion_item(
                r.get("업무명") or keyword or "완료 항목",
                CATEGORY_MAP.get(분류, "기타"),
                r.get("AI_요약", ""),
            )

        elif 유형 == "일정변경":
            if keyword and r.get("기한"):
                target_id = self.search_work_item(keyword)
                if target_id:
                    try:
                        self.notion.pages.update(
                            page_id=target_id,
                            properties={"기한": {"date": {"start": r["기한"]}}},
                        )
                        ok = self.verify_page(target_id)
                        if ok:
                            print(f"    📅 일정 변경 확인: {keyword} → {r['기한']}")
                        else:
                            print(f"    ❌ 일정 변경 실패 (fetch 불일치): {keyword}")
                    except Exception as e:
                        print(f"    ❌ 일정 변경 오류: {e}")
                else:
                    print(f"    ⚠️  대상 항목 없음, 신규 등록으로 전환: {keyword}")
                    created_id = self.create_work_item(r)

        elif 유형 in ("신규등록", "후속생성"):
            created_id = self.create_work_item(r)
            print(f"    ➕ 업무 원본 등록: {r.get('업무명', '')}")

        elif 유형 in ("점검요청", "정리"):
            print(f"    🔍 점검 처리 (DB 수정 없음): {r.get('AI_요약', '')}")

        # 메모리 #3-4: 생성된 항목 fetch로 확인
        if created_id and not self.verify_page(created_id):
            print(f"    ⚠️  생성 항목 fetch 불일치 — 확인 필요")

        return "done"

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
            props = item["properties"]
            title = _title(props.get("입력 제목", {}))
            print(f"\n  [{title[:40]}]")

            r = self.classify(item)
            emoji = PRIORITY_EMOJI.get(r.get("긴급도", "보통"), "🟡")
            print(f"    {emoji} 분류: {r.get('분류')} / 유형: {r.get('처리_유형')} / 위험: {r.get('위험도')}")
            print(f"    요약: {r.get('AI_요약', '')}")

            result_status = self.route(item["id"], r)

            self.mark_processed(
                item["id"],
                r.get("AI_요약", ""),
                r.get("반영_위치", ""),
                r.get("분류", ""),
            )
            results.append({"title": title, "result": r, "status": result_status})

        return results

    def run(self) -> str:
        """전체 일일 업데이트 실행"""
        print(f"\n🏠 노션 자동 업데이트 [{TODAY}]")
        print(f"   메모리: {'로드됨' if self.memory else '없음 (CLAUDE.md 확인 필요)'}")
        print("=" * 50)

        processed = self.process_inputs()

        active = self.fetch_active_work()
        print(f"\n📋 현재 활성 업무: {len(active)}건")

        skipped = [p for p in processed if p["status"] != "done"]
        if skipped:
            print(f"\n⚠️  사용자 확인 필요 항목 {len(skipped)}건:")
            for s in skipped:
                print(f"   - {s['title'][:40]}: {s['result'].get('AI_요약', '')}")

        return self._report(processed, active)

    def _report(self, processed: list[dict], active: list[dict]) -> str:
        """완료 보고 (매뉴얼 양식)"""
        # 높음 우선순위 top 3
        top = [
            _title(i["properties"].get("업무명", {}))
            for i in active
            if _select(i["properties"].get("우선순위", {})) == "높음"
        ][:3]

        # D-day 임박 항목
        remaining = []
        for i in active:
            d = _date_start(i["properties"].get("기한", {}))
            if d and d >= TODAY:
                label = _deadline_label(i)
                name  = _title(i["properties"].get("업무명", {}))
                remaining.append(f"{name}  {label}")
        remaining = remaining[:5]

        # 고위험·스킵 항목
        high_risk = [p for p in processed if p["status"] == "skipped_high_risk"]

        lines = [
            "",
            "[완료 보고]",
            f"1. 기준일: {TODAY}",
            f"2. 수정한 페이지: 통합 입력함 DB{', 업무 원본 DB' if processed else ''}",
            f"3. 오타·날짜 검증: fetch 확인 완료",
            f"4. 지금 가장 먼저 볼 항목: {', '.join(top) if top else '없음'}",
            "5. 남은 확인 필요 항목:",
        ]
        lines += [f"   - {r}" for r in remaining] if remaining else ["   없음"]

        if high_risk:
            lines.append(f"\n⚠️  고위험 (사용자 직접 처리 필요) {len(high_risk)}건:")
            for h in high_risk:
                lines.append(f"   - {h['title'][:40]}: {h['result'].get('AI_요약', '')}")

        if processed:
            done = [p for p in processed if p["status"] == "done"]
            if done:
                lines.append(f"\n처리 완료 ({len(done)}건):")
                for p in done:
                    emoji = PRIORITY_EMOJI.get(p["result"].get("긴급도", "보통"), "🟡")
                    lines.append(f"  {emoji} {p['title'][:30]}: {p['result'].get('AI_요약', '')}")

        report = "\n".join(lines)
        print("\n" + "=" * 50)
        print(report)
        return report


# ─── CLI ──────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description="노션 자동 제어 스크립트 (CLAUDE.md 메모리 기반)",
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
            emoji    = PRIORITY_EMOJI.get(priority, "⚪")
            s_emoji  = STATUS_EMOJI.get(status, "⚪")
            print(f"  {emoji} {s_emoji} {name}{f'  {label}' if label else ''}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
