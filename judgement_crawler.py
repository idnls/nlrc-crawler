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

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_IDS = [cid.strip() for cid in (os.environ.get('CHAT_ID') or '').split(',') if cid.strip()]
LAST_CASE_FILE = "last_case.json"
CASE_CATEGORIES = ['부해', '부노', '차별', '교섭', '단위', '공정', '단협', '손해', '의결', '휴업', '재해', '상병', '노협']
MAX_DAYS_OLD = 60

def clean_text(text):
    if not text: return ""
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()

def get_committee_abbr(committee_name):
    """위원회 이름 앞 2자 추출 (충남지방노동위원회 → 충남)"""
    if not committee_name:
        return ''
    return committee_name[:2]

def send_telegram_message(text, reply_to_message_ids=None):
    """
    텔레그램 메시지 전송.
    reply_to_message_ids: {chat_id: message_id} → 해당 메시지의 댓글(답글)로 전송.
    반환값: {chat_id: first_message_id}
    """
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("⚠️ TELEGRAM_TOKEN 또는 CHAT_ID 미설정")
        return {}

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
    sent_message_ids = {}

    for chat_id in CHAT_IDS:
        print(f"📡 [{chat_id}] 전송 시작...")
        for i, part in enumerate(parts):
            message_to_send = f"[{i+1}/{len(parts)}]\n" + part if len(parts) > 1 else part
            payload = {'chat_id': chat_id, 'text': message_to_send}
            if i == 0 and reply_to_message_ids and chat_id in reply_to_message_ids:
                payload['reply_to_message_id'] = reply_to_message_ids[chat_id]
            try:
                response = requests.post(url, data=payload)
                response.raise_for_status()
                res_data = response.json()
                if res_data.get('ok') and i == 0:
                    sent_message_ids[chat_id] = res_data['result']['message_id']
                print(f"  ✅ [{chat_id}] 파트 {i+1}/{len(parts)} 전송 성공!")
            except Exception as e:
                print(f"  ❌ [{chat_id}] 파트 {i+1} 전송 실패: {e}")
    return sent_message_ids

def extract_matter_and_summary(detail_soup):
    """판정사항/결정사항, 판정요지/결정요지 추출"""
    matter_text = '상세 내용 없음'
    summary_text = '상세 내용 없음'
    matter_label = '판정사항'
    summary_label = '판정요지'

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
        td = matter_th.find_next('td')
        if td:
            matter_text = clean_text(td.get_text(separator="\n"))

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
        td = summary_th.find_next('td')
        if td:
            summary_text = clean_text(td.get_text(separator="\n"))

    return matter_text, summary_text, matter_label, summary_label

def extract_committee_from_detail(detail_soup):
    """
    detail.do 응답 HTML에서 위원회 이름 추출.
    팝업 상단에 "강원지방노동위원회 판정요지" 형식의 th/caption/h2/h3/h4 태그 탐색.

    ★ td 제외 이유: td는 본문 내용 셀까지 포함하므로 "중앙노동위원회"가
      조기 매칭될 수 있음 → th/caption/h2~h4 한정.

    ★ 이중 패스:
      1차) '노동위원회' + ('판정' or '결정') 동시 포함 → 타이틀 행 우선
      2차) '노동위원회'만 포함 (1차 미검출 시 fallback)
    """
    TITLE_TAGS = ['th', 'caption', 'h2', 'h3', 'h4']

    # 1차: 위원회명 + 판정/결정 키워드가 함께 있는 태그 (타이틀 행)
    for tag in detail_soup.find_all(TITLE_TAGS):
        text = tag.get_text(strip=True)
        if '노동위원회' in text and ('판정' in text or '결정' in text):
            match = re.match(r'(.+노동위원회)', text)
            if match:
                return match.group(1).strip()

    # 2차: 위원회명만 있는 태그 (fallback)
    for tag in detail_soup.find_all(TITLE_TAGS):
        text = tag.get_text(strip=True)
        if '노동위원회' in text:
            match = re.match(r'(.+노동위원회)', text)
            if match:
                return match.group(1).strip()

    return None

