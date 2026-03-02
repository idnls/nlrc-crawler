import asyncio
import json
import os
import sys
import re
import html
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import requests
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ==========================================
# 1. 텔레그램 봇 설정 (환경 변수)
# ==========================================
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')

# CHAT_ID를 쉼표로 구분하여 여러 개 지원
# 예: "6517178136,-1001234567890"
CHAT_IDS = [cid.strip() for cid in (os.environ.get('CHAT_ID') or '').split(',') if cid.strip()]

# 마지막 사건번호 저장 파일 경로
LAST_CASE_FILE = "last_case.json"

# 사건 종류 리스트
CASE_CATEGORIES = ['부해', '부노', '차별', '교섭', '단위', '공정', '단협', '손해', '의결', '휴업', '재해', '상병', '노협']

# 얼마나 과거의 소식까지 허용할지 (최근 60일 이내 판정된 건만 신규로 간주)
MAX_DAYS_OLD = 60

def clean_text(text):
    """HTML 태그 제거 및 텍스트 정제 (줄바꿈 보존)"""
    if not text: return ""
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()

def send_telegram_message(text):
    """텔레그램으로 메시지 전송 (여러 채팅방 지원, 4096자 초과 시 분할 전송)"""
    if not TELEGRAM_TOKEN:
        print("⚠️ TELEGRAM_TOKEN이 설정되지 않았습니다.")
        return
    if not CHAT_IDS:
        print("⚠️ CHAT_ID가 설정되지 않았습니다.")
        return

    MAX_LENGTH = 4000
    parts = []
    remaining = text
    while len(remaining) > MAX_LENGTH:
        split_index = remaining.rfind('\n', 0, MAX_LENGTH)
        if split_index == -1:
            split_index = MAX_LENGTH
        parts.append(remaining[:split_index].strip())
        remaining = remaining[split_index:].strip()
    parts.append(remaining)

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    for chat_id in CHAT_IDS:
        print(f"📡 [{chat_id}] 전송 시작...")
        for i, part in enumerate(parts):
            message_to_send = part
            if len(parts) > 1:
                message_to_send = f"[{i+1}/{len(parts)}]\n" + part
            payload = {'chat_id': chat_id, 'text': message_to_send}
            try:
                response = requests.post(url, data=payload)
                response.raise_for_status()
                print(f"  ✅ [{chat_id}] 파트 {i+1}/{len(parts)} 전송 성공!")
            except Exception as e:
                print(f"  ❌ [{chat_id}] 파트 {i+1} 전송 실패: {e}")
                if hasattr(e, 'response') and e.response is not None:
                    print(f"     응답 내용: {e.response.text}")

def extract_matter_and_summary(detail_soup):
    """판정사항/결정사항, 판정요지/결정요지 추출 (두 가지 명칭 모두 지원)"""
    matter_text = '상세 내용 없음'
    summary_text = '상세 내용 없음'
    matter_label = '판정사항'
    summary_label = '판정요지'

    # ① 판정사항 또는 결정사항 찾기
    matter_th = None
    for keyword in ['판정사항', '결정사항']:
        matter_th = (
            detail_soup.find('th', string=re.compile(rf'^{keyword}$')) or
            detail_soup.find('th', string=keyword)
        )
        if matter_th:
            matter_label = keyword
            break

    if matter_th:
        matter_td = matter_th.find_next('td')
        if matter_td:
            matter_text = clean_text(matter_td.get_text(separator="\n"))

    # ② 판정요지 또는 결정요지 찾기
    summary_th = None
    for keyword in ['판정요지', '결정요지']:
        summary_th = detail_soup.find('th', string=keyword)
        if not summary_th:
            for th in detail_soup.find_all('th', string=re.compile(keyword)):
                if th.get_text(strip=True) == keyword:
                    summary_th = th
                    break
        if summary_th:
            summary_label = keyword
            break

    if summary_th:
        summary_td = summary_th.find_next('td')
        if summary_td:
            summary_text = clean_text(summary_td.get_text(separator="\n"))

    return matter_text, summary_text, matter_label, summary_label

