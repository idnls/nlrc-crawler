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

# ==========================================
# 1. 텔레그램 봇 설정 (환경 변수 우선 지원)
# ==========================================
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8659861176:AAFULSx6IumW1BWJWJhHspqBk9ss_1kmeso')
CHAT_ID = os.environ.get('CHAT_ID', '6517178136')

# 마지막 사건번호 저장 파일 경로
LAST_CASE_FILE = "last_case.json"

# 사건 종류 리스트
CASE_CATEGORIES = ['부해', '부노', '차별', '교섭', '단위', '공정', '단협', '손해', '의결', '휴업', '재해', '상병', '노협']

def clean_text(text):
    """HTML 태그 제거 및 텍스트 정제"""
    if not text: return ""
    # HTML 엔티티 변환 (&lt; -> < 등)
    text = html.unescape(text)
    # 모든 HTML 태그 제거 (<b> 등)
    clean = re.compile('<.*?>')
    cleaned = re.sub(clean, '', text)
    # 불필요한 공백 및 줄바꿈 정리
    return cleaned.strip()

def send_telegram_message(text):
    """텔레그램으로 메시지 전송"""
    if TELEGRAM_TOKEN == '여기에_봇_토큰_입력' or CHAT_ID == '여기에_채팅방_ID_입력':
        print("⚠️ 텔레그램 토큰 또는 채팅 ID가 설정되지 않았습니다. 메시지를 보내지 않습니다.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {'chat_id': CHAT_ID, 'text': text}
    print(f"📡 텔레그램 메시지 전송 시도 중... (Chat ID: {CHAT_ID})")
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status()
        print("✅ 텔레그램 메시지 전송 성공!")
    except Exception as e:
        print(f"❌ 텔레그램 전송 실패: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"   - 응답 내용: {e.response.text}")

async def get_recent_judgments(search_keyword='부해', count=1):
    """검색 페이지에서 키워드로 검색하여 최근 N개의 상세 데이터를 추출"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        try:
            url = "https://nlrc.go.kr/nlrc/mainCase/judgment/search/index.do"
            print(f"🌐 검색 페이지 접속 중: {url} (키워드: {search_keyword}, 목표 개수: {count})")
            await page.goto(url, wait_until="networkidle", timeout=60000)
            
            # 1. 검색어 입력 (사건 종류 리스트 중 하나를 사용하거나 빈 검색어로 전체 시도)
            # 사용자가 준 리스트 중 하나라도 검색어에 포함되면 좋음. 기본은 '부해'
            await page.fill('#pQuery', search_keyword)
            
            # 2. 검색 실행 및 목록 응답 대기
            print("🔘 검색 실행...")
            await page.focus('#pQuery')
            await page.keyboard.press('Enter')
            
            try:
                # expect_response는 클릭이나 엔터와 함께 비동기로 동작시키기 좋음
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
                    'decision_summary': '상세 내용 없음'
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

            # 4. 판정일 기준 정렬 (최신순)
            def parse_date(date_str):
                try:
                    # '2026.02.04' 형식 파싱
                    return datetime.strptime(date_str, '%Y.%m.%d')
                except:
                    return datetime.min

            judgments.sort(key=lambda x: parse_date(x['decision_date']), reverse=True)
            
            # 5. 상위 N건에 대해 상세 정보 추출
            final_results = []
            for i, item in enumerate(judgments[:count]):
                print(f"🔎 [{i+1}/{count}] {item['case_number']} 상세 정보 추출 중...")
                try:
                    # 클릭과 응답 대기를 동시에 수행하여 혼선 방지 (expect_response 사용)
                    target_selector = f'a[data-k2="{item["case_number"]}"]'
                    async with page.expect_response(lambda res: "/detail.do" in res.url and res.status == 200, timeout=15000) as response_info:
                        # force=True를 사용하여 interception을 무시하고 클릭
                        await page.click(target_selector, force=True)
                        detail_resp_obj = await response_info.value
                        detail_content = await detail_resp_obj.text()
                        
                        detail_soup = BeautifulSoup(detail_content, 'html.parser')
                        
                        # 판정사항
                        matter_th = detail_soup.find('th', string=re.compile(r'^판정사항$')) or detail_soup.find('th', string='판정사항')
                        if matter_th:
                            matter_td = matter_th.find_next('td')
                            if matter_td: item['decision_matter'] = clean_text(matter_td.get_text())
                        
                        # 판정요지 (정확하게 "판정요지"인 th만 타격)
                        summary_th = detail_soup.find('th', string='판정요지')
                        if summary_th:
                            summary_td = summary_th.find_next('td')
                            if summary_td: item['decision_summary'] = clean_text(summary_td.get_text())
                        else:
                            # 차선책
                            summary_ths = detail_soup.find_all('th', string=re.compile('판정요지'))
                            for th in summary_ths:
                                if th.get_text(strip=True) == "판정요지":
                                    summary_td = th.find_next('td')
                                    if summary_td: item['decision_summary'] = clean_text(summary_td.get_text())
                                    break
                    
                    final_results.append(item)
                    print(f"   ✅ 데이터 확보 완료")

                    # 팝업 닫기 (강제 제거 방식 - 모든 관련 클래스 제거)
                    await page.evaluate("""
                        document.querySelectorAll('.layer-wrap, .layer-bg, .dimmed, .layer-close, .btnClose').forEach(el => el.remove());
                        document.body.classList.remove('layer-open');
                        document.documentElement.style.overflow = 'auto';
                    """)
                    await asyncio.sleep(1)

                except Exception as detail_e:
                    print(f"   ⚠️ 상세 정보 추출 실패 ({item['case_number']}): {detail_e}")
                    # 실패 시 현재 페이지라도 분석 시도
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
                if isinstance(data, dict): return {data.get('case_number')} # 레거시 대응
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
        
        # 1. 모든 카테고리 순회 검색
        print(f"\n🔍 전체 {len(CASE_CATEGORIES)}개 사건 종류 모니터링 중...")
        for category in CASE_CATEGORIES:
            print(f"👉 '{category}' 검색 중...")
            # 테스트 모드나 깃허브 액션에서는 요청 개수만큼, 일반 모드에선 최신 2건 확인
            fetch_count = count if (is_test or is_github_actions) else 2
            cat_results = await get_recent_judgments(search_keyword=category, count=fetch_count)
            all_results.extend(cat_results)
            await asyncio.sleep(1) # 부하 방지
        
        # 2. 전체 결과 판정일 기준 정렬
        from datetime import datetime
        def parse_date(date_str):
            try: return datetime.strptime(date_str, '%Y.%m.%d')
            except: return datetime.min

        all_results.sort(key=lambda x: parse_date(x['decision_date']), reverse=True)
        
        # 3. 알림 발송 처리
        items_to_send = all_results[:count] if is_test else all_results
        
        # 오래된 순서대로 발송 (역순 처리)
        for latest in reversed(items_to_send):
            if latest['case_number'] == '미검출': continue
            
            # 테스트 모드이거나 신규 업데이트인 경우 발송
            if is_test or (latest['case_number'] not in sent_cases):
                print(f"🎉 알림 발송 시도: {latest['case_number']} ({latest['committee']})")
                
                message = (
                    f"🚨 [노동위원회 판정·결정요지 신규 업데이트]\n\n"
                    f"🏢 위원회: {latest['committee']}\n"
                    f"🔢 사건번호: {latest['case_number']}\n"
                    f"📅 판정일: {latest['decision_date']}\n"
                    f"⚖️ 판정결과: {latest['decision_result']}\n"
                    f"📝 제목: {latest['title']}\n\n"
                    f"✅ [판정사항]\n{latest['decision_matter']}\n\n"
                    f"📖 [판정요지]\n{latest['decision_summary'][:1000]}"
                )
                
                send_telegram_message(message)
                
                if not is_test:
                    sent_cases.add(latest['case_number'])
                    save_sent_cases(sent_cases)
            else:
                # 일반 모드에서는 이미 보낸 건이면 이후 과거 건들은 스킵 가능 (정렬되어 있으므로)
                if not is_test: continue

        if is_test or is_github_actions:
            if is_test: print("\n🧪 테스트 모드 종료.")
            if is_github_actions: print("\n✅ GitHub Actions 1회 실행 완료.")
            break
            
        print("⏰ 6시간 후 다시 확인합니다...")
        await asyncio.sleep(21600)

if __name__ == "__main__":
    asyncio.run(main())
