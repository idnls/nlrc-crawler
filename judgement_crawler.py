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
# 1. 텔레그램 봇 설정 (토큰 및 ID 하드코딩 제거, 다중 ID 지원)
# ==========================================
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '').strip()
RAW_CHAT_IDS = os.environ.get('CHAT_ID', '')

# 쉼표(,)로 구분된 다중 ID를 리스트로 변환
CHAT_IDS = [cid.strip() for cid in RAW_CHAT_IDS.split(',')] if RAW_CHAT_IDS else []

LAST_CASE_FILE = "last_case.json"
CASE_CATEGORIES = ['부해', '부노', '차별', '교섭', '단위', '공정', '단협', '손해', '의결', '휴업', '재해', '상병', '노협']
MAX_DAYS_OLD = 60

def clean_text(text):
    if not text: return ""
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()

def safe_html(text):
    return html.escape(str(text))

def send_telegram_message(text, reply_to_message_ids=None):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        return {}
        
    MAX_LENGTH = 4000
    parts = []
    while len(text) > MAX_LENGTH:
        split_index = text.rfind('\n', 0, MAX_LENGTH)
        if split_index == -1:
            split_index = MAX_LENGTH
        parts.append(text[:split_index].strip())
        text = text[split_index:].strip()
    parts.append(text)
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    first_message_ids = {} # 각 채팅방별 첫 메시지 ID 저장 {chat_id: message_id}
    
    for chat_id in CHAT_IDS:
        if not chat_id: continue
        
        # 현재 chat_id에 해당하는 답글 대상 메시지 ID 찾기
        reply_to_id = None
        if reply_to_message_ids and isinstance(reply_to_message_ids, dict):
            reply_to_id = reply_to_message_ids.get(chat_id)
            
        for i, part in enumerate(parts):
            message_to_send = part
            if len(parts) > 1:
                message_to_send = f"[{i+1}/{len(parts)}]\n" + part
            
            payload = {'chat_id': chat_id, 'text': message_to_send, 'parse_mode': 'HTML'}
            if reply_to_id and i == 0: # 분할 메시지일 경우 첫 번째 파트에만 답장 처리
                payload['reply_to_message_id'] = reply_to_id
                
            try:
                response = requests.post(url, data=payload)
                response.raise_for_status()
                res_data = response.json()
                if i == 0 and res_data.get('ok'):
                    first_message_ids[chat_id] = res_data['result']['message_id']
            except Exception as e:
                print(f"Error sending message to {chat_id}: {e}")
                
    return first_message_ids

async def fetch_initial_case_details(page, case_number):
    try:
        url = "https://nlrc.go.kr/nlrc/mainCase/judgment/search/index.do"
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await page.fill('#pQuery', case_number)
        await page.focus('#pQuery')
        await page.keyboard.press('Enter')
        
        try:
            async with page.expect_response(lambda res: "/list.do" in res.url and res.status == 200, timeout=15000):
                await page.click('.btnSearch')
        except:
            pass
            
        list_content = await page.content()
        soup = BeautifulSoup(list_content, 'html.parser')
        dl_list = soup.find_all('dl', class_='C_Cts')
        
        target_case = None
        for dl in dl_list:
            dt = dl.find('dt', class_='tit')
            if dt:
                a_tag = dt.find('a')
                if a_tag:
                    spans = a_tag.find_all('span')
                    if len(spans) >= 1 and case_number in spans[0].get_text():
                        target_case = a_tag
                        break
                        
        if not target_case:
            return None
            
        extracted_data = {'matter': '', 'summary': ''}
        
        async with page.expect_response(lambda res: "/detail.do" in res.url and res.status == 200, timeout=15000) as response_info:
            target_selector = f'a[data-k2="{case_number}"]'
            await page.click(target_selector, force=True)
            detail_resp_obj = await response_info.value
            detail_content = await detail_resp_obj.text()
            
            detail_soup = BeautifulSoup(detail_content, 'html.parser')
            matter_th = detail_soup.find('th', string=re.compile(r'^판정사항$')) or detail_soup.find('th', string='판정사항')
            if matter_th:
                matter_td = matter_th.find_next('td')
                if matter_td: extracted_data['matter'] = clean_text(matter_td.get_text(separator="\n"))
                
            summary_ths = detail_soup.find_all('th', string=re.compile('판정요지'))
            for th in summary_ths:
                if th.get_text(strip=True) == "판정요지":
                    summary_td = th.find_next('td')
                    if summary_td: extracted_data['summary'] = clean_text(summary_td.get_text(separator="\n"))
                    break
                    
        return extracted_data
    except Exception:
        return None

