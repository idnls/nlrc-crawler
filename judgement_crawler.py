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

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN') or '토큰 입력'
CHAT_ID = os.environ.get('CHAT_ID') or 'id 입력'

LAST_CASE_FILE = "last_case.json"
CASE_CATEGORIES = ['부해', '부노', '차별', '교섭', '단위', '공정', '단협', '손해', '의결', '휴업', '재해', '상병', '노협']
MAX_DAYS_OLD = 60

def clean_text(text):
    if not text: return ""
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()

def send_telegram_message(text):
    if TELEGRAM_TOKEN == '토큰 입력' or CHAT_ID == 'id 입력':
        return

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
    
    chat_id_list = [cid.strip() for cid in str(CHAT_ID).split(',') if cid.strip()]

    for cid in chat_id_list:
        for i, part in enumerate(parts):
            message_to_send = part
            if len(parts) > 1:
                message_to_send = f"[{i+1}/{len(parts)}]\n" + part

            payload = {'chat_id': cid, 'text': message_to_send}
            try:
                response = requests.post(url, data=payload)
                response.raise_for_status()
            except Exception:
                pass

async def get_recent_judgments(search_keyword='부해', count=1):
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True, channel="chrome")
        except Exception:
            browser = await p.chromium.launch(headless=True)
            
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

            def parse_date(date_str):
                try:
                    return datetime.strptime(date_str, '%Y.%m.%d')
                except:
                    return datetime.min

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
                    
                    final_results.append(item)

                    await page.evaluate("""
                        document.querySelectorAll('.layer-wrap, .layer-bg, .dimmed, .layer-close, .btnClose').forEach(el => el.remove());
                        document.body.classList.remove('layer-open');
                        document.documentElement.style.overflow = 'auto';
                    """)
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
                if isinstance(data, dict):
                    return data.get("sent_cases", [])
                if isinstance(data, list):
                    return data
            except:
                return []
    return []

def save_sent_cases(sent_cases):
    cases_list = sent_cases[-200:]
    data = {
        "sent_cases": cases_list,
        "last_run": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    with open(LAST_CASE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

async def main():
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
        
        from datetime import datetime
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
                sent_cases.append(latest['case_number'])
                save_sent_cases(sent_cases)

        sent_count = len(new_items)
        
        if sent_count > 0 and not is_test:
            send_telegram_message(f"🔔 이번 주 노동위원회 판정·결정요지 신규 업데이트는 총 {sent_count}건입니다.")

        for latest in new_items:
            message = (
                f"🚨 [노동위원회 판정·결정요지 신규 업데이트]\n\n"
                f"🏢 위원회: {latest['committee']}\n"
                f"🔢 사건번호: {latest['case_number']}\n"
                f"📅 판정일: {latest['decision_date']}\n"
                f"⚖️ 판정결과: {latest['decision_result']}\n"
                f"📝 제목: {latest['title']}\n\n"
                f"✅ [판정사항]\n{latest['decision_matter']}\n\n"
                f"📖 [판정요지]\n{latest['decision_summary']}"
            )
            
            send_telegram_message(message)
            
            if not is_test:
                if latest['case_number'] not in sent_cases:
                    sent_cases.append(latest['case_number'])
                save_sent_cases(sent_cases)

        if sent_count == 0 and not is_test:
            send_telegram_message("✅ 이번 주 노동위원회 판정·결정요지 신규 업데이트가 없습니다.")

        if is_test or is_github_actions:
            break
            
        await asyncio.sleep(21600)

if __name__ == "__main__":
    asyncio.run(main())