def extract_initial_case_from_html(html_content, current_case_number):
    """
    detail.do AJAX 응답 HTML에서 초심사건번호 추출.

    실제 HTML 구조 (개발자도구 확인):
      <input type="hidden" id="medi_numb" name="medi_numb" value="2025부해110">  ← 초심사건번호
      <input type="hidden" id="even_numb" name="even_numb" value="2025부해1582"> ← 재심사건번호(현재)
      <button ... title="초심보기" onclick="detailClick('JR')">초심보기</button>  ← onclick에 번호 없음!

    따라서 medi_numb hidden input의 value를 직접 읽는 것이 정확한 방법.
    """
    soup = BeautifulSoup(html_content, 'html.parser')

    # ★ 핵심: medi_numb hidden input에서 직접 추출
    medi_input = (
        soup.find('input', {'id': 'medi_numb'}) or
        soup.find('input', {'name': 'medi_numb'})
    )
    if medi_input:
        case_number = medi_input.get('value', '').strip()
        if case_number and case_number != current_case_number:
            print(f"   ✅ 초심사건번호 추출 (medi_numb): {case_number}")
            return case_number
        elif case_number:
            print(f"   ℹ️ medi_numb 값이 현재 사건번호와 동일 (초심 없음): {case_number}")
            return None

    print("   ℹ️ HTML에서 medi_numb 미검출 → fallback 시도")
    return None


async def get_initial_case_number_via_js(page, current_case_number):
    """
    JS DOM 쿼리로 medi_numb hidden input의 value 읽기.
    (AJAX HTML 파싱 실패 시 fallback)
    """
    try:
        result = await page.evaluate("""
            () => {
                const el = document.querySelector('#medi_numb, input[name="medi_numb"]');
                return el ? el.value : null;
            }
        """)

        if result and result.strip():
            case_number = result.strip()
            if case_number != current_case_number:
                print(f"   ✅ 초심사건번호 추출 (JS DOM medi_numb): {case_number}")
                return case_number
            else:
                print(f"   ℹ️ medi_numb DOM 값 = 현재 사건번호 (초심 없음)")
                return None

        print("   ℹ️ DOM에서 medi_numb 없음")
        return None
    except Exception as e:
        print(f"   ⚠️ JS DOM 오류: {e}")
        return None