async def get_recent_judgments(search_keyword='부해', count=1):
    async with async_playwright() as p:
        chrome_args = ['--no-first-run', '--no-default-browser-check', '--disable-features=TranslateUI']
        try:
            browser = await p.chromium.launch(headless=True, channel="chrome", args=chrome_args)
        except Exception:
            browser = await p.chromium.launch(headless=True, args=chrome_args)
        page = await browser.new_page()
        try:
            url = "https://nlrc.go.kr/nlrc/mainCase/judgment/search/index.do"
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
                async with page.expect_response(lambda res: "/list.do" in res.url and res.status == 200, timeout=30000) as response_info:
                    await page.click('.btnSearch')
                    list_resp = await response_info.value
                    list_content = await list_resp.text()
            except:
                list_content = await page.content()
            soup = BeautifulSoup(list_content, 'html.parser')
            dl_list = soup.find_all('dl', class_='C_Cts')
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
                try: return datetime.strptime(date_str, '%Y.%m.%d')
                except: return datetime.min
            judgments.sort(key=lambda x: parse_date(x['decision_date']), reverse=True)
            final_results = []
            for i, item in enumerate(judgments[:count]):
                try:
                    target_selector = f'a[data-k2="{item["case_number"]}"]'
                    async with page.expect_response(lambda res: "/detail.do" in res.url and res.status == 200, timeout=15000) as response_info:
                        await page.click(target_selector, force=True)
                        detail_resp_obj = await response_info.value
                        detail_content = await detail_resp_obj.text()
                        detail_soup = BeautifulSoup(detail_content, 'html.parser')
                        matter_th = detail_soup.find('th', string=re.compile(r'^판정사항$')) or detail_soup.find('th', string='판정사항')
                        if matter_th:
                            matter_td = matter_th.find_next('td')
                            if matter_td: item['decision_matter'] = clean_text(matter_td.get_text(separator="\n"))
                        summary_th = detail_soup.find('th', string='판정요지')
                        if summary_th:
                            summary_td = summary_th.find_next('td')
                            if summary_td: item['decision_summary'] = clean_text(summary_td.get_text(separator="\n"))
                        else:
                            summary_ths = detail_soup.find_all('th', string=re.compile('판정요지'))
                            for th in summary_ths:
                                if th.get_text(strip=True) == "판정요지":
                                    summary_td = th.find_next('td')
                                    if summary_td: item['decision_summary'] = clean_text(summary_td.get_text(separator="\n"))
                                    break
                        chosim_btn = detail_soup.find(lambda t: t.name in ['a', 'button', 'span'] and '초심보기' in t.get_text())
                        if chosim_btn:
                            btn_html = str(chosim_btn)
                            match = re.search(r'([0-9]{4}[가-힣]+[0-9]+)', btn_html)
                            if match:
                                item['initial_case'] = match.group(1)
                                
                    await page.evaluate("""
                        document.querySelectorAll('.layer-wrap, .layer-bg, .dimmed, .layer-close, .btnClose').forEach(el => el.remove());
                        document.body.classList.remove('layer-open');
                        document.documentElement.style.overflow = 'auto';
                    """)
                    
                    if item['initial_case']:
                        item['initial_case_data'] = await fetch_initial_case_details(page, item['initial_case'])
                    else:
                        item['initial_case_data'] = None
                        
                    final_results.append(item)
                    await asyncio.sleep(1)
                except Exception:
                    final_results.append(item)
            return final_results
        except Exception:
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
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        print("❌ 오류: TELEGRAM_TOKEN 또는 CHAT_ID 환경 변수가 설정되지 않았습니다.")
        return

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
        for category in CASE_CATEGORIES:
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
            send_telegram_message(f"🔔 이번 주 노동위원회 판정·결정요지 신규 업데이트는 총 {sent_count}건입니다.")
            
        for latest in new_items:
            message = (
                f"🚨 <b>[노동위원회 판정·결정요지 신규 업데이트]</b>\n\n"
                f"🏢 위원회: {safe_html(latest['committee'])}\n"
                f"🔢 사건번호: {safe_html(latest['case_number'])}\n"
            )
            if latest.get('initial_case'):
                message += f"🔗 초심사건: {safe_html(latest['initial_case'])}\n"
            message += (
                f"📅 판정일: {safe_html(latest['decision_date'])}\n"
                f"⚖️ 판정결과: {safe_html(latest['decision_result'])}\n"
                f"📝 제목: {safe_html(latest['title'])}\n\n"
                f"✅ <b>[판정사항]</b>\n{safe_html(latest['decision_matter'])}\n\n"
                f"📖 <b>[판정요지]</b>\n{safe_html(latest['decision_summary'])}"
            )
            
            # 메인 메시지를 발송하고, 각 채팅방별 메시지 ID 딕셔너리를 반환받음
            main_msg_ids_dict = send_telegram_message(message)
            
            # 초심 사건 정보가 존재하고 메인 메시지 발송에 성공했다면 답장 발송
            if main_msg_ids_dict and latest.get('initial_case_data'):
                initial_data = latest['initial_case_data']
                reply_message = (
                    f"🔍 <b>[초심사건 판정정보: {safe_html(latest['initial_case'])}]</b>\n\n"
                    f"✅ <b>[초심 판정사항]</b>\n{safe_html(initial_data['matter'])}\n\n"
                    f"📖 <b>[초심 판정요지]</b>\n{safe_html(initial_data['summary'])}"
                )
                # 각 방의 메인 메시지 ID를 매칭하여 답장(Reply) 형태로 발송
                send_telegram_message(reply_message, reply_to_message_ids=main_msg_ids_dict)
                
            if not is_test:
                sent_cases.add(latest['case_number'])
                save_sent_cases(sent_cases)
                
        if sent_count == 0 and not is_test:
            send_telegram_message("✅ 이번 주 노동위원회 판정·결정요지 신규 업데이트가 없습니다.")
            
        if is_test or is_github_actions:
            break
        await asyncio.sleep(21600)

if __name__ == "__main__":
    asyncio.run(main())