async def try_get_initial_case(page, current_case_number):
    """초심보기 버튼 클릭으로 초심사건번호 추출 시도"""
    try:
        # 초심보기 버튼 찾기
        chosim_btn = page.locator('button:has-text("초심보기"), a:has-text("초심보기")')
        btn_count = await chosim_btn.count()

        if btn_count == 0:
            print("   ℹ️ 초심보기 버튼 없음 (초심사건 아님)")
            return None

        print("   🔍 초심보기 버튼 발견, 클릭 시도...")

        # 초심보기 버튼 클릭 후 detail.do 응답 캡처
        async with page.expect_response(
            lambda res: "/detail.do" in res.url and res.status == 200,
            timeout=15000
        ) as init_resp_info:
            await chosim_btn.first.click(force=True)
            init_resp = await init_resp_info.value
            init_content = await init_resp.text()

        init_soup = BeautifulSoup(init_content, 'html.parser')

        # 초심 페이지에서 사건번호 추출
        # 방법 1: "사건 : XXXX" 패턴 찾기
        case_text_nodes = init_soup.find_all(string=re.compile(r'사건\s*:'))
        for node in case_text_nodes:
            match = re.search(r'사건\s*:\s*([0-9]{4}[가-힣]{1,4}[0-9]+)', node)
            if match:
                initial_case = match.group(1)
                if initial_case != current_case_number:
                    print(f"   🔗 초심사건번호 확인: {initial_case}")
                    return initial_case

        # 방법 2: 페이지 전체에서 사건번호 패턴 검색
        all_case_matches = re.findall(r'([0-9]{4}[가-힣]{1,4}[0-9]+)', init_content)
        for match in all_case_matches:
            if match != current_case_number:
                print(f"   🔗 초심사건번호 추정: {match}")
                return match

    except Exception as e:
        print(f"   ⚠️ 초심보기 버튼 클릭 실패: {e}")

    return None