async def get_initial_case_by_clicking(page):
    """
    현재 열린 팝업의 '초심보기' 버튼을 JS로 클릭하고,
    even_gubn 폴링으로 DOM 교체를 확인한 뒤 .layer-cont를 직접 읽어 파싱.

    ★ "재심보기 버튼 탐색" 방식을 버린 이유:
      - 버튼 텍스트·타이틀이 변경되지 않거나 렌더링 타이밍 문제로 미검출되는 경우 발생.

    ★ even_gubn 폴링 방식이 신뢰도 높은 이유:
      - 초심보기 클릭 → detailClick('JR') 실행 → AJAX 완료 후
        hidden input even_gubn 값이 'JS'(재심) → 'JR'(초심)으로 반드시 변경됨.
      - 이 변경을 감지하면 .layer-cont 내용이 초심사건으로 교체된 것이 확실.

    ★ 흐름:
      1) 초심보기 버튼 JS 클릭
      2) even_gubn이 'JR'로 바뀔 때까지 0.5초 간격으로 폴링 (최대 5초)
      3) .layer-cont outerHTML 추출
      4) BeautifulSoup 파싱 → 위원회·판정내용 추출
    """
    try:
        # Step 1: 초심보기 버튼 클릭
        clicked = await page.evaluate("""
            () => {
                const btn = Array.from(document.querySelectorAll('button')).find(
                    b => b.title === '초심보기' ||
                         (b.textContent && b.textContent.trim().includes('초심보기'))
                );
                if (btn) { btn.click(); return true; }
                return false;
            }
        """)
        if not clicked:
            print("   ℹ️ 초심보기 버튼 없음 (초심사건 없음)")
            return None

        # Step 2: even_gubn이 'JR'로 바뀔 때까지 폴링 (최대 5초)
        # 초심보기(detailClick('JR')) 클릭 → AJAX 완료 → even_gubn = 'JR'
        updated = False
        for attempt in range(10):
            await asyncio.sleep(0.5)
            gubn = await page.evaluate("""
                () => {
                    const el = document.querySelector('#even_gubn, input[name="even_gubn"]');
                    return el ? el.value : null;
                }
            """)
            if gubn == 'JR':
                updated = True
                print(f"   ✅ DOM 교체 확인 (even_gubn=JR, {(attempt+1)*0.5:.1f}초 소요)")
                break

        if not updated:
            print("   ⚠️ 5초 내 even_gubn 변경 미감지 → 강제 진행")

        # Step 3: .layer-cont 직접 읽기
        popup_html = await page.evaluate("""
            () => {
                const layerCont = document.querySelector('.layer-cont');
                if (layerCont) return layerCont.outerHTML;
                // fallback: .layer-wrap 전체
                const layerWrap = document.querySelector('.layer-wrap');
                if (layerWrap) return layerWrap.outerHTML;
                return null;
            }
        """)

        if not popup_html:
            print("   ⚠️ .layer-cont/.layer-wrap 미검출")
            return None

        # Step 4: 파싱
        initial_soup = BeautifulSoup(popup_html, 'html.parser')
        committee = extract_committee_from_detail(initial_soup)
        matter, summary, matter_label, summary_label = extract_matter_and_summary(initial_soup)

        print(f"   ✅ 초심사건 확보 (위원회: {committee})")
        return {
            'committee': committee,
            'matter': matter,
            'summary': summary,
            'matter_label': matter_label,
            'summary_label': summary_label
        }
    except Exception as e:
        print(f"   ⚠️ 초심보기 클릭 실패: {e}")
        return None

