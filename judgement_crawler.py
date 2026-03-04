import asyncio
import json
import os
import sys
import re
import html
import time
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

# 검색 키워드 → NLRC 카테고리 체크박스 레이블 매핑
# 미매핑 키워드는 '기타판정' 체크박스 선택
_CATEGORY_LABEL_MAP = {
    '부노': '부당해고',  '부해': '부당해고',
    '차별': '차별시정',
    '교섭': '교섭창구',
    '단위': '교섭단위',
    '공정': '공정대표',
}

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
                res_data = response.json()
                # Telegram 속도 제한(429) 발생 시 retry_after 만큼 대기 후 재시도
                if response.status_code == 429:
                    retry_after = res_data.get('parameters', {}).get('retry_after', 5)
                    print(f"  ⏳ [{chat_id}] 속도 제한 → {retry_after}초 대기 후 재시도")
                    time.sleep(retry_after)
                    response = requests.post(url, data=payload)
                    res_data = response.json()
                response.raise_for_status()
                if res_data.get('ok') and i == 0:
                    sent_message_ids[chat_id] = res_data['result']['message_id']
                print(f"  ✅ [{chat_id}] 파트 {i+1}/{len(parts)} 전송 성공!")
            except Exception as e:
                print(f"  ❌ [{chat_id}] 파트 {i+1} 전송 실패: {e}")
            # 채팅방 간 / 파트 간 전송 딜레이 (Telegram 속도 제한 예방)
            time.sleep(0.5)
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
    """
    TITLE_TAGS = ['th', 'caption', 'h2', 'h3', 'h4']
    for tag in detail_soup.find_all(TITLE_TAGS):
        text = tag.get_text(strip=True)
        if '노동위원회' in text and ('판정' in text or '결정' in text):
            match = re.match(r'(.+노동위원회)', text)
            if match:
                return match.group(1).strip()
    for tag in detail_soup.find_all(TITLE_TAGS):
        text = tag.get_text(strip=True)
        if '노동위원회' in text:
            match = re.match(r'(.+노동위원회)', text)
            if match:
                return match.group(1).strip()
    return None

def extract_hidden_vals_from_soup(detail_soup):
    """
    detail.do AJAX 응답 HTML에서 초심사건 조회에 필요한 파라미터 추출.
    """
    fields = ['medi_numb', 'begi_orga', 'even_numb', 'even_gubn', 'comm_code', 'midd_rscd']
    result = {}
    for field in fields:
        el = detail_soup.find('input', {'id': field}) or detail_soup.find('input', {'name': field})
        result[field] = el.get('value', '').strip() if el else ''
    initial_gubn = None
    for btn in detail_soup.find_all('button'):
        title = btn.get('title', '')
        text  = btn.get_text(strip=True)
        if title == '초심보기' or text == '초심보기':
            onclick = btn.get('onclick', '') or ''
            m = re.search(r"detailClick\(['\"](\w+)['\"]\)", onclick)
            if m:
                initial_gubn = m.group(1)
                break
    result['initial_gubn'] = initial_gubn or ''
    return result

async def fetch_initial_case(page, hidden_vals):
    """
    초심사건 상세 정보를 직접 fetch()로 가져온다.
    DOM·버튼 클릭 완전 우회 — detailClick의 stale DOM 문제 원천 해소.
    """
    medi_numb = hidden_vals.get('medi_numb', '')
    if not medi_numb:
        print("   ℹ️ medi_numb 없음 → 초심사건 없음")
        return None
    initial_gubn = hidden_vals.get('initial_gubn', '') or hidden_vals.get('even_gubn', '')
    begi_orga  = hidden_vals.get('begi_orga', '')
    comm_code  = begi_orga
    params = {
        'type':        'brjuPoin',
        'subType':     '06',
        'even_gubn':   initial_gubn,
        'comm_code':   comm_code,
        'begi_orga':   begi_orga,
        'even_numb':   medi_numb,
        'resu_yeno':   '',
        'midd_rscd':   hidden_vals.get('midd_rscd', ''),
        'detail_gubn': '',
    }
    print(f"   🌐 초심 직접 fetch: even_gubn={params['even_gubn']}, comm_code={comm_code}, even_numb={medi_numb}")
    try:
        initial_content = await page.evaluate("""
            async (params) => {
                const formData = new URLSearchParams(params);
                const response = await fetch(
                    '/nlrc/mainCase/judgment/search/detail.do',
                    {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                        body: formData.toString()
                    }
                );
                return await response.text();
            }
        """, params)
        print(f"   ✅ 초심 AJAX 응답 ({len(initial_content)}자), 사건번호 포함: {medi_numb in initial_content}")
        if medi_numb not in initial_content:
            print(f"   ⚠️ 응답에 초심사건번호({medi_numb}) 없음 → 파라미터 불일치 가능성")
        initial_soup = BeautifulSoup(initial_content, 'html.parser')
        committee = extract_committee_from_detail(initial_soup)
        matter, summary, matter_label, summary_label = extract_matter_and_summary(initial_soup)
        print(f"   ✅ 초심사건 확보 (위원회: {committee})")
        return {
            'committee':    committee,
            'matter':       matter,
            'summary':      summary,
            'matter_label': matter_label,
            'summary_label':summary_label
        }
    except Exception as e:
        print(f"   ⚠️ 초심 fetch 실패: {e}")
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
            # Enter 후 체크박스 렌더링 대기
            try:
                await page.wait_for_load_state('networkidle', timeout=15000)
            except Exception:
                await page.wait_for_timeout(2000)

            # ── pageUnit=30 설정 ─────────────────────────────────────────────
            await page.evaluate('''() => {
                const form = document.querySelector('#searchForm') || document.forms[0];
                if (form) {
                    let pu = form.querySelector('[name="pageUnit"]');
                    if (!pu) {
                        pu = document.createElement('input');
                        pu.type = 'hidden'; pu.name = 'pageUnit';
                        form.appendChild(pu);
                    }
                    pu.value = '30';
                }
            }''')

            # ── 카테고리 체크박스 선택 ───────────────────────────────────────
            # 선택 시 "더보기" 없이 바로 전체 목록이 반환됨 (안정적)
            # 미선택 시 카테고리별 ~5건 미리보기만 나와 count > 5 이면 데이터 누락 위험
            _cat_label = _CATEGORY_LABEL_MAP.get(search_keyword, '기타판정')

            _all_cbs = await page.query_selector_all('input[type="checkbox"]')
            for _cb in _all_cbs:
                await _cb.evaluate('cb => { cb.checked = false; }')

            _sel_result = await page.evaluate(f"""() => {{
                const target = {repr(_cat_label)};
                const allCbs = Array.from(document.querySelectorAll('input[type="checkbox"]'));
                if (!allCbs.length) return 'NO_CBS';
                for (const cb of allCbs) {{
                    const lbl = cb.closest('label');
                    if (lbl && lbl.textContent.includes(target)) {{
                        cb.checked = true; return 'label_wrap';
                    }}
                }}
                for (const cb of allCbs) {{
                    if (cb.id) {{
                        const lbl = document.querySelector('label[for="' + cb.id + '"]');
                        if (lbl && lbl.textContent.includes(target)) {{
                            cb.checked = true; return 'label_for';
                        }}
                    }}
                }}
                const allLabels = Array.from(document.querySelectorAll('label'));
                for (let i = 0; i < allLabels.length; i++) {{
                    if (allLabels[i].textContent.includes(target) && i < allCbs.length) {{
                        allCbs[i].checked = true; return 'index_match';
                    }}
                }}
                return 'NOT_FOUND';
            }}""")

            _selected_ok = _sel_result not in ('NO_CBS', 'NOT_FOUND')
            if not _selected_ok and _all_cbs:
                for _cb in _all_cbs:
                    await _cb.evaluate('cb => { cb.checked = true; }')
            print(f"   🗂️ 카테고리: [{_cat_label}] → "
                  f"{'✅ ' + _sel_result if _selected_ok else '⚠️ ' + _sel_result + ' → 전체 체크 복원'}")

            # ── 검색 실행 ────────────────────────────────────────────────────
            try:
                async with page.expect_response(
                    lambda res: "/list.do" in res.url and res.status == 200, timeout=30000
                ) as response_info:
                    await page.click('.btnSearch')
                list_content = await (await response_info.value).text()
            except Exception:
                print("⚠️ 응답 대기 초과")
                list_content = await page.content()

            soup = BeautifulSoup(list_content, 'html.parser')
            dl_list = soup.find_all('dl', class_='C_Cts')
            print(f"📋 {len(dl_list)}건 발견")

            judgments = []
            for dl in dl_list:
                item_data = {
                    'case_number':      '미검출',
                    'title':            '',
                    'committee':        '중앙노동위원회',
                    'decision_result':  '결과 미표기',
                    'decision_date':    '날짜 미표기',
                    'decision_matter':  '상세 내용 없음',
                    'decision_summary': '상세 내용 없음',
                    'matter_label':     '판정사항',
                    'summary_label':    '판정요지',
                    'initial_case':         None,
                    'initial_case_display': None,
                    'initial_case_data':    None
                }
                dt = dl.find('dt', class_='tit')
                if dt:
                    a_tag = dt.find('a')
                    if a_tag:
                        strong = a_tag.find('strong')
                        if strong: item_data['committee'] = clean_text(strong.get_text())
                        spans = a_tag.find_all('span')
                        if len(spans) >= 1: item_data['case_number'] = clean_text(spans[0].get_text())
                        if len(spans) >= 2: item_data['title']       = clean_text(spans[1].get_text())
                    em_dates = dt.find_all('em', class_='date')
                    if len(em_dates) >= 1: item_data['decision_date']   = clean_text(em_dates[0].get_text())
                    if len(em_dates) >= 2: item_data['decision_result'] = clean_text(em_dates[1].get_text().replace("|", ""))
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
                    async with page.expect_response(
                        lambda res: "/detail.do" in res.url and res.status == 200, timeout=15000
                    ) as response_info:
                        await page.click(target_selector, force=True)
                    detail_content = await (await response_info.value).text()

                    detail_soup = BeautifulSoup(detail_content, 'html.parser')
                    matter, summary, matter_label, summary_label = extract_matter_and_summary(detail_soup)
                    item['decision_matter']  = matter
                    item['decision_summary'] = summary
                    item['matter_label']     = matter_label
                    item['summary_label']    = summary_label

                    # 중앙노동위원회 재심판정만 초심사건 처리
                    if '중앙' in item['committee']:
                        hidden_vals = extract_hidden_vals_from_soup(detail_soup)
                        medi_numb = hidden_vals.get('medi_numb', '')
                        even_numb = hidden_vals.get('even_numb', '')
                        print(f"   📌 hidden_vals: even_numb={even_numb}, medi_numb={medi_numb}, even_gubn={hidden_vals.get('even_gubn')}")

                        initial_case_number = None
                        if medi_numb and medi_numb != even_numb and medi_numb != item['case_number']:
                            initial_case_number = medi_numb
                            print(f"   ✅ 초심사건번호 추출: {initial_case_number}")
                        else:
                            print(f"   ℹ️ 초심사건 없음 (medi_numb={medi_numb!r})")

                        if initial_case_number:
                            initial_data = await fetch_initial_case(page, hidden_vals)
                            item['initial_case'] = initial_case_number
                            if initial_data and initial_data.get('committee'):
                                abbr = get_committee_abbr(initial_data['committee'])
                                item['initial_case_display'] = f"{abbr} {initial_case_number}"
                            else:
                                item['initial_case_display'] = initial_case_number
                            item['initial_case_data'] = initial_data
                        else:
                            item['initial_case']         = None
                            item['initial_case_display'] = None
                            item['initial_case_data']    = None

                    final_results.append(item)
                    print(f"   ✅ 완료 (초심: {item.get('initial_case_display') or '없음'})")

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
    seen_case_numbers = set()
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
        matter_label  = latest.get('matter_label', '판정사항')
        summary_label = latest.get('summary_label', '판정요지')

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
        if matter_label == '판정사항':
            message += f"\n\n📖 [{summary_label}]\n{latest['decision_summary']}"
        elif latest['decision_summary'] != '상세 내용 없음':
            message += f"\n\n📖 [{summary_label}]\n{latest['decision_summary']}"

        sent_ids = send_telegram_message(message)

        if sent_ids and latest.get('initial_case') and latest.get('initial_case_data'):
            initial_data = latest['initial_case_data']
            init_committee   = initial_data.get('committee', '')
            init_matter_label  = initial_data.get('matter_label', '판정사항')
            init_summary_label = initial_data.get('summary_label', '판정요지')

            reply_message = f"🔍 [초심사건 정보: {latest['initial_case_display']}]\n"
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

    print("\n✅ 실행 완료.")

if __name__ == "__main__":
    asyncio.run(main())