async def get_recent_judgments(search_keyword='부해', count=1):
    """검색 페이지에서 키워드로 검색하여 최근 N개의 상세 데이터를 추출"""
    async with async_playwright() as p:
        chrome_args = ['--no-first-run', '--no-default-browser-check', '--disable-features=TranslateUI']
        try:
            browser = await p.chromium.launch(headless=True, channel="chrome", args=chrome_args)
        except Exception as e:
            print(f"⚠️ 시스템 Chrome 실행 실패, 기본 Chromium으로 대체 시도: {e}")
            browser = await p.chromium.launch(headless=True, args=chrome_args)

        page = await browser.new_page()

        try:
            url = "https://nlrc.go.kr/nlrc/mainCase/judgment/search/index.do"
            print(f"🌐 검색 페이지 접속 중: {url} (키워드: {search_keyword}, 목표 개수: {count})")
            await page.goto(url, wait_until="networkidle", timeout=60000)

            await page.fill('#pQuery', search_keyword)

            print("🔘 검색 실행 (최대 30건 요청)...")
            await page.focus('#pQuery')
            await page.keyboard.press('Enter')

            await page.evaluate('''() => {
                let form = document.querySelector('#searchForm') || document.forms[0];
                if (form) {
                    let input = document.createElement('input');
                    input.type = 'hidden'; input.name = 'pageUnit'; input.value = '30';
                    form.appendChild(input);
                }
            }''')

            try:
                async with page.expect_response(lambda res: "/list.do" in res.url and res.status == 200, timeout=30000) as response_info:
                    await page.click('.btnSearch')
                    list_resp = await response_info.value
                    list_content = await list_resp.text()
            except:
                print("⚠️ 검색 결과 응답 대기 제한 시간 초과. 현재 페이지 내용으로 진행합니다.")
                list_content = await page.content()

            soup = BeautifulSoup(list_content, 'html.parser')
            dl_list = soup.find_all('dl', class_='C_Cts')
            print(f"📋 검색 결과 {len(dl_list)}건 발견")

            judgments = []
            for dl in dl_list:
                item_data = {
                    'case_number': '미검출',
                    'title': '최신 판정 사례',
                    'committee': '중앙노동위원회',
                    'decision_result': '결과 미표기',
                    'decision_date': '날짜 미표기',
                    'decision_matter': '상세 내용 없음',
                    'decision_summary': '상세 내용 없음',
                    'matter_label': '판정사항',
                    'summary_label': '판정요지',
                    'initial_case': None
                }

                dt = dl.find('dt', class_='tit')
                if dt:
                    a_tag = dt.find('a')
                    if a_tag:
                        strong = a_tag.find('strong')
                        if strong: item_data['committee'] = clean_text(strong.get_text())
                        spans = a_tag.find_all('span')
                        if len(spans) >= 1: item_data['case_number'] = clean_text(spans[0].get_text())
                        if len(spans) >= 2: item_data['title'] = clean_text(spans[1].get_text())

                    em_dates = dt.find_all('em', class_='date')
                    if len(em_dates) >= 1: item_data['decision_date'] = clean_text(em_dates[0].get_text())
                    if len(em_dates) >= 2:
                        raw_result = em_dates[1].get_text()
                        item_data['decision_result'] = clean_text(raw_result.replace("|", ""))

                if item_data['case_number'] != '미검출':
                    judgments.append(item_data)

            def parse_date(date_str):
                try:
                    return datetime.strptime(date_str, '%Y.%m.%d')
                except:
                    return datetime.min

            judgments.sort(key=lambda x: parse_date(x['decision_date']), reverse=True)

            final_results = []
            for i, item in enumerate(judgments[:count]):
                print(f"🔎 [{i+1}/{count}] {item['case_number']} 상세 정보 추출 중...")
                try:
                    target_selector = f'a[data-k2="{item["case_number"]}"]'

                    # ★ 원본 방식 유지: 네트워크 응답을 직접 캡처
                    async with page.expect_response(lambda res: "/detail.do" in res.url and res.status == 200, timeout=15000) as response_info:
                        await page.click(target_selector, force=True)
                        detail_resp_obj = await response_info.value
                        detail_content = await detail_resp_obj.text()

                    detail_soup = BeautifulSoup(detail_content, 'html.parser')

                    # ★ 판정사항/결정사항, 판정요지/결정요지 모두 지원
                    matter, summary, matter_label, summary_label = extract_matter_and_summary(detail_soup)
                    item['decision_matter'] = matter
                    item['decision_summary'] = summary
                    item['matter_label'] = matter_label
                    item['summary_label'] = summary_label

                    # ★ 초심보기 버튼 클릭으로 초심사건번호 추출
                    item['initial_case'] = await try_get_initial_case(page, item['case_number'])

                    final_results.append(item)
                    print(f"   ✅ 데이터 확보 완료 (초심사건: {item['initial_case'] or '없음'})")

                    # 팝업 닫기
                    await page.evaluate("""
                        document.querySelectorAll('.layer-wrap, .layer-bg, .dimmed, .layer-close, .btnClose').forEach(el => el.remove());
                        document.body.classList.remove('layer-open');
                        document.documentElement.style.overflow = 'auto';
                    """)
                    await asyncio.sleep(1)

                except Exception as detail_e:
                    print(f"   ⚠️ 상세 정보 추출 실패 ({item['case_number']}): {detail_e}")
                    final_results.append(item)

            return final_results

        except Exception as e:
            print(f"❌ 크롤링 중 에러 발생: {e}")
            return []
        finally:
            await browser.close()