async def get_recent_judgments(search_keyword='부해', count=1):
    async with async_playwright() as p:
        chrome_args = ['--no-first-run', '--no-default-browser-check', '--disable-features=TranslateUI']
        try:
            browser = await p.chromium.launch(headless=True, channel="chrome", args=chrome_args)
        except Exception as e:
            print(f"⚠️ Chrome 실패, Chromium 대체: {e}")
            browser = await p.chromium.launch(headless=True, args=chrome_args)

        page = await browser.new_page()
        try:
            url = "https://nlrc.go.kr/nlrc/mainCase/judgment/search/index.do"
            print(f"🌐 {url} (키워드: {search_keyword}, {count}건)")
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await page.fill('#pQuery', search_keyword)
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
                async with page.expect_response(
                    lambda res: "/list.do" in res.url and res.status == 200, timeout=30000
                ) as response_info:
                    await page.click('.btnSearch')
                    list_content = await (await response_info.value).text()
            except:
                print("⚠️ 응답 대기 초과")
                list_content = await page.content()

            soup = BeautifulSoup(list_content, 'html.parser')
            dl_list = soup.find_all('dl', class_='C_Cts')
            print(f"📋 {len(dl_list)}건 발견")

            judgments = []
            for dl in dl_list:
                item_data = {
                    'case_number': '미검출',
                    'title': '',
                    'committee': '중앙노동위원회',
                    'decision_result': '결과 미표기',
                    'decision_date': '날짜 미표기',
                    'decision_matter': '상세 내용 없음',
                    'decision_summary': '상세 내용 없음',
                    'matter_label': '판정사항',
                    'summary_label': '판정요지',
                    'initial_case': None,
                    'initial_case_display': None,
                    'initial_case_data': None
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
                        item_data['decision_result'] = clean_text(em_dates[1].get_text().replace("|", ""))
                if item_data['case_number'] != '미검출':
                    judgments.append(item_data)

            def parse_date(date_str):
                try: return datetime.strptime(date_str, '%Y.%m.%d')
                except: return datetime.min

            judgments.sort(key=lambda x: parse_date(x['decision_date']), reverse=True)
            final_results = []

            for i, item in enumerate(judgments[:count]):
                print(f"🔎 [{i+1}/{count}] {item['case_number']} ({item['committee']}) 처리 중...")
                try:
                    target_selector = f'a[data-k2="{item["case_number"]}"]'

                    # 재심 상세 정보 캡처 (원본 방식 유지)
                    async with page.expect_response(
                        lambda res: "/detail.do" in res.url and res.status == 200, timeout=15000
                    ) as response_info:
                        await page.click(target_selector, force=True)
                        detail_content = await (await response_info.value).text()

                    detail_soup = BeautifulSoup(detail_content, 'html.parser')
                    matter, summary, matter_label, summary_label = extract_matter_and_summary(detail_soup)
                    item['decision_matter'] = matter
                    item['decision_summary'] = summary
                    item['matter_label'] = matter_label
                    item['summary_label'] = summary_label

                    # 중앙노동위원회 재심판정만 초심사건 처리
                    if '중앙' in item['committee']:
                        # ★ 방법 1: AJAX 응답 HTML에서 직접 파싱 (가장 신뢰도 높음)
                        initial_case_number = extract_initial_case_from_html(
                            detail_content, item['case_number']
                        )

                        # ★ 방법 2: JS DOM 쿼리 (HTML 파싱 실패 시 fallback)
                        if not initial_case_number:
                            print("   🔄 HTML 파싱 실패 → JS DOM 쿼리 시도...")
                            await asyncio.sleep(1)  # DOM 렌더링 대기
                            initial_case_number = await get_initial_case_number_via_js(
                                page, item['case_number']
                            )

                        if initial_case_number:
                            # ★ 초심보기 버튼 클릭 → AJAX 응답에서 위원회 + 내용 추출
                            # (사건번호가 여러 위원회에 중복될 수 있으므로 이 방식이 유일하게 정확)
                            await asyncio.sleep(0.5)
                            initial_data = await get_initial_case_by_clicking(page)
                            item['initial_case'] = initial_case_number

                            if initial_data and initial_data.get('committee'):
                                abbr = get_committee_abbr(initial_data['committee'])
                                item['initial_case_display'] = f"{abbr} {initial_case_number}"
                            else:
                                item['initial_case_display'] = initial_case_number

                            item['initial_case_data'] = initial_data
                        else:
                            item['initial_case'] = None
                            item['initial_case_display'] = None
                            item['initial_case_data'] = None

                    final_results.append(item)
                    print(f"   ✅ 완료 (초심: {item.get('initial_case_display') or '없음'})")

                    # 팝업 닫기
                    await page.evaluate("""
                        document.querySelectorAll(
                            '.layer-wrap, .layer-bg, .dimmed, .layer-close, .btnClose'
                        ).forEach(el => el.remove());
                        document.body.classList.remove('layer-open');
                        document.documentElement.style.overflow = 'auto';
                    """)
                    await asyncio.sleep(1)

                except Exception as detail_e:
                    print(f"   ⚠️ 실패 ({item['case_number']}): {detail_e}")
                    final_results.append(item)

            return final_results

        except Exception as e:
            print(f"❌ 크롤링 에러: {e}")
            return []
        finally:
            await browser.close()

def load_sent_cases():
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
    cases_list = list(sent_cases)[-200:]
    with open(LAST_CASE_FILE, "w", encoding="utf-8") as f:
        json.dump(cases_list, f, ensure_ascii=False, indent=4)

async def main():
    print("🤖 중앙노동위원회 알림 봇 가동 시작...")
    print(f"📬 수신 채팅방: {CHAT_IDS}")

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

        print(f"\n🔍 {len(CASE_CATEGORIES)}개 사건 종류 모니터링 중...")
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
        seen_case_numbers = set()   # 동일 실행 내 중복 방지
        now = datetime.now()
        for latest in reversed(items_to_send):
            if latest['case_number'] == '미검출': continue
            if latest['case_number'] in seen_case_numbers:
                print(f"   ⏭️ 중복 사건번호 스킵: {latest['case_number']}")
                continue
            days_diff = (now - parse_date(latest['decision_date'])).days
            if is_test or (latest['case_number'] not in sent_cases and days_diff <= MAX_DAYS_OLD):
                new_items.append(latest)
                seen_case_numbers.add(latest['case_number'])
            elif not is_test and latest['case_number'] not in sent_cases:
                sent_cases.add(latest['case_number'])
                save_sent_cases(sent_cases)

        sent_count = len(new_items)

        if sent_count > 0 and not is_test:
            send_telegram_message(
                f"🔔 이번 주 노동위원회 판정·결정요지 신규 업데이트는 총 {sent_count}건입니다."
            )

        for latest in new_items:
            print(f"🎉 발송: {latest['case_number']} ({latest['committee']})")

            matter_label = latest.get('matter_label', '판정사항')
            summary_label = latest.get('summary_label', '판정요지')

            # ① 재심 메시지
            message = (
                f"🚨 [노동위원회 판정·결정요지 신규 업데이트]\n\n"
                f"🏢 위원회: {latest['committee']}\n"
                f"🔢 사건번호: {latest['case_number']}\n"
            )
            if latest.get('initial_case_display'):
                message += f"🔗 초심사건: {latest['initial_case_display']}\n"

            message += (
                f"📅 판정일: {latest['decision_date']}\n"
                f"⚖️ 판정결과: {latest['decision_result']}\n"
                f"📝 제목: {latest['title']}\n\n"
                f"✅ [{matter_label}]\n{latest['decision_matter']}"
            )
            # 판정사건: 판정요지 항상 표시 (없으면 "상세 내용 없음")
            # 결정사건: 결정요지가 실제로 있을 때만 표시 (없으면 아예 생략)
            if matter_label == '판정사항':
                message += f"\n\n📖 [{summary_label}]\n{latest['decision_summary']}"
            elif latest['decision_summary'] != '상세 내용 없음':
                message += f"\n\n📖 [{summary_label}]\n{latest['decision_summary']}"

            sent_ids = send_telegram_message(message)

            # ② 초심사건 데이터 있으면 댓글(답글)로 전송
            if sent_ids and latest.get('initial_case') and latest.get('initial_case_data'):
                initial_data = latest['initial_case_data']
                init_committee = initial_data.get('committee', '')
                init_abbr = get_committee_abbr(init_committee) if init_committee else ''
                init_matter_label = initial_data.get('matter_label', '판정사항')
                init_summary_label = initial_data.get('summary_label', '판정요지')

                reply_message = (
                    f"🔍 [초심사건 정보: {latest['initial_case_display']}]\n"
                )
                if init_committee:
                    reply_message += f"🏢 위원회: {init_committee}\n\n"
                else:
                    reply_message += "\n"

                reply_message += f"✅ [{init_matter_label}]\n{initial_data['matter']}"
                if init_matter_label == '판정사항':
                    reply_message += f"\n\n📖 [{init_summary_label}]\n{initial_data['summary']}"
                elif initial_data['summary'] != '상세 내용 없음':
                    reply_message += f"\n\n📖 [{init_summary_label}]\n{initial_data['summary']}"

                send_telegram_message(reply_message, reply_to_message_ids=sent_ids)
                print(f"   💬 초심사건 댓글 전송 완료")

            if not is_test:
                sent_cases.add(latest['case_number'])
                save_sent_cases(sent_cases)

        if sent_count == 0 and not is_test:
            print("ℹ️ 신규 업데이트 없음")
            send_telegram_message("✅ 이번 주 노동위원회 판정·결정요지 신규 업데이트가 없습니다.")

        if is_test or is_github_actions:
            if is_test: print("\n🧪 테스트 모드 종료.")
            if is_github_actions: print("\n✅ GitHub Actions 1회 실행 완료.")
            break

        print("⏰ 6시간 후 다시 확인합니다...")
        await asyncio.sleep(21600)

if __name__ == "__main__":
    asyncio.run(main())