def load_sent_cases():
    """알림을 보낸 사건번호 리스트 로드"""
    if os.path.exists(LAST_CASE_FILE):
        with open(LAST_CASE_FILE, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                if isinstance(data, list): return set(data)
                if isinstance(data, dict): return {data.get('case_number')}
            except:
                return set()
    return set()

def save_sent_cases(sent_cases):
    """알림을 보낸 사건번호 리스트 저장 (최근 200건 유지)"""
    cases_list = list(sent_cases)[-200:]
    with open(LAST_CASE_FILE, "w", encoding="utf-8") as f:
        json.dump(cases_list, f, ensure_ascii=False, indent=4)

async def main():
    print("🤖 중앙노동위원회 알림 봇 가동 시작...")
    print(f"📬 메시지 수신 채팅방: {CHAT_IDS}")

    is_test = "--test" in sys.argv
    count = 1
    if is_test:
        count = 5
        for i, arg in enumerate(sys.argv):
            if arg == "--test" and i + 1 < len(sys.argv) and sys.argv[i+1].isdigit():
                count = int(sys.argv[i+1])

    is_github_actions = "GITHUB_ACTIONS" in os.environ

    while True:
        sent_cases = load_sent_cases()
        all_results = []

        print(f"\n🔍 전체 {len(CASE_CATEGORIES)}개 사건 종류 모니터링 중...")
        for category in CASE_CATEGORIES:
            print(f"👉 '{category}' 검색 중...")
            fetch_count = count if is_test else 30
            cat_results = await get_recent_judgments(search_keyword=category, count=fetch_count)
            all_results.extend(cat_results)
            await asyncio.sleep(1)

        def parse_date(date_str):
            try: return datetime.strptime(date_str, '%Y.%m.%d')
            except: return datetime.min

        all_results.sort(key=lambda x: parse_date(x['decision_date']), reverse=True)

        items_to_send = all_results[:count] if is_test else all_results

        new_items = []
        now = datetime.now()
        for latest in reversed(items_to_send):
            if latest['case_number'] == '미검출': continue
            days_diff = (now - parse_date(latest['decision_date'])).days
            if is_test or (latest['case_number'] not in sent_cases and days_diff <= MAX_DAYS_OLD):
                new_items.append(latest)
            elif not is_test and latest['case_number'] not in sent_cases:
                sent_cases.add(latest['case_number'])
                save_sent_cases(sent_cases)

        sent_count = len(new_items)

        if sent_count > 0 and not is_test:
            print(f"📊 신규 업데이트 {sent_count}건 발견. 요약 메시지 발송 중...")
            send_telegram_message(f"🔔 이번 주 노동위원회 판정·결정요지 신규 업데이트는 총 {sent_count}건입니다.")

        for latest in new_items:
            print(f"🎉 알림 발송 시도: {latest['case_number']} ({latest['committee']})")

            matter_label = latest.get('matter_label', '판정사항')
            summary_label = latest.get('summary_label', '판정요지')

            message = (
                f"🚨 [노동위원회 판정·결정요지 신규 업데이트]\n\n"
                f"🏢 위원회: {latest['committee']}\n"
                f"🔢 사건번호: {latest['case_number']}\n"
            )

            if latest.get('initial_case'):
                message += f"🔗 초심사건번호: {latest['initial_case']}\n"

            message += (
                f"📅 판정일: {latest['decision_date']}\n"
                f"⚖️ 판정결과: {latest['decision_result']}\n"
                f"📝 제목: {latest['title']}\n\n"
                f"✅ [{matter_label}]\n{latest['decision_matter']}\n\n"
                f"📖 [{summary_label}]\n{latest['decision_summary']}"
            )

            send_telegram_message(message)

            if not is_test:
                sent_cases.add(latest['case_number'])
                save_sent_cases(sent_cases)

        if sent_count == 0 and not is_test:
            print("ℹ️ 신규 업데이트 건이 없습니다.")
            send_telegram_message("✅ 이번 주 노동위원회 판정·결정요지 신규 업데이트가 없습니다.")

        if is_test or is_github_actions:
            if is_test: print("\n🧪 테스트 모드 종료.")
            if is_github_actions: print("\n✅ GitHub Actions 1회 실행 완료.")
            break

        print("⏰ 6시간 후 다시 확인합니다...")
        await asyncio.sleep(21600)

if __name__ == "__main__":
    asyncio.run(main())
